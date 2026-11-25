"""Temporal knowledge graph service (ADR-0044, Track B).

Builds and queries a typed, bi-temporal knowledge graph over the org's notes:

- ``extract_facts`` runs the per-org METERED LLM (resolve_llm, op='extract')
  over a note body, parses (entity, relation, entity) triples defensively,
  resolves/dedupes entities, and writes facts -- always born ``review_state=
  'proposed'`` (ADR-0043, ADR-0071), effective only once a person approves.
- ``ensure_entity`` is the idempotent get-or-create resolver (one canonical
  node per (org, type, normalized name), the identities.ensure_* race pattern).
- ``add_fact`` is idempotent on the current triple; ``supersede_fact`` closes
  a fact's valid window and links the replacement (a temporal update keeps the
  old fact as-of-queryable); ``invalidate_fact`` tombstones a wrong fact
  (invalidate-not-delete; a DB trigger freezes it thereafter).
- ``entity_facts`` / ``traverse`` answer 1-hop and multi-hop questions with an
  optional ``as_of`` valid-time clamp; effective facts only.

Effective + as-of predicate: ``invalidated_at IS NULL AND review_state NOT
IN ('proposed', 'rejected') AND (valid_from IS NULL OR valid_from <= t) AND
(valid_to IS NULL OR valid_to > t)`` where t = ``as_of`` or now.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import bindparam, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.kg import (
    KG_ENTITY_TYPES,
    KgEdge,
    KgEntity,
    KgEntitySource,
    KgNoteExtraction,
)
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.identities import ensure_for_user
from mycelium_core.services.llm_resolver import MeteredLLM, resolve_llm
from mycelium_core.services.note_effective import GATED_STATES
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


_PROPOSED = "proposed"
_APPROVED = "approved"
_REJECTED = "rejected"


# Every fact a model reads out of a note is born 'proposed', withheld from every
# read until a person approves it, whoever asked for the reading: the
# amendment to ADR-0043 says nothing a model writes about the notes takes
# effect before approval, and asking for an extraction is not vouching for
# what it finds (ADR-0071, decided 2026-11-17). It used to follow
# ``garden_review_gate_enabled`` for the autonomous pass, which defaults to off,
# and to be effective for ``kg_extract``.
_MODEL_REVIEW_STATE = _PROPOSED


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
    the whole string, then the first ``{...}`` block; never raise. An answer
    with no JSON object in it parses to ``{}``, not to empty lists: it used to
    return ``{"entities": [], "relations": []}``, which made an unreadable
    answer indistinguishable from a note that states no fact."""
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
    return {}


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


@dataclass(frozen=True)
class _Reading:
    """What a model answered about one note, before anything is written."""

    model_id: str
    parsed: dict[str, Any]
    # False when the answer held no JSON object with an entities or
    # relations list: an empty graph and an unreadable answer are different
    # outcomes, and only the first one is a successful read.
    understood: bool


async def _read_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    llm: LLMProvider | None,
    attempt: int | None,
) -> _Reading:
    """Call the metered model on a note's body. Writes nothing but the
    metering, so a caller can keep the debit of an answer it then fails to
    use. ``attempt`` is set by the unattended pass only: it checks the wallet
    BEFORE the call (an unattended loop must not pay a provider for a debit
    that is then refused), and it enters the metering key, so a retry of the
    same text is a new call and a new charge rather than a replay of the
    first one's debit."""
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
    # The metering key names the TEXT read, not just the note: keyed on the
    # note alone, a re-read after an edit replayed the first debit and was
    # never charged, while a retry of the same request must not be charged
    # twice.
    body_key = hashlib.sha256(body.encode()).hexdigest()[:16]
    op_key = f"kg_extract:{org_id}:{note_id}:{body_key}"
    if attempt is not None:
        op_key += f":auto{attempt}"
    provider = llm or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=op_key,
        op="extract",
    )
    if attempt is not None and isinstance(provider, MeteredLLM):
        estimate = estimate_read_tokens(len(body.encode()))
        await provider.ensure_affordable(
            tokens_in=estimate - _output_allowance(estimate),
            tokens_out=_output_allowance(estimate),
        )
    res = await provider.complete(system=_EXTRACT_SYSTEM, messages=[("user", body)])
    parsed = _parse_extraction(res.text)
    understood = isinstance(parsed.get("entities"), list) or isinstance(
        parsed.get("relations"), list
    )
    return _Reading(model_id=res.model_id, parsed=parsed, understood=understood)


async def _write_reading(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    reading: _Reading,
    autonomous: bool,
) -> ExtractionResult:
    """Persist what a reading states as entities and facts."""
    parsed = reading.parsed
    identity_id = await _actor_identity_id(session, org_id, actor_id)

    # Cache mirrors the DB dedupe key (type, normalized_name) so two same-name
    # entities of DIFFERENT types (e.g. "Apple" the org vs the product) stay
    # distinct instead of the second silently overwriting the first. A second
    # map (normalized_name -> first typed entity) lets an untyped relation
    # endpoint reuse a typed entity from the entities[] pass rather than minting
    # a spurious 'other' duplicate.
    by_key: dict[tuple[str, str], KgEntity] = {}
    by_name: dict[str, KgEntity] = {}
    # The types the reading declared, by normalized name. A reading creates an
    # entity only when a relation uses it, typed from here: an entity is
    # visible to every search as soon as it exists, and one that no proposed
    # fact explains would be a model's output in effect before anyone
    # approved it.
    declared: dict[str, Any] = {}

    async def _entity(name: str, entity_type: Any) -> KgEntity:
        norm = normalize_entity_name(name)
        etype = _coerce_entity_type(entity_type)
        if etype == "other" and norm in by_name:
            return by_name[norm]
        if etype == "other" and norm in declared:
            etype = _coerce_entity_type(declared[norm])
        key = (etype, norm)
        if key in by_key:
            return by_key[key]
        ent = await ensure_entity(
            session,
            org_id=org_id,
            name=name,
            entity_type=etype,
            origin_model_id=reading.model_id,
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
                declared.setdefault(normalize_entity_name(spec["name"]), spec.get("type"))

    edge_ids: list[uuid.UUID] = []
    entity_ids: set[uuid.UUID] = set()
    dropped = 0
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
            valid_from = _parse_dt(rel.get("valid_from"))
            valid_to = _parse_dt(rel.get("valid_to"))
            if valid_from is not None and valid_to is not None and valid_to <= valid_from:
                # A window the model got backwards is one bad relation, not a
                # bad reading: dropping it keeps every other fact of the note.
                # It used to raise from add_fact and take them all down.
                dropped += 1
                continue
            subj = await _entity(subj_name, "other")
            obj = await _entity(obj_name, "other")
            entity_ids.update((subj.id, obj.id))
            if subj.id == obj.id:  # the DB CHECK forbids a self-edge
                continue
            if autonomous and await _rejected_before(
                session, org_id=org_id, subject_id=subj.id, predicate=pred, object_id=obj.id
            ):
                # A person already turned this triple down, or retracted it.
                # Proposing it again on every re-read would ask the same
                # question until they gave in. A reading a person asks for
                # with kg_extract does propose it again: that is them asking
                # the question anew, and the only way back for a triple
                # rejected by mistake.
                continue
            edge = await add_fact(
                session,
                org_id=org_id,
                subject_id=subj.id,
                predicate=pred,
                object_id=obj.id,
                valid_from=valid_from,
                valid_to=valid_to,
                confidence=_parse_confidence(rel.get("confidence")),
                source_note_id=note_id,
                origin_model_id=reading.model_id,
                review_state=_MODEL_REVIEW_STATE,
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
        diff={
            "entities": len(entity_ids),
            "facts": len(edge_ids),
            "dropped": dropped,
            "model_id": reading.model_id,
            "autonomous": autonomous,
        },
    )
    return ExtractionResult(
        entities=len(entity_ids),
        facts=len(edge_ids),
        edge_ids=edge_ids,
        model_id=reading.model_id,
    )


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
    metered LLM and persist entities + facts. Facts are born 'proposed'
    whoever asked, and only entities a fact uses are created. An answer with no JSON in it yields an
    empty extraction here; the unattended pass, which must tell the two
    apart, calls :func:`read_autonomously` instead. ``llm`` is an explicit
    test/override injection (decomposition pattern)."""
    reading = await _read_note(
        session, org_id=org_id, actor_id=actor_id, note_id=note_id, llm=llm, attempt=None
    )
    return await _write_reading(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        reading=reading,
        autonomous=autonomous,
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
    # Identity before the assignments, as in reject_fact: a flush between
    # them would freeze the row half-written.
    invalidated_by = await _actor_identity_id(session, org_id, actor_id)
    edge.invalidated_at = now or _now()
    edge.invalidated_by = invalidated_by
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


async def _load_reviewable(
    session: AsyncSession, *, org_id: uuid.UUID, edge_id: uuid.UUID
) -> KgEdge:
    """A fact that is, or was, up for review: proposed, approved or rejected.
    A fact that never went through review (asserted by a person) is absent
    here, not forbidden: it is not a proposal, so there is nothing to decide."""
    edge = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.id == edge_id,
                KgEdge.review_state.in_((_PROPOSED, _APPROVED, _REJECTED)),
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise NotFoundError(MessageCode.KG_FACT_NOT_FOUND)
    return edge


async def approve_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    edge_id: uuid.UUID,
    expected_version: int | None = None,
) -> KgEdge:
    """Approve a proposed fact: it becomes effective (``review_state=
    'approved'``, which is DISTINCT FROM 'proposed'). ADR-0043 for facts.

    ``expected_version`` is the version the reviewer read; a fact that changed
    since fails with ``stale_version``, checked before the idempotent
    short-circuit so a stale view is an error even when the end state would
    coincide. Approving an approved fact is a no-op; a rejected one is no
    longer a proposal and is absent."""
    await require_role(session, org_id, actor_id, Role.member)
    edge = await _load_reviewable(session, org_id=org_id, edge_id=edge_id)
    if expected_version is not None and edge.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    if edge.review_state == _APPROVED:
        return edge
    if edge.review_state != _PROPOSED or edge.invalidated_at is not None:
        raise NotFoundError(MessageCode.KG_FACT_NOT_FOUND)
    edge.review_state = _APPROVED
    edge.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="kg_edge",
        entity_id=edge_id,
        action="kg_approve",
        diff={"review_state": {"old": _PROPOSED, "new": _APPROVED}},
    )
    return edge


async def withdraw_fact_approval(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    edge_id: uuid.UUID,
) -> KgEdge:
    """Take an approval back: an approved fact goes back to ``proposed``,
    out of every read and back in the review inbox (ADR-0075 decision 4).
    A rejected fact cannot come back this way: rejecting invalidated it, and
    the database freezes an invalidated row."""
    await require_role(session, org_id, actor_id, Role.member)
    edge = await _load_reviewable(session, org_id=org_id, edge_id=edge_id)
    if edge.review_state != _APPROVED or edge.invalidated_at is not None:
        raise NotFoundError(MessageCode.KG_FACT_NOT_FOUND)
    edge.review_state = _PROPOSED
    edge.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="kg_edge",
        entity_id=edge_id,
        action="kg_withdraw_approval",
        diff={"review_state": {"old": _APPROVED, "new": _PROPOSED}},
    )
    return edge


async def reject_fact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    edge_id: uuid.UUID,
    expected_version: int | None = None,
) -> KgEdge:
    """Reject a proposed fact: ``review_state='rejected'`` and invalidated in
    the same write (invalidate-not-delete), so it leaves every read and the
    row is frozen from then on by the DB trigger. The rejection is remembered
    per triple: the autonomous pass does not propose it again.

    Only a PROPOSED fact can be rejected here. This used to delegate to
    ``invalidate_fact`` unconditionally, so "reject" could tombstone an
    effective fact a person had asserted. Rejecting a rejected fact is a
    no-op; ``expected_version`` as for :func:`approve_fact`."""
    await require_role(session, org_id, actor_id, Role.member)
    edge = await _load_reviewable(session, org_id=org_id, edge_id=edge_id)
    if expected_version is not None and edge.version != expected_version:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
    if edge.review_state == _REJECTED:
        return edge
    if edge.review_state != _PROPOSED or edge.invalidated_at is not None:
        raise NotFoundError(MessageCode.KG_FACT_NOT_FOUND)
    edge.review_state = _REJECTED
    # The identity first: resolving it may flush, and a flush between the two
    # assignments would write invalidated_at alone, after which the trigger
    # refuses the row's next UPDATE (measured by the review of 2026-11-16).
    invalidated_by = await _actor_identity_id(session, org_id, actor_id)
    edge.invalidated_at = _now()
    edge.invalidated_by = invalidated_by
    edge.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="kg_edge",
        entity_id=edge_id,
        action="kg_reject",
        diff={"review_state": {"old": _PROPOSED, "new": _REJECTED}},
    )
    return edge


async def _rejected_before(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    subject_id: uuid.UUID,
    predicate: str,
    object_id: uuid.UUID,
) -> bool:
    return (
        await session.execute(
            select(KgEdge.id)
            .where(
                KgEdge.org_id == org_id,
                KgEdge.subject_id == subject_id,
                KgEdge.predicate == normalize_predicate(predicate),
                KgEdge.object_id == object_id,
                # Rejected by a person, or retracted as wrong
                # (invalidate_fact). A superseded fact is not here: it was
                # true for its window, so it does not set invalidated_at.
                or_(KgEdge.review_state == _REJECTED, KgEdge.invalidated_at.is_not(None)),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


@dataclass(frozen=True)
class ProposedFact:
    edge_id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    subject_type: str
    predicate: str
    object_id: uuid.UUID
    object_name: str
    object_type: str
    valid_from: datetime.datetime | None
    valid_to: datetime.datetime | None
    confidence: Decimal | None
    origin_model_id: str | None
    source_note_id: uuid.UUID | None
    source_note_title: str | None
    created_at: datetime.datetime
    version: int


async def list_proposed_facts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    limit: int,
    after: tuple[datetime.datetime, uuid.UUID] | None = None,
) -> list[ProposedFact]:
    """Facts awaiting a person's decision, newest first, keyset-paged on
    ``(created_at, id)``. Fetch ``limit + 1`` to learn whether a next page
    exists. A proposed fact appears here and nowhere else: every other read
    withholds it."""
    from mycelium_core.models.note import Note

    await require_role(session, org_id, actor_id, Role.member)
    subj = aliased(KgEntity)
    obj = aliased(KgEntity)
    stmt = (
        select(KgEdge, subj, obj, Note.title)
        .join(subj, subj.id == KgEdge.subject_id)
        .join(obj, obj.id == KgEdge.object_id)
        .outerjoin(Note, Note.id == KgEdge.source_note_id)
        .where(
            KgEdge.org_id == org_id,
            KgEdge.review_state == _PROPOSED,
            KgEdge.invalidated_at.is_(None),
        )
    )
    if after is not None:
        created, row_id = after
        stmt = stmt.where(
            or_(
                KgEdge.created_at < created,
                (KgEdge.created_at == created) & (KgEdge.id < row_id),
            )
        )
    stmt = stmt.order_by(KgEdge.created_at.desc(), KgEdge.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        ProposedFact(
            edge_id=e.id,
            subject_id=s_.id,
            subject_name=s_.name,
            subject_type=s_.entity_type,
            predicate=e.predicate,
            object_id=o_.id,
            object_name=o_.name,
            object_type=o_.entity_type,
            valid_from=e.valid_from,
            valid_to=e.valid_to,
            confidence=e.confidence,
            origin_model_id=e.origin_model_id,
            source_note_id=e.source_note_id,
            source_note_title=title,
            created_at=e.created_at,
            version=e.version,
        )
        for e, s_, o_, title in rows
    ]


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
    every axis, and so does a rejected one: it was a proposal until a person
    turned it down and was never believed, while its ``invalidated_at`` alone
    would let ``tx_as_of`` before the rejection read it as a belief."""
    t = as_of or _now()
    if tx_as_of is None:
        tx_clause: Any = KgEdge.invalidated_at.is_(None)
    else:
        tx_clause = (KgEdge.created_at <= tx_as_of) & or_(
            KgEdge.invalidated_at.is_(None), KgEdge.invalidated_at > tx_as_of
        )
    return [
        tx_clause,
        or_(KgEdge.review_state.is_(None), KgEdge.review_state.notin_((_PROPOSED, _REJECTED))),
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
    """Entities whose normalized name contains ``query`` (RLS-scoped).

    An entity shows when at least one effective fact touches it, or when no
    fact touches it at all (one made directly with ``ensure_entity``, never
    by a reading). One whose every fact is still proposed, or was rejected,
    stays hidden: a reading creates entities only for the facts it proposes,
    and listing them would put a model's reading in effect before a person
    approved any of it."""
    await require_role(session, org_id, actor_id, Role.member)
    norm = normalize_entity_name(query)
    touches = or_(KgEdge.subject_id == KgEntity.id, KgEdge.object_id == KgEntity.id)
    # Believed and decided, whatever its validity window: an entity whose
    # facts all lie in the past is still a real entity.
    effective_fact = (
        select(KgEdge.id).where(
            KgEdge.org_id == org_id,
            touches,
            KgEdge.invalidated_at.is_(None),
            or_(KgEdge.review_state.is_(None), KgEdge.review_state.notin_((_PROPOSED, _REJECTED))),
        )
    ).exists()
    any_fact = select(KgEdge.id).where(KgEdge.org_id == org_id, touches).exists()
    stmt = select(KgEntity).where(
        KgEntity.org_id == org_id,
        KgEntity.normalized_name.like(f"%{norm}%"),
        or_(effective_fact, ~any_fact),
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


# --------------------------------------------------------------------------
# Autonomous extraction (ADR-0071): what feeds the graph without being asked
# --------------------------------------------------------------------------


class ExtractionOutcome(enum.StrEnum):
    """The closed set ``kg_note_extraction.outcome`` holds (DB CHECK)."""

    OK = "ok"
    FAILED = "failed"
    TOO_LARGE = "too_large"


#: Output allowance per read, as a fraction of the input estimate plus a
#: floor. NOT measured: no autonomous pass has run yet to measure it on. It
#: only sizes how many notes one pass attempts; what a read really costs is
#: metered from the provider's own count and bounded by the workspace's
#: daily credit cap (``autonomous_budget``), which this estimate never
#: replaces. Revisit against the usage records of the first passes.
_OUTPUT_FRACTION = 0.5
_OUTPUT_FLOOR_TOKENS = 256


def estimate_read_tokens(body_bytes: int) -> int:
    """What reading a note of ``body_bytes`` costs in tokens, input and
    output: the system prompt and the body at the byte-per-token ratio
    measured for billing (Italian prose and JSON), plus the output
    allowance above."""
    from mycelium_core.services.billing import BYTES_PER_TOKEN

    input_tokens = math.ceil((len(_EXTRACT_SYSTEM.encode()) + body_bytes) / BYTES_PER_TOKEN)
    return input_tokens + math.ceil(input_tokens * _OUTPUT_FRACTION) + _OUTPUT_FLOOR_TOKENS


def _output_allowance(estimate: int) -> int:
    """The output share of an :func:`estimate_read_tokens` figure."""
    input_tokens = math.floor((estimate - _OUTPUT_FLOOR_TOKENS) / (1 + _OUTPUT_FRACTION))
    return estimate - input_tokens


@dataclass(frozen=True)
class PendingRead:
    note_id: uuid.UUID
    parts_signature: str
    estimated_tokens: int
    attempts: int  # attempts already recorded for THIS signature (0 = never)


# A note qualifies when a person could have written it and wants it seen:
# live, not archived, not held at the review gate (pending or declined, the
# set ``note_effective.GATED_STATES`` defines, spelled into the SQL below from
# that tuple as a bound list rather than copied by hand), not opted out of
# automatic indexing (ADR-0056: index_scope='none' keeps a note away from
# every automatic reader, and a model reading it is one), not still being
# transcribed, and left alone for ``settle`` so a note being typed is not
# paid for once per pause. The signature hashes the parts' text in order:
# measured on 2026-11-16, ``update_note`` rewrites part 0's body in place
# without bumping ``note_part.version``, so a signature over id, version and
# order (the first version of this query) missed every ordinary edit. The
# hash is computed in the database; nothing leaves it.
_PENDING_SQL = text(
    """
    WITH sig AS (
        SELECT p.note_id,
               md5(string_agg(p.ord || ':' || md5(p.body), ','
                              ORDER BY p.ord, p.id)) AS signature,
               max(p.updated_at) AS last_edit,
               sum(octet_length(p.body)) AS body_bytes,
               bool_or(p.body ~ '[^[:space:]]') AS has_text
          FROM note_part p
         WHERE p.org_id = :org_id
         GROUP BY p.note_id
    )
    SELECT n.id AS note_id, sig.signature, sig.body_bytes,
           CASE WHEN x.parts_signature = sig.signature THEN x.attempts ELSE 0 END AS attempts
      FROM notes n
      JOIN sig ON sig.note_id = n.id
      LEFT JOIN kg_note_extraction x ON x.org_id = n.org_id AND x.note_id = n.id
     WHERE n.org_id = :org_id
       AND n.deleted_at IS NULL
       AND NOT n.is_archived
       AND n.index_scope = 'org'
       AND n.status <> 'transcribing'
       AND COALESCE(n.review_state, '') NOT IN :gated
       AND sig.has_text
       AND sig.last_edit < :settled_before
       AND (x.id IS NULL
            OR x.parts_signature <> sig.signature
            OR (x.outcome = 'failed' AND x.attempts < :max_attempts)
            OR (x.outcome = 'too_large' AND x.estimated_tokens <= :token_budget))
     ORDER BY sig.last_edit ASC, n.id ASC
     LIMIT :scan
    """
).bindparams(bindparam("gated", expanding=True))


async def pending_reads(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    settled_before: datetime.datetime,
    max_attempts: int,
    token_budget: int,
    scan: int,
) -> list[PendingRead]:
    """Notes the autonomous pass has not read in their current form, the
    longest-settled first, so a large note waiting for budget is never
    overtaken by a stream of fresh edits. A note set aside as too large
    comes back once ``token_budget`` can hold it, as well as after an edit."""
    rows = (
        await session.execute(
            _PENDING_SQL,
            {
                "org_id": org_id,
                "gated": list(GATED_STATES),
                "settled_before": settled_before,
                "max_attempts": max_attempts,
                "token_budget": token_budget,
                "scan": scan,
            },
        )
    ).all()
    return [
        PendingRead(
            note_id=r.note_id,
            parts_signature=r.signature,
            estimated_tokens=estimate_read_tokens(int(r.body_bytes)),
            attempts=int(r.attempts),
        )
        for r in rows
    ]


@dataclass(frozen=True)
class PassPlan:
    read: list[PendingRead]
    too_large: list[PendingRead]


def plan_pass(pending: list[PendingRead], *, token_budget: int) -> PassPlan:
    """Which pending notes one pass reads, charging each its own estimate.

    In order, and stopping at the first note that does not fit what is left:
    skipping it for smaller ones behind it would let a steady trickle of
    small notes starve it forever. A note that alone exceeds the whole budget
    can never fit, so it is set aside as ``too_large`` (recorded, visible,
    re-tried only when it changes) instead of blocking every note behind it."""
    read: list[PendingRead] = []
    too_large: list[PendingRead] = []
    left = token_budget
    for p in pending:
        if p.estimated_tokens > token_budget:
            too_large.append(p)
            continue
        if p.estimated_tokens > left:
            break
        read.append(p)
        left -= p.estimated_tokens
    return PassPlan(read=read, too_large=too_large)


async def _record_read(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pending: PendingRead,
    outcome: ExtractionOutcome,
    model_id: str | None,
    facts: int,
) -> None:
    stmt = pg_insert(KgNoteExtraction).values(
        org_id=org_id,
        note_id=pending.note_id,
        parts_signature=pending.parts_signature,
        outcome=outcome.value,
        attempts=pending.attempts + 1,
        model_id=model_id,
        facts=facts,
        estimated_tokens=pending.estimated_tokens,
        attempted_at=_now(),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["org_id", "note_id"],
            set_={
                "parts_signature": stmt.excluded.parts_signature,
                "outcome": stmt.excluded.outcome,
                "attempts": stmt.excluded.attempts,
                "model_id": stmt.excluded.model_id,
                "facts": stmt.excluded.facts,
                "estimated_tokens": stmt.excluded.estimated_tokens,
                "attempted_at": stmt.excluded.attempted_at,
            },
        )
    )


async def record_too_large(
    session: AsyncSession, *, org_id: uuid.UUID, pending: PendingRead
) -> None:
    """Set a note aside without reading it (see :func:`plan_pass`)."""
    await _record_read(
        session,
        org_id=org_id,
        pending=pending,
        outcome=ExtractionOutcome.TOO_LARGE,
        model_id=None,
        facts=0,
    )


async def _taken_elsewhere(
    session: AsyncSession, *, org_id: uuid.UUID, pending: PendingRead
) -> bool:
    """Hold this note's read for the rest of the transaction, or report that
    another pass holds it or has already done it.

    Nothing guarantees one worker process: two passes over the same
    workspace would each pay a provider for the same note (measured by the
    review of 2026-11-16: two concurrent reads, two provider calls, one
    charge). The lock is transaction-scoped, and each note is read in its own
    transaction, so it covers exactly the call and its writes. The ledger is
    re-read after the lock, because the other pass may have finished and
    released it between our listing and our turn."""
    got = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"kg_read:{pending.note_id}"},
        )
    ).scalar_one()
    if not got:
        return True
    done = (
        await session.execute(
            select(KgNoteExtraction.id).where(
                KgNoteExtraction.org_id == org_id,
                KgNoteExtraction.note_id == pending.note_id,
                KgNoteExtraction.parts_signature == pending.parts_signature,
                KgNoteExtraction.attempts > pending.attempts,
            )
        )
    ).scalar_one_or_none()
    return done is not None


async def read_autonomously(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    pending: PendingRead,
    llm: LLMProvider | None = None,
) -> ExtractionOutcome | None:
    """Read one note on nobody's request and write what it states as
    PROPOSED facts, then record the read. Returns the outcome, or None when
    another pass holds or has done this read.

    Two savepoints, because the call and the writes fail differently. The
    model call carries the debit: if it fails, nothing was charged, and the
    note is recorded ``failed``. The writes come after the debit: if they
    fail, they roll back alone and the debit stays, because the provider was
    paid; a single savepoint around both, the first version of this, rolled
    the debit back with them and every retry was free. An answer with no JSON
    in it is ``failed`` too, charged, and retried under a new metering key.

    Running out of credits is not a failure of the note: it is re-raised, so
    the caller stops the pass, and it is normally raised before the call by
    the wallet check in :func:`_read_note`."""
    if await _taken_elsewhere(session, org_id=org_id, pending=pending):
        return None

    async def _failed(model_id: str | None) -> ExtractionOutcome:
        await _record_read(
            session,
            org_id=org_id,
            pending=pending,
            outcome=ExtractionOutcome.FAILED,
            model_id=model_id,
            facts=0,
        )
        return ExtractionOutcome.FAILED

    try:
        async with session.begin_nested():
            reading = await _read_note(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=pending.note_id,
                llm=llm,
                attempt=pending.attempts + 1,
            )
    except DomainError as exc:
        if exc.code == MessageCode.INSUFFICIENT_CREDITS:
            raise
        _log.warning(
            "kg autonomous read failed org=%s note=%s code=%s", org_id, pending.note_id, exc.code
        )
        return await _failed(None)
    except Exception:
        _log.exception("kg autonomous read failed org=%s note=%s", org_id, pending.note_id)
        return await _failed(None)
    if not reading.understood:
        _log.warning(
            "kg autonomous read unreadable org=%s note=%s model=%s",
            org_id,
            pending.note_id,
            reading.model_id,
        )
        return await _failed(reading.model_id)
    try:
        async with session.begin_nested():
            res = await _write_reading(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=pending.note_id,
                reading=reading,
                autonomous=True,
            )
    except Exception:
        _log.exception("kg autonomous write failed org=%s note=%s", org_id, pending.note_id)
        return await _failed(reading.model_id)
    await _record_read(
        session,
        org_id=org_id,
        pending=pending,
        outcome=ExtractionOutcome.OK,
        model_id=res.model_id,
        facts=res.facts,
    )
    return ExtractionOutcome.OK
