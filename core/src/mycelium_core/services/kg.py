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
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.kg import KG_ENTITY_TYPES, KgEdge, KgEntity
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.identities import ensure_for_user
from mycelium_core.services.llm_resolver import resolve_llm
from mycelium_core.services.rbac import require_role

_WS_RE = re.compile(r"\s+")
_PRED_RE = re.compile(r"[^a-z0-9]+")
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

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
    """Dedupe key for an entity: trimmed, whitespace-collapsed, casefolded."""
    return _WS_RE.sub(" ", name.strip()).casefold()


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
    """Insert a fact, idempotent on the CURRENT triple (org, subject,
    predicate, object). Role-gating is the caller's responsibility."""
    pred = normalize_predicate(predicate)
    existing = (
        await session.execute(
            select(KgEdge).where(
                KgEdge.org_id == org_id,
                KgEdge.subject_id == subject_id,
                KgEdge.predicate == pred,
                KgEdge.object_id == object_id,
                KgEdge.invalidated_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
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
        edge = (
            await session.execute(
                select(KgEdge).where(
                    KgEdge.org_id == org_id,
                    KgEdge.subject_id == subject_id,
                    KgEdge.predicate == pred,
                    KgEdge.object_id == object_id,
                    KgEdge.invalidated_at.is_(None),
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

    by_norm: dict[str, KgEntity] = {}

    async def _entity(name: str, entity_type: Any) -> KgEntity:
        key = normalize_entity_name(name)
        if key in by_norm:
            return by_norm[key]
        ent = await ensure_entity(
            session,
            org_id=org_id,
            name=name,
            entity_type=entity_type,
            origin_model_id=res.model_id,
            created_by=identity_id,
        )
        by_norm[key] = ent
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

    entity_ids.update(e.id for e in by_norm.values())
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


def _effective_clauses(as_of: datetime.datetime | None) -> list[Any]:
    """Effective + as-of-valid-time predicate. Invalidated (retracted) facts
    are excluded on both axes; the valid window slices to time ``t``."""
    t = as_of or _now()
    return [
        KgEdge.invalidated_at.is_(None),
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


def _facts_stmt(org_id: uuid.UUID, as_of: datetime.datetime | None):  # type: ignore[no-untyped-def]
    subj = aliased(KgEntity)
    obj = aliased(KgEntity)
    return (
        select(KgEdge, subj.name, obj.name)
        .join(subj, subj.id == KgEdge.subject_id)
        .join(obj, obj.id == KgEdge.object_id)
        .where(KgEdge.org_id == org_id, *_effective_clauses(as_of))
    )


async def entity_facts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID,
    as_of: datetime.datetime | None = None,
    limit: int = 50,
) -> list[FactView]:
    """Effective facts where ``entity_id`` is the subject or the object,
    optionally as-of a valid-time instant."""
    await require_role(session, org_id, actor_id, Role.member)
    stmt = (
        _facts_stmt(org_id, as_of)
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
    max_edges: int = 200,
) -> list[FactView]:
    """Breadth-first multi-hop walk from ``seed_id`` over effective (as-of)
    facts, up to ``depth`` hops. Answers cross-entity questions ('which
    projects did X and Y share') natively over the typed graph."""
    await require_role(session, org_id, actor_id, Role.member)
    seen_entities: set[uuid.UUID] = {seed_id}
    frontier: set[uuid.UUID] = {seed_id}
    seen_edges: set[uuid.UUID] = set()
    out: list[FactView] = []
    for _ in range(max(1, depth)):
        if not frontier or len(out) >= max_edges:
            break
        stmt = (
            _facts_stmt(org_id, as_of)
            .where(or_(KgEdge.subject_id.in_(frontier), KgEdge.object_id.in_(frontier)))
            .limit(max_edges)
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
    return out[:max_edges]
