"""Temporal knowledge graph service (ADR-0044, Track B).

Builds and queries a typed, bi-temporal knowledge graph over the org's notes:

- ``extract_facts`` runs the per-org METERED LLM (resolve_llm, op='extract')
  over a note body, parses (entity, relation, entity) triples defensively,
  resolves/dedupes entities, and writes facts -- born ``review_state=
  'proposed'`` when autonomous (ADR-0043), else effective.
- ``ensure_entity`` is the idempotent get-or-create resolver (one canonical
  node per (org, type, normalized name), the identities.ensure_* race pattern).
- ``add_fact`` is idempotent on the current triple; ``supersede_fact`` closes
  a fact's valid window and links the replacement (a temporal update keeps the
  old fact as-of-queryable); ``invalidate_fact`` tombstones a wrong fact
  (invalidate-not-delete; a DB trigger freezes it thereafter).
- ``entity_facts`` / ``traverse`` answer 1-hop and multi-hop questions with an
  optional ``as_of`` valid-time clamp; effective facts only.

Effective + as-of predicate: ``invalidated_at IS NULL AND review_state IS
DISTINCT FROM 'proposed' AND (valid_from IS NULL OR valid_from <= t) AND
(valid_to IS NULL OR valid_to > t)`` where t = ``as_of`` or now.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.kg import KG_ENTITY_TYPES, KgEdge, KgEntity, KgEntitySource
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.identities import ensure_for_user
from mycelium_core.services.llm_resolver import resolve_llm
from mycelium_core.services.rbac import require_role

_WS_RE = re.compile(r"\s+")
_PRED_RE = re.compile(r"[^a-z0-9]+")
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_log = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "You extract a knowledge graph from the user's note. Output ONLY a single "
    "JSON object, no prose and no markdown fences, with exactly this shape:\n"
    '{"entities": [{"name": "<canonical name>", "type": '
    '"person|organization|project|place|product|event|concept|other"}], '
    '"relations": [{"subject": "<entity name>", "predicate": "<lower_snake_case>", '
    '"object": "<entity name>", "valid_from": "YYYY-MM-DD or null", '
    '"valid_to": "YYYY-MM-DD or null", "confidence": 0.0}]}\n'
    "Rules: every relation's subject and object MUST also appear in entities. "
    "Use concise canonical entity names and a small consistent predicate "
    "vocabulary (e.g. works_at, located_in, part_of, member_of, knows, "
    "depends_on, created, related_to). Extract ONLY facts stated in the note; "
    "do not infer or invent. Omit a date (use null) when the note gives none. "
    "If there are no facts, return empty arrays."
)


def normalize_entity_name(name: str) -> str:
    """Dedupe key for an entity: Unicode-NFKC-normalized (so ``Café`` in NFC and
    NFD, or full/half-width variants, collapse to ONE node), then trimmed,
    whitespace-collapsed, casefolded."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", name).strip()).casefold()


def normalize_predicate(predicate: str) -> str:
    """Predicates are an open vocabulary normalized to ``lower_snake_case``;
    empty/garbage collapses to the generic ``related_to``."""
    norm = _PRED_RE.sub("_", predicate.strip().lower()).strip("_")
    return (norm or "related_to")[:64]


def _coerce_entity_type(value: Any) -> str:
    candidate = value.strip().lower() if isinstance(value, str) else ""
    return candidate if candidate in KG_ENTITY_TYPES else "other"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _review_state_for(autonomous: bool) -> str | None:
    """Autonomous extraction is born 'proposed' (withheld until adjudicated)
    when the review gate is on; user-initiated extraction is effective. Mirrors
    decomposition._review_state_for (ADR-0043)."""
    return "proposed" if autonomous and get_settings().garden_review_gate_enabled else None


@dataclass(frozen=True)
class ExtractionResult:
    entities: int  # distinct entities referenced (resolved or created)
    facts: int  # distinct current facts referenced
    edge_ids: list[uuid.UUID]
    model_id: str


@dataclass(frozen=True)
class FactView:
    edge_id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    predicate: str
    object_id: uuid.UUID
    object_name: str
    valid_from: datetime.datetime | None
    valid_to: datetime.datetime | None
    confidence: Decimal | None
    review_state: str | None


def _parse_extraction(raw: str) -> dict[str, Any]:
    """Defensive parse of the extractor's output (no provider JSON-mode): try
    the whole string, then the first ``{...}`` block; never raise."""
    for candidate in (raw.strip(), None):
        text = candidate if candidate is not None else (_first_json_block(raw))
        if not text:
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return {"entities": [], "relations": []}


def _first_json_block(raw: str) -> str | None:
    match = _JSON_BLOCK_RE.search(raw)
    return match.group(0) if match else None


def _parse_dt(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def _parse_confidence(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        conf = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return max(Decimal("0"), min(Decimal("1"), conf))


async def _actor_identity_id(
    session: AsyncSession, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """The author Identity for an action (user OR ai_assistant, ADR-0028)."""
    identity = await ensure_for_user(session, org_id=org_id, user_id=actor_id)
    return identity.id


async def ensure_entity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    entity_type: Any = "other",
    origin_model_id: str | None = None,
    created_by: uuid.UUID | None = None,
) -> KgEntity:
    """Idempotent get-or-create keyed by (org, type, normalized name). Mirrors
    identities.ensure_* : SAVEPOINT-isolated insert, re-select the winner on a
    concurrent unique violation. The caller is responsible for role-gating."""
    norm = normalize_entity_name(name)
    etype = _coerce_entity_type(entity_type)
    found = (
        await session.execute(
            select(KgEntity).where(
                KgEntity.org_id == org_id,
                KgEntity.entity_type == etype,
                KgEntity.normalized_name == norm,
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    entity = KgEntity(
        org_id=org_id,
        entity_type=etype,
        name=name.strip()[:512],
        normalized_name=norm[:512],
        origin_model_id=origin_model_id,
        created_by=created_by,
    )
    session.add(entity)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        entity = (
            await session.execute(
                select(KgEntity).where(
                    KgEntity.org_id == org_id,
                    KgEntity.entity_type == etype,
                    KgEntity.normalized_name == norm,
                )
            )
        ).scalar_one()
    return entity


async def _link_entity_source(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID,
    source_note_id: uuid.UUID,
) -> None:
    """Record that ``source_note_id`` is a provenance of ``entity_id`` (the GDPR
    erase handle for edge-less entities, migration 0069). Idempotent on
    (org, entity, note) so re-extraction never duplicates the link."""
    await session.execute(
        pg_insert(KgEntitySource)
        .values(org_id=org_id, entity_id=entity_id, source_note_id=source_note_id)
        .on_conflict_do_nothing(index_elements=["org_id", "entity_id", "source_note_id"])
    )


async def add_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    subject_id: uuid.UUID,
    predicate: str,
    object_id: uuid.UUID,
    valid_from: datetime.datetime | None = None,
    valid_to: datetime.datetime | None = None,
    confidence: Decimal | None = None,
    source_note_id: uuid.UUID | None = None,
    origin_model_id: str | None = None,
    review_state: str | None = None,
    created_by: uuid.UUID | None = None,
) -> KgEdge:
    """Insert a fact, idempotent on the OPEN current triple (org, subject,
    predicate, object with no ``valid_to``). Role-gating is the caller's
    responsibility.

    Idempotency keys the OPEN-ended fact only (``invalidated_at IS NULL AND
    valid_to IS NULL``), mirroring ``uq_kg_edge_current`` (migration 0068): a
    superseded (closed) row no longer shadows a re-assertion of the same triple,
    and an explicitly-windowed historical fact is always inserted (never deduped
    against the live one). Use ``supersede_fact``/``invalidate_fact`` to change
    an existing fact's window or confidence -- this is a pure get-or-create."""
    pred = normalize_predicate(predicate)
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        # A zero-width / inverted window is invisible to every read (the half-open
        # predicate needs valid_from <= t < valid_to); reject it cleanly here
        # rather than as a raw ck_kg_edge_valid_window IntegrityError.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    existing = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.subject_id == subject_id,
                KgEdge.predicate == pred,
                KgEdge.object_id == object_id,
                KgEdge.invalidated_at.is_(None),
                KgEdge.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None and valid_to is None:
        return existing
    edge = KgEdge(
        org_id=org_id,
        subject_id=subject_id,
        object_id=object_id,
        predicate=pred,
        valid_from=valid_from,
        valid_to=valid_to,
        confidence=confidence,
        source_note_id=source_note_id,
        origin_model_id=origin_model_id,
        review_state=review_state,
        created_by=created_by,
    )
    session.add(edge)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        if valid_to is not None:
            # The uq_kg_edge_current race only applies to the OPEN fact; a
            # windowed historical insert that violated something (CHECK / FK)
            # is a real error, not a recoverable dedupe.
            raise
        edge = (
            await session.execute(
                select(KgEdge).where(
                    KgEdge.org_id == org_id,
                    KgEdge.subject_id == subject_id,
                    KgEdge.predicate == pred,
                    KgEdge.object_id == object_id,
                    KgEdge.invalidated_at.is_(None),
                    KgEdge.valid_to.is_(None),
                )
            )
        ).scalar_one()
    return edge


async def extract_facts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    llm: LLMProvider | None = None,
    autonomous: bool = False,
) -> ExtractionResult:
    """Extract a typed knowledge graph from a note's body via the per-org
    metered LLM and persist entities + facts. Facts are born 'proposed' when
    ``autonomous`` and the review gate is on, else effective. ``llm`` is an
    explicit test/override injection (decomposition pattern)."""
    await require_role(session, org_id, actor_id, Role.member)
    # Read the note row before its text (task a186c989): ``get_body`` joins
    # the parts and never looks at ``notes``, so without this the body of a
    # trashed note or of an un-approved proposal went into the metered
    # prompt AND came back out as effective KG facts. Same guard the twin
    # ``decomposition.distill_note`` has always had.
    await notes_svc.get_note(session, org_id=org_id, note_id=note_id)
    body = await notes_svc.get_body(session, note_id=note_id)
    if not body or not body.strip():
        raise DomainError(MessageCode.DOMAIN_ERROR)
    provider = llm or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"kg_extract:{org_id}:{note_id}",
        op="extract",
    )
    res = await provider.complete(system=_EXTRACT_SYSTEM, messages=[("user", body)])
    parsed = _parse_extraction(res.text)
    identity_id = await _actor_identity_id(session, org_id, actor_id)
    review = _review_state_for(autonomous)

    # Cache mirrors the DB dedupe key (type, normalized_name) so two same-name
    # entities of DIFFERENT types (e.g. "Apple" the org vs the product) stay
    # distinct instead of the second silently overwriting the first. A second
    # map (normalized_name -> first typed entity) lets an untyped relation
    # endpoint reuse a typed entity from the entities[] pass rather than minting
    # a spurious 'other' duplicate.
    by_key: dict[tuple[str, str], KgEntity] = {}
    by_name: dict[str, KgEntity] = {}

    async def _entity(name: str, entity_type: Any) -> KgEntity:
        norm = normalize_entity_name(name)
        etype = _coerce_entity_type(entity_type)
        if etype == "other" and norm in by_name:
            return by_name[norm]
        key = (etype, norm)
        if key in by_key:
            return by_key[key]
        ent = await ensure_entity(
            session,
            org_id=org_id,
            name=name,
            entity_type=etype,
            origin_model_id=res.model_id,
            created_by=identity_id,
        )
        by_key[key] = ent
        by_name.setdefault(norm, ent)
        return ent

    specs = parsed.get("entities")
    if isinstance(specs, list):
        for spec in specs:
            if (
                isinstance(spec, dict)
                and isinstance(spec.get("name"), str)
                and spec["name"].strip()
            ):
                await _entity(spec["name"], spec.get("type"))

    edge_ids: list[uuid.UUID] = []
    entity_ids: set[uuid.UUID] = set()
    relations = parsed.get("relations")
    if isinstance(relations, list):
        for rel in relations:
            if not isinstance(rel, dict):
                continue
            subj_name = rel.get("subject")
            pred = rel.get("predicate")
            obj_name = rel.get("object")
            if not (
                isinstance(subj_name, str)
                and subj_name.strip()
                and isinstance(pred, str)
                and pred.strip()
                and isinstance(obj_name, str)
                and obj_name.strip()
            ):
                continue
            subj = await _entity(subj_name, "other")
            obj = await _entity(obj_name, "other")
            entity_ids.update((subj.id, obj.id))
            if subj.id == obj.id:  # the DB CHECK forbids a self-edge
                continue
            edge = await add_fact(
                session,
                org_id=org_id,
                subject_id=subj.id,
                predicate=pred,
                object_id=obj.id,
                valid_from=_parse_dt(rel.get("valid_from")),
                valid_to=_parse_dt(rel.get("valid_to")),
                confidence=_parse_confidence(rel.get("confidence")),
                source_note_id=note_id,
                origin_model_id=res.model_id,
                review_state=review,
                created_by=identity_id,
            )
            edge_ids.append(edge.id)

    entity_ids.update(e.id for e in by_key.values())
    # Record note provenance for EVERY resolved entity (incl. ones that appear
    # only in entities[] with no relation -> no edge), so a GDPR erase can reach
    # them later (migration 0069). Idempotent.
    for ent in by_key.values():
        await _link_entity_source(session, org_id=org_id, entity_id=ent.id, source_note_id=note_id)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="kg_extract",
        diff={"entities": len(entity_ids), "facts": len(edge_ids), "model_id": res.model_id},
    )
    return ExtractionResult(
        entities=len(entity_ids),
        facts=len(edge_ids),
        edge_ids=edge_ids,
        model_id=res.model_id,
    )


async def invalidate_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    edge_id: uuid.UUID,
    superseded_by_edge_id: uuid.UUID | None = None,
    now: datetime.datetime | None = None,
) -> KgEdge:
    """Tombstone a fact (it was WRONG / retracted): set invalidated_at so it
    leaves every read. Invalidate-not-delete -- the row stays as audit history
    and the DB trigger freezes it from further mutation."""
    await require_role(session, org_id, actor_id, Role.member)
    edge = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.id == edge_id,
                KgEdge.invalidated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    edge.invalidated_at = now or _now()
    edge.invalidated_by = await _actor_identity_id(session, org_id, actor_id)
    edge.superseded_by_edge_id = superseded_by_edge_id
    edge.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="kg_edge",
        entity_id=edge_id,
        action="kg_invalidate",
    )
    return edge


async def supersede_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    old_edge_id: uuid.UUID,
    subject_id: uuid.UUID,
    predicate: str,
    object_id: uuid.UUID,
    valid_from: datetime.datetime | None = None,
    confidence: Decimal | None = None,
    source_note_id: uuid.UUID | None = None,
    origin_model_id: str | None = None,
) -> tuple[KgEdge, KgEdge]:
    """A TEMPORAL update: the old fact stopped being true (close its
    ``valid_to``) and a new fact takes over from ``valid_from``. The old fact
    is NOT invalidated -- it remains true for its window and as-of-queryable;
    they are chained via ``superseded_by_edge_id``. Returns (old, new)."""
    await require_role(session, org_id, actor_id, Role.member)
    old = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.id == old_edge_id,
                KgEdge.invalidated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if old is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    cutover = valid_from or _now()
    if old.valid_from is not None and cutover <= old.valid_from:
        # The cutover would close the old fact at or before its own start,
        # producing an empty/inverted window (and a ck_kg_edge_valid_window
        # violation). A correction at/under the start is an invalidate, not a
        # temporal supersede -- reject explicitly.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    identity_id = await _actor_identity_id(session, org_id, actor_id)
    new_edge = await add_fact(
        session,
        org_id=org_id,
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        valid_from=cutover,
        confidence=confidence,
        source_note_id=source_note_id,
        origin_model_id=origin_model_id,
        created_by=identity_id,
    )
    if new_edge.id != old.id:
        if old.valid_to is None or old.valid_to > cutover:
            old.valid_to = cutover
        old.superseded_by_edge_id = new_edge.id
        old.version += 1
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="kg_edge",
            entity_id=old.id,
            action="kg_supersede",
            diff={"superseded_by": str(new_edge.id)},
        )
    return old, new_edge


async def approve_fact(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, edge_id: uuid.UUID
) -> KgEdge:
    """Approve a proposed fact: it becomes effective (review_state='approved',
    which is DISTINCT FROM 'proposed'). ADR-0043 review gate for facts."""
    await require_role(session, org_id, actor_id, Role.member)
    edge = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.id == edge_id,
                KgEdge.review_state == "proposed",
                KgEdge.invalidated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    edge.review_state = "approved"
    edge.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="kg_edge",
        entity_id=edge_id,
        action="kg_approve",
    )
    return edge


async def reject_fact(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, edge_id: uuid.UUID
) -> KgEdge:
    """Reject a proposed fact: invalidate it (invalidate-not-delete)."""
    return await invalidate_fact(session, org_id=org_id, actor_id=actor_id, edge_id=edge_id)


async def erase_by_source(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_note_id: uuid.UUID,
) -> int:
    """GDPR erase-by-provenance for the KG: HARD-DELETE every fact extracted
    from ``source_note_id`` -- including invalidated history, because the right
    to erasure overrides invalidate-not-delete -- drop the note's entity
    provenance links, and delete every entity then left with NO remaining
    provenance AND no remaining facts (so an edge-less extracted entity is
    reached too, migration 0069). This is the ONLY sanctioned delete path: it
    opts in via the ``app.kg_allow_erase`` GUC so the immutability triggers
    (migration 0068) permit the deletion (a casual app DELETE is still refused).
    Returns the number of facts erased. The caller owns the authorization."""
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(text("SELECT set_config('app.kg_allow_erase', 'on', true)"))
    try:
        rows = (
            await session.execute(
                select(KgEdge.subject_id, KgEdge.object_id).where(
                    KgEdge.org_id == org_id, KgEdge.source_note_id == source_note_id
                )
            )
        ).all()
        erased = len(rows)
        candidate_ids: set[uuid.UUID] = set()
        for sid, oid in rows:
            candidate_ids.update((sid, oid))
        # Entities this note is a provenance of -- candidates for pruning even
        # if they never had an edge (extracted into entities[] only).
        candidate_ids.update(
            (
                await session.execute(
                    select(KgEntitySource.entity_id).where(
                        KgEntitySource.org_id == org_id,
                        KgEntitySource.source_note_id == source_note_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.execute(
            delete(KgEdge).where(KgEdge.org_id == org_id, KgEdge.source_note_id == source_note_id)
        )
        await session.execute(
            delete(KgEntitySource).where(
                KgEntitySource.org_id == org_id,
                KgEntitySource.source_note_id == source_note_id,
            )
        )
        await session.flush()
        pruned = 0
        for eid in candidate_ids:
            edges = (
                await session.execute(
                    select(func.count())
                    .select_from(KgEdge)
                    .where(
                        KgEdge.org_id == org_id,
                        or_(KgEdge.subject_id == eid, KgEdge.object_id == eid),
                    )
                )
            ).scalar_one()
            sources = (
                await session.execute(
                    select(func.count())
                    .select_from(KgEntitySource)
                    .where(KgEntitySource.org_id == org_id, KgEntitySource.entity_id == eid)
                )
            ).scalar_one()
            # Delete only when the entity has no surviving facts AND no remaining
            # provenance -- an entity still referenced by another note's facts or
            # listed as that note's provenance is correctly retained.
            if edges == 0 and sources == 0:
                await session.execute(
                    delete(KgEntity).where(KgEntity.org_id == org_id, KgEntity.id == eid)
                )
                pruned += 1
        await session.flush()
    finally:
        # Re-arm the immutability guard for the rest of the transaction.
        await session.execute(text("SELECT set_config('app.kg_allow_erase', '', true)"))
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=source_note_id,
        action="kg_erase_by_source",
        diff={"facts": str(erased), "entities": str(pruned)},
    )
    return erased


def _effective_clauses(
    as_of: datetime.datetime | None,
    tx_as_of: datetime.datetime | None = None,
) -> list[Any]:
    """Bi-temporal effective predicate.

    valid-time (``as_of``, the world): the half-open window ``valid_from <= t <
    valid_to`` slices the fact true at instant ``t`` (default now).

    transaction-time (``tx_as_of``, the system's belief): by default only the
    CURRENTLY-believed fact is read (``invalidated_at IS NULL``). With
    ``tx_as_of`` set, reconstruct what the system believed THEN -- a fact
    asserted by ``tx_as_of`` and not yet invalidated as of ``tx_as_of``
    (``created_at <= tx_as_of AND (invalidated_at IS NULL OR invalidated_at >
    tx_as_of)``). NOTE: ``review_state`` is a current-state column (not
    transaction-time-tracked), so a currently-proposed fact stays excluded on
    every axis."""
    t = as_of or _now()
    if tx_as_of is None:
        tx_clause: Any = KgEdge.invalidated_at.is_(None)
    else:
        tx_clause = (KgEdge.created_at <= tx_as_of) & or_(
            KgEdge.invalidated_at.is_(None), KgEdge.invalidated_at > tx_as_of
        )
    return [
        tx_clause,
        KgEdge.review_state.is_distinct_from("proposed"),
        or_(KgEdge.valid_from.is_(None), KgEdge.valid_from <= t),
        or_(KgEdge.valid_to.is_(None), KgEdge.valid_to > t),
    ]


async def search_entities(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[KgEntity]:
    """Entities whose normalized name contains ``query`` (RLS-scoped)."""
    await require_role(session, org_id, actor_id, Role.member)
    norm = normalize_entity_name(query)
    stmt = select(KgEntity).where(
        KgEntity.org_id == org_id,
        KgEntity.normalized_name.like(f"%{norm}%"),
    )
    if entity_type:
        stmt = stmt.where(KgEntity.entity_type == _coerce_entity_type(entity_type))
    stmt = stmt.order_by(KgEntity.normalized_name).limit(max(1, min(limit, 100)))
    return list((await session.execute(stmt)).scalars().all())


def _fact_views(rows: Any) -> list[FactView]:
    out: list[FactView] = []
    for edge, subject_name, object_name in rows:
        out.append(
            FactView(
                edge_id=edge.id,
                subject_id=edge.subject_id,
                subject_name=subject_name,
                predicate=edge.predicate,
                object_id=edge.object_id,
                object_name=object_name,
                valid_from=edge.valid_from,
                valid_to=edge.valid_to,
                confidence=edge.confidence,
                review_state=edge.review_state,
            )
        )
    return out


def _facts_stmt(  # type: ignore[no-untyped-def]
    org_id: uuid.UUID,
    as_of: datetime.datetime | None,
    tx_as_of: datetime.datetime | None = None,
):
    subj = aliased(KgEntity)
    obj = aliased(KgEntity)
    return (
        select(KgEdge, subj.name, obj.name)
        .join(subj, subj.id == KgEdge.subject_id)
        .join(obj, obj.id == KgEdge.object_id)
        .where(KgEdge.org_id == org_id, *_effective_clauses(as_of, tx_as_of))
        # Deterministic order so a LIMIT is a stable cut, never an arbitrary one.
        .order_by(KgEdge.created_at, KgEdge.id)
    )


async def entity_facts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    as_of: datetime.datetime | None = None,
    tx_as_of: datetime.datetime | None = None,
    limit: int = 50,
) -> list[FactView]:
    """Effective facts where ``entity_id`` is the subject or the object,
    optionally as-of a valid-time (``as_of``) and/or transaction-time
    (``tx_as_of``) instant."""
    await require_role(session, org_id, actor_id, Role.member)
    stmt = (
        _facts_stmt(org_id, as_of, tx_as_of)
        .where(or_(KgEdge.subject_id == entity_id, KgEdge.object_id == entity_id))
        .limit(max(1, min(limit, 200)))
    )
    return _fact_views((await session.execute(stmt)).all())


async def traverse(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    seed_id: uuid.UUID,
    depth: int = 2,
    as_of: datetime.datetime | None = None,
    tx_as_of: datetime.datetime | None = None,
    max_edges: int = 200,
) -> list[FactView]:
    """Breadth-first multi-hop walk from ``seed_id`` over effective (as-of)
    facts, up to ``depth`` hops. Answers cross-entity questions ('which
    projects did X and Y share') natively over the typed graph.

    Each hop fetches only edges NOT already collected (``id NOT IN seen``) in a
    deterministic order, so the ``max_edges`` budget is never wasted re-reading
    seen edges -- a high-degree seed can no longer starve the deeper hops. If
    the budget is exhausted the walk stops and logs (no silent truncation)."""
    await require_role(session, org_id, actor_id, Role.member)
    seen_entities: set[uuid.UUID] = {seed_id}
    frontier: set[uuid.UUID] = {seed_id}
    seen_edges: set[uuid.UUID] = set()
    out: list[FactView] = []
    truncated = False
    for _ in range(max(1, depth)):
        if not frontier or len(out) >= max_edges:
            truncated = truncated or bool(frontier)
            break
        stmt = (
            _facts_stmt(org_id, as_of, tx_as_of)
            .where(or_(KgEdge.subject_id.in_(frontier), KgEdge.object_id.in_(frontier)))
            .where(KgEdge.id.notin_(seen_edges) if seen_edges else text("true"))
            .limit(max_edges - len(out))
        )
        next_frontier: set[uuid.UUID] = set()
        for view in _fact_views((await session.execute(stmt)).all()):
            if view.edge_id in seen_edges:
                continue
            seen_edges.add(view.edge_id)
            out.append(view)
            for nid in (view.subject_id, view.object_id):
                if nid not in seen_entities:
                    seen_entities.add(nid)
                    next_frontier.add(nid)
        frontier = next_frontier
    if truncated or len(out) >= max_edges:
        _log.warning(
            "kg.traverse hit max_edges=%d from seed=%s (org=%s); result truncated",
            max_edges,
            seed_id,
            org_id,
        )
    return out[:max_edges]
