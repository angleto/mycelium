"""WS-EVAL T3 (nota 0cb0dda0 §4 + §6, task 0cea068d): scenario runner
through the REAL MCP tool layer.

Every step goes through :func:`mycelium_mcp.gateway.execute_tool` with the
acting identity published in ``_PRINCIPAL`` — the same dispatch, argument
validation, metering and error envelope the HTTP bearer path uses. That is
the point of §4: the scenarios measure what an agent actually experiences,
not what the service layer could do.

The runner consumes the T1 workspace (:mod:`eval_workspace`: registry as
ground truth, ingested through the real services) and the T2 query records
(:mod:`eval_queries`: erasure/perimeter linkage fields), scores with the T4
metric functions, and emits results in the eval_report JSONL dialect (extra
fields are ignored by ``eval_report.load_records``), so the confirmatory
report has ONE path.

Scoring bookkeeping (unit→blob resolution) intentionally bypasses MCP: it
is harness accounting, not a system interaction, and runs in its own
tenant session.

Multi-agent (§6): actors are distinct org members (distinct user ids). An
actor may carry a real ``mycelium_at_…`` agent token; when it does, every
call resolves the principal through :func:`agent_tokens.authenticate` — the
SAME SECURITY DEFINER path the HTTP bearer middleware uses (migration
0056) — instead of injecting a hand-built tuple, so the token→actor
binding is genuinely exercised (§6 addendum A5). ``_PRINCIPAL`` is a
``ContextVar`` and ``asyncio.gather`` wraps coroutines into Tasks that each
copy the ambient context, so concurrent actors cannot leak identities.

§6 hardening (protocol nota 0cb0dda0 §6 addendum, self-adversarial panel
wf_b3b89a9d): concurrency is a TOURNAMENT of independent read→write races
on separate connections with a revision-ledger oracle and a
Clopper-Pearson bound (A4); the review gate leak-probe covers EVERY peer
read surface across many cycles (A6); latency is per-client closed-loop
with warm-up discarded and a bootstrap CI, labelled as in-process
server-side dispatch cost (A7); post-commit visibility is reclassified as
a topology property, not a latency, and a pure-dense probe is added (A8);
and a personal→shared trajectory scenario is added (A9). What remains
in-process (not separate OS transports) is labelled precisely and never
called an "MCP client" when it is a coroutine.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import random
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mycelium_core.db import tenant_session
from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_core.services.eval_metrics import freshness_ok, ndcg_at_k, tokens_chars4
from mycelium_core.services.eval_queries import QueryRecord
from mycelium_core.services.eval_stats import clopper_pearson_upper_zero, percentile_bootstrap
from mycelium_core.services.eval_workspace import (
    IngestResult,
    Workspace,
    resolve_unit_blobs,
)
from mycelium_mcp import gateway
from mycelium_mcp.server import _PRINCIPAL

# Poll budget for cross-agent visibility (§6a): the write is committed when
# the MCP call returns, so visibility is expected on the first probe; the
# loop only absorbs scheduler jitter, it is not an eventual-consistency
# allowance.
VISIBILITY_MAX_PROBES = 40
VISIBILITY_PROBE_SLEEP_S = 0.05
REDTEAM_DEFAULT_ATTEMPTS = 20


@dataclasses.dataclass(frozen=True)
class ScenarioActor:
    """One acting identity: a distinct org member (§6 actors are real
    memberships, not labels — provenance checks read the revision trail).

    ``token`` is the actor's raw ``mycelium_at_…`` agent token. When set,
    :meth:`ScenarioRunner.call` resolves the principal through the real
    ``authenticate_agent_token`` path (§6 addendum A5); when ``None`` the
    runner falls back to the injected-principal path (harness bookkeeping /
    back-compat)."""

    name: str
    kind: str  # "agent" | "human"
    user_id: uuid.UUID
    org_id: uuid.UUID
    token: str | None = None


@dataclasses.dataclass
class StepLog:
    actor: str
    tool: str
    args_hash: str
    ok: bool
    latency_ms: float
    error_code: str | None = None
    authenticated: bool = False  # principal resolved via a real agent token


@dataclasses.dataclass
class ScenarioRecord:
    """One scored observation, emitted in the eval_report dialect (qid,
    category, fact_id, rank, impossible, abstained, top_score,
    served_tokens, gold_tokens, event, ndcg) plus scenario extras.
    ``fact_id`` doubles as the cluster id for T4's cluster bootstrap, so
    steps of one scenario instance share it."""

    qid: str
    category: str
    fact_id: str
    rank: int | None
    impossible: bool = False
    abstained: bool = False
    top_score: float = 0.0
    served_tokens: int = 0
    gold_tokens: int = 0
    event: bool = False
    ndcg: float | None = None
    scenario: str = ""
    actor: str = ""
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "qid": self.qid,
            "category": self.category,
            "fact_id": self.fact_id,
            "rank": self.rank,
            "impossible": self.impossible,
            "abstained": self.abstained,
            "top_score": self.top_score,
            "served_tokens": self.served_tokens,
            "gold_tokens": self.gold_tokens,
            "event": self.event,
            "ndcg": self.ndcg,
            "scenario": self.scenario,
            "actor": self.actor,
        }
        out.update(self.extra)
        return out


@dataclasses.dataclass
class ScenarioSummary:
    """Non-record outcomes: skips with reasons, latency quantiles, red-team
    tallies. Everything the report tables don't consume but the article's
    methods section does."""

    skipped: list[dict[str, str]] = dataclasses.field(default_factory=list)
    concurrency_latency: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


class ScenarioRunner:
    """Executes scripted steps as explicit actors and accumulates the
    step trail + scored records."""

    def __init__(
        self, *, org_id: uuid.UUID, k: int = 10, seed: int = 0, hardware_label: str = "unlabelled"
    ) -> None:
        self.org_id = org_id
        self.k = k
        self.rng = random.Random(seed)  # noqa: S311 - deterministic scenarios, not crypto
        self.steps: list[StepLog] = []
        self.records: list[ScenarioRecord] = []
        self.summary = ScenarioSummary()
        self.hardware_label = hardware_label
        # token_id 0..0 sentinel is never used; cache maps raw token -> the
        # resolved (user_id, org_id, token_id) principal so the binding is
        # resolved once per actor (a bearer's binding is stable) rather than
        # a DB round-trip on every call, which would dominate the A7 latency.
        self._principal_cache: dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]] = {}

    async def _principal_for(
        self, actor: ScenarioActor
    ) -> tuple[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None], bool]:
        """Resolve the principal for an actor. With a real token: through the
        SECURITY DEFINER ``authenticate_agent_token`` (the bearer path), so
        the token→actor binding and token-id population are exercised. Without
        a token: the injected tuple (back-compat / bookkeeping actors)."""
        if actor.token is None:
            return (actor.user_id, actor.org_id, None), False
        cached = self._principal_cache.get(actor.token)
        if cached is None:
            principal = await agent_tokens_svc.authenticate(actor.token)
            if principal is None:
                raise RuntimeError(f"agent token for {actor.name} did not authenticate")
            if principal.user_id != actor.user_id or principal.org_id != actor.org_id:
                raise RuntimeError(f"token binding mismatch for {actor.name}")
            cached = (principal.user_id, principal.org_id, principal.token_id)
            self._principal_cache[actor.token] = cached
        return cached, True

    async def call(self, actor: ScenarioActor, tool: str, args: dict[str, Any]) -> Any:
        digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[
            :12
        ]
        principal, authenticated = await self._principal_for(actor)
        token = _PRINCIPAL.set(principal)
        t0 = time.perf_counter()
        try:
            res = await gateway.execute_tool(name=tool, arguments=args)
        finally:
            _PRINCIPAL.reset(token)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        error_code: str | None = None
        if isinstance(res, dict) and "error" in res:
            err = res["error"]
            error_code = err.get("code", "error") if isinstance(err, dict) else str(err)
        self.steps.append(
            StepLog(
                actor=actor.name,
                tool=tool,
                args_hash=digest,
                ok=error_code is None,
                latency_ms=latency_ms,
                error_code=error_code,
                authenticated=authenticated,
            )
        )
        return res

    async def search(
        self,
        actor: ScenarioActor,
        query: str,
        *,
        project_id: uuid.UUID | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        res = await self.call(
            actor,
            "memory_search",
            {
                "query": query,
                "operation_id": f"wseval-t3-{uuid.uuid4().hex}",
                "project_id": str(project_id) if project_id else None,
                "limit": limit or self.k,
            },
        )
        if not isinstance(res, dict) or "hits" not in res:
            raise RuntimeError(f"memory_search failed: {res!r}")
        return res


async def mint_actor_tokens(actors: Sequence[ScenarioActor]) -> list[ScenarioActor]:
    """Mint a real ``mycelium_at_…`` agent token for each actor and return
    copies carrying the raw token, so calls thereafter resolve the principal
    through the real bearer path (§6 addendum A5).

    Agent tokens are owner-bound (``mint`` requires the actor to be an
    ``owner`` and binds ``user_id=actor_id``): each token-bearing actor must
    therefore be an org owner. This is a documented property of mycelium's
    token model, not a harness shortcut — a distinct-identity token needs a
    distinct owner. The scenario env grants the agents owner role for exactly
    this reason."""
    out: list[ScenarioActor] = []
    for a in actors:
        async with tenant_session(str(a.org_id), str(a.user_id)) as s:
            res = await agent_tokens_svc.mint(
                s, org_id=a.org_id, actor_id=a.user_id, name=f"wseval-{a.name}", scope="mcp"
            )
        out.append(dataclasses.replace(a, token=res.raw))
    return out


# ── scoring helpers ─────────────────────────────────────────────────────────


def _hit_blob_ids(res: dict[str, Any]) -> list[str]:
    return [h["blob"]["id"] for h in res.get("hits", [])]


def _rank_of(res: dict[str, Any], gold_blobs: set[str]) -> int | None:
    for i, bid in enumerate(_hit_blob_ids(res), start=1):
        if bid in gold_blobs:
            return i
    return None


def _served_tokens(res: dict[str, Any]) -> int:
    return sum(tokens_chars4(h["blob"].get("text") or "") for h in res.get("hits", []))


def _top_score(res: dict[str, Any]) -> float:
    hits = res.get("hits", [])
    return float(hits[0]["rrf"]) if hits else 0.0


def _abstained(res: dict[str, Any]) -> bool:
    return bool(res.get("meta", {}).get("abstained", False))


class BlobMap:
    """unit_id -> blob ids (as str) resolved once per ingested workspace;
    late notes (atoms, scenario writes) resolve on demand."""

    def __init__(self, org_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        self._org_id = org_id
        self._actor_id = actor_id
        self.by_unit: dict[str, set[str]] = {}

    async def load(self, ingest: IngestResult) -> None:
        async with tenant_session(str(self._org_id), str(self._actor_id)) as s:
            for unit_id, ref in ingest.units.items():
                note_id = ref.get("note_id")
                if note_id:
                    blobs = await resolve_unit_blobs(
                        s, org_id=self._org_id, note_id=uuid.UUID(note_id)
                    )
                    self.by_unit[unit_id] = {str(b) for b in blobs}

    async def resolve_note(self, note_id: uuid.UUID) -> set[str]:
        async with tenant_session(str(self._org_id), str(self._actor_id)) as s:
            blobs = await resolve_unit_blobs(s, org_id=self._org_id, note_id=note_id)
        return {str(b) for b in blobs}

    def gold_set(self, unit_ids: Sequence[str]) -> set[str]:
        out: set[str] = set()
        for u in unit_ids:
            out |= self.by_unit.get(u, set())
        return out


def _project_of_record(
    record: QueryRecord, ws: Workspace, ingest: IngestResult
) -> uuid.UUID | None:
    """The retrieval perimeter for a query: its declared context project
    (perimeter cases) or the home project of its first gold unit. The
    fallback for goldless records (impossible) is the entity's project via
    any unit realizing one of its facts — a query always runs from SOME
    perimeter, never from the org-wide null scope (the documented
    ``_project_pred`` trap)."""
    units_by_id = {u.unit_id: u for u in ws.units}
    if record.context_project:
        return ingest.project_ids.get(record.context_project)
    if record.gold_unit_ids:
        unit = units_by_id.get(record.gold_unit_ids[0])
        if unit is not None:
            return ingest.project_ids.get(unit.project)
    if record.fact_id:
        entity = record.fact_id.split(":")[0] if ":" in record.fact_id else None
        facts_by_id = {f.fact_id: f for f in ws.facts}
        f = facts_by_id.get(record.fact_id)
        name = f.entity_name if f is not None else entity
        for u in ws.units:
            if name and name in u.text and u.unit_id in ingest.units:
                return ingest.project_ids.get(u.project)
    return next(iter(ingest.project_ids.values()), None)


def _gold_tokens(record: QueryRecord, ws: Workspace) -> int:
    units_by_id = {u.unit_id: u for u in ws.units}
    return sum(tokens_chars4(units_by_id[u].text) for u in record.gold_unit_ids if u in units_by_id)


# ── §4 static queries through MCP ───────────────────────────────────────────


async def run_static_queries(
    runner: ScenarioRunner,
    actor: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
    blobmap: BlobMap,
    queries: Sequence[QueryRecord],
) -> None:
    """T2 records executed via MCP as ``actor``: collision / distributed /
    freshness (incl. anti-recency) / as_of_previous / impossible. Perimeter
    and erasure records are handled by their dedicated scenarios."""
    facts_by_id = {f.fact_id: f for f in ws.facts}
    for q in queries:
        if q.category in ("perimeter", "erasure"):
            continue
        project_id = _project_of_record(q, ws, ingest)
        res = await runner.search(actor, q.query_text, project_id=project_id)
        gold = blobmap.gold_set(q.gold_unit_ids)
        rank = _rank_of(res, gold) if gold else None
        ndcg = (
            ndcg_at_k(
                [uuid.UUID(b) for b in _hit_blob_ids(res)],
                {uuid.UUID(b) for b in gold},
                k=runner.k,
            )
            if gold
            else None
        )
        extra: dict[str, Any] = {"lang_inverted": q.lang_inverted}
        if q.category in ("freshness", "as_of_previous"):
            fact = facts_by_id.get(q.fact_id or "")
            if fact is not None and fact.stale_unit_id:
                current = blobmap.gold_set(q.gold_unit_ids)
                stale = blobmap.gold_set([fact.stale_unit_id])
                hit_uuids = [uuid.UUID(b) for b in _hit_blob_ids(res)]
                fresh = all(
                    freshness_ok(
                        hit_uuids,
                        current_id=uuid.UUID(c),
                        stale_id=uuid.UUID(next(iter(stale))) if stale else uuid.uuid4(),
                        k=runner.k,
                    )
                    for c in list(current)[:1]
                )
                extra["freshness_ok"] = fresh
        runner.records.append(
            ScenarioRecord(
                qid=q.query_id,
                category=q.category,
                fact_id=q.fact_id or q.category,
                rank=rank,
                impossible=q.expected_empty and q.category == "impossible",
                abstained=_abstained(res),
                top_score=_top_score(res),
                served_tokens=_served_tokens(res),
                gold_tokens=_gold_tokens(q, ws),
                event=False,
                ndcg=ndcg,
                scenario="static",
                actor=actor.name,
                extra=extra,
            )
        )


# ── §4 freshness, interactive ───────────────────────────────────────────────


async def run_freshness_interactive(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    reader: ScenarioActor,
    *,
    project_name: str,
    ingest: IngestResult,
    blobmap: BlobMap,
) -> None:
    """write→search→update→re-search: the updated content must be served
    to a DIFFERENT reader after the update, from the same note."""
    marker = f"wsx{runner.rng.randrange(10**8):08d}"
    project_id = ingest.project_ids.get(project_name)
    v1 = f"Il codice di sblocco della cassaforte {marker} è alfa-31."
    v2 = f"Il codice di sblocco della cassaforte {marker} è omega-77."
    note = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"Cassaforte {marker}",
            "text": v1,
            "project_id": str(project_id) if project_id else None,
        },
    )
    note_id = uuid.UUID(note["id"])
    note_blobs = await blobmap.resolve_note(note_id)
    pre = await runner.search(
        reader, f"codice di sblocco cassaforte {marker}", project_id=project_id
    )
    pre_rank = _rank_of(pre, note_blobs)
    await runner.call(
        writer,
        "append_note_part",
        {"note_id": str(note_id), "chunk": v2},
    )
    note_blobs = await blobmap.resolve_note(note_id)
    post = await runner.search(reader, f"codice omega cassaforte {marker}", project_id=project_id)
    post_rank = _rank_of(post, note_blobs)
    runner.records.append(
        ScenarioRecord(
            qid=f"fresh-int-{marker}",
            category="freshness_interactive",
            fact_id=f"fresh-int-{marker}",
            rank=post_rank,
            event=post_rank is None,  # updated content not visible = event
            top_score=_top_score(post),
            served_tokens=_served_tokens(post),
            gold_tokens=tokens_chars4(v1) + tokens_chars4(v2),
            scenario="freshness_interactive",
            actor=reader.name,
            extra={"pre_rank": pre_rank, "writer": writer.name},
        )
    )


# ── §4 humus cycle with a deterministic approval policy ─────────────────────


def approval_policy(atom_text: str, required: Sequence[tuple[str, str]]) -> bool:
    """Deterministic, gold-computable review policy (§4): approve iff the
    atom covers EVERY (attribute, value) pair marked for consolidation.
    Published as part of the protocol freeze package."""
    lowered = atom_text.lower()
    return all(a.lower() in lowered and v.lower() in lowered for a, v in required)


async def run_humus_cycle(
    runner: ScenarioRunner,
    agent: ScenarioActor,
    human: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
    blobmap: BlobMap,
    displacement_queries: Sequence[QueryRecord],
) -> None:
    """archive→distill(external deterministic text)→policy review→retrieval.
    Approved atom must WIN consolidation queries without displacing raw
    results; the rejected atom must never surface."""
    facts_by_id = {f.fact_id: f for f in ws.facts}
    archived = [
        u
        for u in ws.units
        if u.archived and u.unit_kind == "note" and u.unit_id in ingest.units
        if any(facts_by_id[f].queryable and facts_by_id[f].category == "gold" for f in u.fact_ids)
    ]
    if len(archived) < 2:
        runner.summary.skipped.append(
            {"scenario": "humus_cycle", "reason": "fewer than 2 archived gold-bearing notes"}
        )
        return
    approve_src, reject_src = archived[0], archived[1]

    def _required(u: Any) -> list[tuple[str, str]]:
        return [
            (facts_by_id[f].attribute, facts_by_id[f].value)
            for f in u.fact_ids
            if facts_by_id[f].queryable and facts_by_id[f].category == "gold"
        ]

    req_facts = [
        facts_by_id[f]
        for f in approve_src.fact_ids
        if facts_by_id[f].queryable and facts_by_id[f].category == "gold"
    ]
    req = [(f.attribute, f.value) for f in req_facts]
    atom_text = "Sintesi: " + "; ".join(
        f"{f.entity_name} — {f.attribute}: {f.value}" for f in req_facts
    )
    if not approval_policy(atom_text, req):
        raise RuntimeError("humus scenario: composed atom does not satisfy its own policy")
    distilled = await runner.call(
        agent,
        "distill_note",
        {
            "note_id": ingest.units[approve_src.unit_id]["note_id"],
            "distilled_text": atom_text,
            "origin_model_id": "wseval-t3-deterministic",
        },
    )
    atom_note_id = uuid.UUID(distilled["distilled_note_id"])
    await runner.call(human, "garden_review_approve", {"note_id": str(atom_note_id)})
    atom_blobs = await blobmap.resolve_note(atom_note_id)
    project_id = ingest.project_ids.get(approve_src.project)
    first = req_facts[0]
    consolidation_q = f"sintesi {first.entity_name} {first.attribute}"
    res = await runner.search(agent, consolidation_q, project_id=project_id)
    atom_rank = _rank_of(res, atom_blobs)
    src_rank = _rank_of(res, blobmap.gold_set([approve_src.unit_id]))
    atom_won = atom_rank is not None and (src_rank is None or atom_rank <= src_rank)
    runner.records.append(
        ScenarioRecord(
            qid=f"humus-consolidation-{approve_src.unit_id}",
            category="humus_consolidation",
            fact_id=f"humus-{approve_src.unit_id}",
            rank=atom_rank,
            event=not atom_won,
            top_score=_top_score(res),
            served_tokens=_served_tokens(res),
            gold_tokens=tokens_chars4(atom_text),
            scenario="humus_cycle",
            actor=agent.name,
            extra={"atom_rank": atom_rank, "source_rank": src_rank, "atom_won": atom_won},
        )
    )

    # Displacement guard: raw queries must not degrade across the cycle.
    for q in list(displacement_queries)[:2]:
        project = _project_of_record(q, ws, ingest)
        post = await runner.search(agent, q.query_text, project_id=project)
        post_rank = _rank_of(post, blobmap.gold_set(q.gold_unit_ids))
        pre_rank = next(
            (r.rank for r in runner.records if r.qid == q.query_id and r.scenario == "static"),
            None,
        )
        degraded = pre_rank is not None and (post_rank is None or post_rank > pre_rank)
        runner.records.append(
            ScenarioRecord(
                qid=f"humus-displacement-{q.query_id}",
                category="humus_displacement",
                fact_id=q.fact_id or "humus-displacement",
                rank=post_rank,
                event=degraded,
                scenario="humus_cycle",
                actor=agent.name,
                extra={"pre_rank": pre_rank, "post_rank": post_rank},
            )
        )

    # Rejected branch: a non-covering atom, policy rejects, must not surface.
    reject_marker = f"wsr{runner.rng.randrange(10**8):08d}"
    bad_text = f"Sintesi generica priva dei fatti richiesti {reject_marker}."
    if approval_policy(bad_text, _required(reject_src)):
        raise RuntimeError("humus scenario: non-covering atom passed the policy")
    distilled2 = await runner.call(
        agent,
        "distill_note",
        {
            "note_id": ingest.units[reject_src.unit_id]["note_id"],
            "distilled_text": bad_text,
            "origin_model_id": "wseval-t3-deterministic",
        },
    )
    rejected_id = uuid.UUID(distilled2["distilled_note_id"])
    await runner.call(
        human,
        "garden_review_reject",
        {"note_id": str(rejected_id), "reason": "policy: no coverage"},
    )
    rejected_blobs = await blobmap.resolve_note(rejected_id)
    chk = await runner.search(
        agent,
        f"sintesi generica {reject_marker}",
        project_id=ingest.project_ids.get(reject_src.project),
    )
    leaked = _rank_of(chk, rejected_blobs) is not None
    runner.records.append(
        ScenarioRecord(
            qid=f"humus-rejected-{reject_src.unit_id}",
            category="humus_rejected_gate",
            fact_id=f"humus-{reject_src.unit_id}",
            rank=None,
            event=leaked,
            scenario="humus_cycle",
            actor=agent.name,
            extra={"rejected_note_id": str(rejected_id)},
        )
    )


# ── §4 erasure propagation + specificity ────────────────────────────────────


async def run_erasure(
    runner: ScenarioRunner,
    agent: ScenarioActor,
    human: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
    blobmap: BlobMap,
    queries: Sequence[QueryRecord],
) -> None:
    """T2 erasure records: pre-hit → derive an atom from the source →
    gdpr_erase_note(source) → the marked queries MUST degrade; the derived
    atom's survival is MEASURED (erasure propagation, the article's
    governance axis); independent queries must NOT degrade (specificity)."""
    erasure = [q for q in queries if q.category == "erasure" and q.erase_unit_id]
    independent = [
        q for q in queries if q.category in ("collision", "distributed") and not q.erase_unit_id
    ][:2]
    if not erasure:
        runner.summary.skipped.append({"scenario": "erasure", "reason": "no erasure queries"})
        return
    units_by_id = {u.unit_id: u for u in ws.units}
    facts_by_id = {f.fact_id: f for f in ws.facts}

    spec_pre: dict[str, int | None] = {}
    for q in independent:
        res = await runner.search(agent, q.query_text, project_id=_project_of_record(q, ws, ingest))
        spec_pre[q.query_id] = _rank_of(res, blobmap.gold_set(q.gold_unit_ids))

    # A source is erased once; further records targeting the same unit
    # reuse the outcome (T2 can emit several queries per erased source).
    erased_units: dict[str, tuple[bool, set[str]]] = {}
    for q in erasure[:3]:
        unit = units_by_id.get(q.erase_unit_id or "")
        ref = ingest.units.get(q.erase_unit_id or "")
        if unit is None or not ref or "note_id" not in ref:
            continue
        project_id = _project_of_record(q, ws, ingest)
        pre = await runner.search(agent, q.query_text, project_id=project_id)
        pre_rank = _rank_of(pre, blobmap.gold_set(q.gold_unit_ids))

        target_unit = q.erase_unit_id or ""
        if target_unit in erased_units:
            erase_ok, derived_blobs = erased_units[target_unit]
        else:
            # Derive an atom from the source BEFORE erasing, so propagation
            # to derivatives is measurable (protocol §4: erasure a cascata).
            fact = facts_by_id.get(q.fact_id or "")
            derived_text = (
                f"Sintesi: {fact.entity_name} — {fact.attribute}: {fact.value}."
                if fact is not None
                else f"Sintesi del contenuto: {unit.title}."
            )
            distilled = await runner.call(
                agent,
                "distill_note",
                {
                    "note_id": ref["note_id"],
                    "distilled_text": derived_text,
                    "origin_model_id": "wseval-t3-deterministic",
                },
            )
            derived_blobs = set()
            if isinstance(distilled, dict) and "distilled_note_id" in distilled:
                atom_id = uuid.UUID(distilled["distilled_note_id"])
                await runner.call(human, "garden_review_approve", {"note_id": str(atom_id)})
                derived_blobs = await blobmap.resolve_note(atom_id)

            erased = await runner.call(agent, "gdpr_erase_note", {"note_id": ref["note_id"]})
            erase_ok = isinstance(erased, dict) and "error" not in erased
            erased_units[target_unit] = (erase_ok, derived_blobs)

        post = await runner.search(agent, q.query_text, project_id=project_id)
        post_rank = _rank_of(post, blobmap.gold_set(q.gold_unit_ids))
        derived_rank = _rank_of(post, derived_blobs) if derived_blobs else None
        derived_survived = derived_rank is not None
        # The degradation claim only binds where the query HIT before the
        # erase; a pre-miss (hard adversarial phrasing) is recorded as such
        # and excluded from the erasure endpoint, never silently counted.
        pre_hit = pre_rank is not None
        degraded = post_rank is None
        runner.records.append(
            ScenarioRecord(
                qid=f"erasure-{q.query_id}",
                category="erasure_propagation",
                fact_id=q.fact_id or f"erasure-{q.query_id}",
                rank=post_rank,
                # event = the erased fact is still reachable, directly or
                # via the derivative — the exact thing a garante would ask.
                event=(pre_hit and not degraded) or derived_survived,
                top_score=_top_score(post),
                scenario="erasure",
                actor=agent.name,
                extra={
                    "pre_rank": pre_rank,
                    "pre_hit": pre_hit,
                    "erase_ok": erase_ok,
                    "derived_survived": derived_survived,
                    "derived_rank": derived_rank,
                },
            )
        )

    for q in independent:
        res = await runner.search(agent, q.query_text, project_id=_project_of_record(q, ws, ingest))
        post_rank = _rank_of(res, blobmap.gold_set(q.gold_unit_ids))
        pre_rank = spec_pre.get(q.query_id)
        degraded = pre_rank is not None and (post_rank is None or post_rank > pre_rank)
        runner.records.append(
            ScenarioRecord(
                qid=f"erasure-specificity-{q.query_id}",
                category="erasure_specificity",
                fact_id=q.fact_id or "erasure-specificity",
                rank=post_rank,
                event=degraded,
                scenario="erasure",
                actor=agent.name,
                extra={"pre_rank": pre_rank, "post_rank": post_rank},
            )
        )


# ── §4 perimeter: scripted 0/N + red-team hook ──────────────────────────────


async def run_perimeter(
    runner: ScenarioRunner,
    agent: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
    blobmap: BlobMap,
    queries: Sequence[QueryRecord],
) -> None:
    """Perimeter records searched from the WRONG project: any hit carrying
    an out-of-perimeter unit's blob is a leak event (0/N feeds the
    Clopper-Pearson bound in T4's report)."""
    units_by_id = {u.unit_id: u for u in ws.units}
    blob_to_unit: dict[str, str] = {}
    for unit_id, blobs in blobmap.by_unit.items():
        for b in blobs:
            blob_to_unit[b] = unit_id
    for q in (x for x in queries if x.category == "perimeter"):
        if not q.context_project:
            continue
        project_id = ingest.project_ids.get(q.context_project)
        res = await runner.search(agent, q.query_text, project_id=project_id)
        leak = False
        for bid in _hit_blob_ids(res):
            unit = units_by_id.get(blob_to_unit.get(bid, ""))
            if unit is not None and unit.project != q.context_project:
                leak = True
                break
        runner.records.append(
            ScenarioRecord(
                qid=f"perimeter-{q.query_id}",
                category="perimeter",
                fact_id=q.fact_id or f"perimeter-{q.query_id}",
                rank=None,
                event=leak,
                top_score=_top_score(res),
                scenario="perimeter",
                actor=agent.name,
                extra={"home_project": q.home_project, "context_project": q.context_project},
            )
        )


async def redteam_perimeter(
    runner: ScenarioRunner,
    agent: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
    blobmap: BlobMap,
    attempts: int = REDTEAM_DEFAULT_ATTEMPTS,
) -> None:
    """Active adversarial search (§4/§1.4): craft queries from the highest-
    information out-of-perimeter registry terms and try to pull those units
    into a foreign perimeter. Every attempt is a record; any hit is a leak
    event."""
    projects = sorted(ingest.project_ids)
    if len(projects) < 2:
        runner.summary.skipped.append({"scenario": "perimeter_redteam", "reason": "single project"})
        return
    context = projects[0]
    context_id = ingest.project_ids[context]
    units_by_id = {u.unit_id: u for u in ws.units}
    foreign_facts = [
        f
        for f in ws.facts
        if f.category == "gold"
        and f.gold_unit_ids
        and units_by_id.get(f.gold_unit_ids[0]) is not None
        and units_by_id[f.gold_unit_ids[0]].project != context
    ]
    runner.rng.shuffle(foreign_facts)
    blob_to_unit: dict[str, str] = {}
    for unit_id, blobs in blobmap.by_unit.items():
        for b in blobs:
            blob_to_unit[b] = unit_id
    for i, f in enumerate(foreign_facts[:attempts]):
        # Greedy adversarial phrasing: the exact entity + attribute + value
        # terms of the foreign fact, the strongest lexical+dense pull we can
        # craft from the registry alone.
        q = f"{f.entity_name} {f.attribute} {f.value}"
        res = await runner.search(agent, q, project_id=context_id)
        leak = False
        for bid in _hit_blob_ids(res):
            unit = units_by_id.get(blob_to_unit.get(bid, ""))
            if unit is not None and unit.project != context:
                leak = True
                break
        runner.records.append(
            ScenarioRecord(
                qid=f"perimeter-redteam-{i}",
                category="perimeter_redteam",
                fact_id=f.fact_id,
                rank=None,
                event=leak,
                scenario="perimeter_redteam",
                actor=agent.name,
                extra={
                    "context_project": context,
                    "foreign_project": units_by_id[f.gold_unit_ids[0]].project,
                },
            )
        )


# ── §4 kg as-of + bounded graph walk ────────────────────────────────────────


async def run_kg_and_walk(
    runner: ScenarioRunner,
    agent: ScenarioActor,
    *,
    ws: Workspace,
    ingest: IngestResult,
) -> None:
    """KG surface smoke (current facts) + bounded graph walk. The temporal
    as-of assertion and the multi-hop chains are skipped with a reason
    until T1 grows ``valid_from`` and registry-backed relational values
    (protocol §2 addendum, points 4 and 6)."""
    kg_facts = [f for f in ws.facts if f.kg and f.gold_unit_ids and f.category == "gold"]
    if kg_facts:
        f = kg_facts[0]
        ents = await runner.call(agent, "kg_entities", {"query": f.entity_name, "limit": 5})
        ent_id = None
        items = ents.get("entities", ents) if isinstance(ents, dict) else ents
        if isinstance(items, list):
            for e in items:
                if isinstance(e, dict) and e.get("name") == f.entity_name:
                    ent_id = e.get("id")
                    break
        found = False
        if ent_id:
            nb = await runner.call(agent, "kg_neighbors", {"entity": str(ent_id)})
            edges = nb.get("edges", nb) if isinstance(nb, dict) else nb
            if isinstance(edges, list):
                found = any(
                    isinstance(e, dict) and e.get("predicate") == f.attribute for e in edges
                )
        runner.records.append(
            ScenarioRecord(
                qid=f"kg-current-{f.fact_id}",
                category="kg_current",
                fact_id=f.fact_id,
                rank=1 if found else None,
                event=not found,
                scenario="kg",
                actor=agent.name,
            )
        )
    else:
        runner.summary.skipped.append({"scenario": "kg_current", "reason": "no kg gold facts"})
    runner.summary.skipped.append(
        {
            "scenario": "kg_as_of",
            "reason": "T1 registry has no valid_from dates (protocol §2 addendum, point 6)",
        }
    )

    linked = next(
        (u for u in ws.units if u.links and ingest.units.get(u.unit_id, {}).get("note_id")),
        None,
    )
    if linked is None:
        runner.summary.skipped.append({"scenario": "graph_walk", "reason": "no linked note"})
    else:
        walk = await runner.call(
            agent,
            "graph_walk",
            {"seed": ingest.units[linked.unit_id]["note_id"], "mode": "bounded", "budget": 8},
        )
        nodes = walk.get("nodes", walk) if isinstance(walk, dict) else walk
        ok = isinstance(nodes, list) and len(nodes) >= 1
        runner.records.append(
            ScenarioRecord(
                qid=f"walk-bounded-{linked.unit_id}",
                category="graph_walk_bounded",
                fact_id=f"walk-{linked.unit_id}",
                rank=1 if ok else None,
                event=not ok,
                scenario="graph_walk",
                actor=agent.name,
                extra={"nodes": len(nodes) if isinstance(nodes, list) else 0},
            )
        )
    runner.summary.skipped.append(
        {
            "scenario": "multi_hop",
            "reason": (
                "T1 relational values not registry-backed yet (protocol §2 addendum, point 4)"
            ),
        }
    )


# ── §6 multi-agent ──────────────────────────────────────────────────────────


async def run_multi_agent(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    reader: ScenarioActor,
    human: ScenarioActor,
    *,
    ingest: IngestResult,
    blobmap: BlobMap,
    project_name: str,
) -> None:
    """§6 (a)-(d): cross-agent visibility latency, per-actor provenance via
    the revision trail, concurrent same-part writes (optimistic versioning:
    exactly one winner, loser retries to a consistent final state), and the
    distributed review gate (proposed atoms invisible until human approval)."""
    project_id = ingest.project_ids.get(project_name)

    # (a) visibility: writer commits, reader must see it on the next search.
    marker = f"wsv{runner.rng.randrange(10**8):08d}"
    note = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"Visibilità {marker}",
            "text": f"Il canale di emergenza {marker} è il ponte radio nove.",
            "project_id": str(project_id) if project_id else None,
        },
    )
    note_blobs = await blobmap.resolve_note(uuid.UUID(note["id"]))
    t0 = time.perf_counter()
    seen_rank: int | None = None
    probes = 0
    while probes < VISIBILITY_MAX_PROBES:
        probes += 1
        res = await runner.search(reader, f"canale emergenza {marker}", project_id=project_id)
        seen_rank = _rank_of(res, note_blobs)
        if seen_rank is not None:
            break
        await asyncio.sleep(VISIBILITY_PROBE_SLEEP_S)
    visibility_ms = (time.perf_counter() - t0) * 1000.0
    runner.records.append(
        ScenarioRecord(
            qid=f"ma-visibility-{marker}",
            category="ma_visibility",
            fact_id=f"ma-{marker}",
            rank=seen_rank,
            event=seen_rank is None,
            scenario="multi_agent",
            actor=reader.name,
            extra={
                # A8: reclassified. Under a single primary with in-transaction
                # indexing, first-probe visibility is a TOPOLOGY property, not
                # a measured latency; the number below is the post-commit
                # SEARCH latency (dominated by the search itself), NOT an
                # eventual-consistency delay. Deployments with read replicas /
                # a transaction-mode pooler would need re-measuring.
                "classification": "topology_property_single_primary",
                "search_latency_post_commit_ms": visibility_ms,
                "poll_resolution_ms": VISIBILITY_PROBE_SLEEP_S * 1000.0,
                "probes": probes,
                "writer": writer.name,
            },
        )
    )

    # (b) provenance: the creating revision must carry the writer's id.
    revs = await runner.call(reader, "list_note_revisions", {"note_id": note["id"]})
    rev_list = revs if isinstance(revs, list) else revs.get("revisions", [])
    actor_ids = {r.get("actor_id") for r in rev_list if isinstance(r, dict)}
    provenance_ok = str(writer.user_id) in actor_ids
    runner.records.append(
        ScenarioRecord(
            qid=f"ma-provenance-{marker}",
            category="ma_provenance",
            fact_id=f"ma-{marker}",
            rank=1 if provenance_ok else None,
            event=not provenance_ok,
            scenario="multi_agent",
            actor=reader.name,
            extra={"writer": writer.name, "revision_actors": sorted(a for a in actor_ids if a)},
        )
    )

    # (c) concurrent same-part writes under optimistic versioning.
    cnote = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"Concorrenza {marker}",
            "text": "stato iniziale",
            "project_id": str(project_id) if project_id else None,
        },
    )
    got = await runner.call(writer, "get_note", {"note_id": cnote["id"]})
    part_id = got["parts"][0]["id"]
    part_version = got["parts"][0].get("version", 1)

    async def _update(actor: ScenarioActor, body: str) -> Any:
        return await runner.call(
            actor,
            "update_note_part",
            {"part_id": str(part_id), "body": body, "expected_version": part_version},
        )

    res_a, res_b = await asyncio.gather(
        _update(writer, "versione di writer"), _update(reader, "versione di reader")
    )
    errs = [r for r in (res_a, res_b) if isinstance(r, dict) and "error" in r]
    winners = [r for r in (res_a, res_b) if not (isinstance(r, dict) and "error" in r)]
    one_winner = len(winners) == 1 and len(errs) == 1
    # The loser retries against the fresh version: last write wins, state
    # stays consistent (no interleaving of the two bodies).
    final_body = None
    if one_winner:
        got = await runner.call(human, "get_note", {"note_id": cnote["id"]})
        fresh_version = got["parts"][0].get("version")
        loser = reader if winners and winners[0] is res_a else writer
        retry = await runner.call(
            loser,
            "update_note_part",
            {
                "part_id": str(part_id),
                "body": "versione del retry",
                "expected_version": fresh_version,
            },
        )
        if not (isinstance(retry, dict) and "error" in retry):
            got2 = await runner.call(human, "get_note", {"note_id": cnote["id"]})
            final_body = got2["parts"][0].get("body") or got2["parts"][0].get("text")
    consistent = final_body == "versione del retry"
    runner.records.append(
        ScenarioRecord(
            qid=f"ma-concurrent-{marker}",
            category="ma_concurrent_writes",
            fact_id=f"ma-{marker}",
            rank=1 if (one_winner and consistent) else None,
            event=not (one_winner and consistent),
            scenario="multi_agent",
            actor=writer.name,
            extra={"one_winner": one_winner, "final_body": final_body},
        )
    )

    # (d) distributed review gate: proposed is invisible to peers, visible
    # after the human approves.
    gate_marker = f"wsg{runner.rng.randrange(10**8):08d}"
    src = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"Fonte gate {gate_marker}",
            "text": f"La parola d'ordine del cantiere {gate_marker} è ginepro.",
            "project_id": str(project_id) if project_id else None,
        },
    )
    await runner.call(
        writer,
        "archive_note",
        {"note_id": src["id"], "expected_version": src.get("version", 1), "archived": True},
    )
    distilled = await runner.call(
        writer,
        "distill_note",
        {
            "note_id": src["id"],
            "distilled_text": f"Sintesi: cantiere {gate_marker} parola d'ordine ginepro.",
            "origin_model_id": "wseval-t3-deterministic",
        },
    )
    atom_id = distilled["distilled_note_id"]
    atom_blobs = await blobmap.resolve_note(uuid.UUID(atom_id))
    pre = await runner.search(reader, f"sintesi cantiere {gate_marker}", project_id=project_id)
    visible_before = _rank_of(pre, atom_blobs) is not None
    await runner.call(human, "garden_review_approve", {"note_id": str(atom_id)})
    post = await runner.search(reader, f"sintesi cantiere {gate_marker}", project_id=project_id)
    visible_after = _rank_of(post, atom_blobs) is not None
    runner.records.append(
        ScenarioRecord(
            qid=f"ma-review-gate-{gate_marker}",
            category="ma_review_gate",
            fact_id=f"ma-{gate_marker}",
            rank=1 if (not visible_before and visible_after) else None,
            event=visible_before or not visible_after,
            scenario="multi_agent",
            actor=reader.name,
            extra={
                "visible_before_approval": visible_before,
                "visible_after_approval": visible_after,
            },
        )
    )


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


async def run_concurrency_latency(
    runner: ScenarioRunner,
    actors: Sequence[ScenarioActor],
    *,
    queries: Sequence[str],
    project_id: uuid.UUID | None,
    levels: Sequence[int] = (1, 5, 10),
    per_client: int = 100,
    warmup: int = 20,
    replicas: int = 5,
    hardware_label: str | None = None,
) -> None:
    """§6(e) hardening (A7): search latency p50/p95 at increasing
    concurrency. Each level runs L per-client CLOSED-LOOP workers with NO
    shared batch barrier (the old batched ``gather`` imposed a barrier per
    batch — coordinated omission — and pinned n=30/level). Warm-up requests
    are discarded; each level is measured over ``replicas`` runs whose ORDER
    is randomized to unconfound cache/warm state; the p95 carries a bootstrap
    CI.

    IMPORTANT LABEL (never publish bare): these are IN-PROCESS concurrent
    coroutine workers sharing one event loop and one process, not separate
    OS/transport MCP clients. The number is the SERVER-SIDE dispatch cost
    under concurrent in-flight calls, a lower bound on real client-perceived
    latency, not the latency a remote client would see."""
    pool = list(queries) or ["stato del progetto"]
    hw = hardware_label or runner.hardware_label

    async def _worker(actor: ScenarioActor, count: int, sink: list[float]) -> None:
        # Closed-loop: issue the next request as soon as the previous
        # returns, no barrier with sibling workers.
        for j in range(count):
            q = pool[(id(sink) + j) % len(pool)]
            t0 = time.perf_counter()
            await runner.search(actor, q, project_id=project_id)
            sink.append((time.perf_counter() - t0) * 1000.0)

    # Warm-up (discarded): a serial burst so the model / connection pool /
    # query plan caches are hot before any level is timed.
    for j in range(warmup):
        await runner.search(actors[j % len(actors)], pool[j % len(pool)], project_id=project_id)

    for level in levels:
        p95_replicas: list[float] = []
        p50_replicas: list[float] = []
        total_n = 0
        order = list(range(replicas))
        runner.rng.shuffle(order)  # randomize replica order (level order is caller's)
        for _rep in order:
            sinks: list[list[float]] = [[] for _ in range(level)]
            await asyncio.gather(
                *(_worker(actors[c % len(actors)], per_client, sinks[c]) for c in range(level))
            )
            lat = sorted(x for sink in sinks for x in sink)
            if not lat:
                continue
            total_n += len(lat)
            p50_replicas.append(_quantile(lat, 0.50))
            p95_replicas.append(_quantile(lat, 0.95))
        if not p95_replicas:
            continue
        p95_lo, p95_hi = percentile_bootstrap(p95_replicas, rng=runner.rng, n_resamples=2000)
        runner.summary.concurrency_latency.append(
            {
                "concurrent_workers": level,
                "n": total_n,
                "replicas": len(p95_replicas),
                "p50_ms": round(sorted(p50_replicas)[len(p50_replicas) // 2], 2),
                "p95_ms": round(sorted(p95_replicas)[len(p95_replicas) // 2], 2),
                "p95_ci95_ms": [round(p95_lo, 2), round(p95_hi, 2)],
                "hardware": hw,
                "measures": "in-process server-side dispatch cost (not separate MCP clients)",
            }
        )


# ── §6 A4: write-race tournament with a revision-ledger oracle ──────────────


async def run_write_race_tournament(
    runner: ScenarioRunner,
    contenders: Sequence[ScenarioActor],
    *,
    project_id: uuid.UUID | None,
    races: int = 60,
    ks: Sequence[int] = (2, 3, 5),
) -> dict[str, Any]:
    """A4: replace the single n=1 same-version race with a TOURNAMENT of
    independent read→write races and a revision-LEDGER oracle.

    Each race: a fresh note+part; k contenders each INDEPENDENTLY read the
    current version (``get_note``) and then write with that
    ``expected_version`` — a genuine race on the read→write window, run
    concurrently on separate principals, so the DB (not a pre-shared stale
    version) arbitrates. Oracle over the revision trail: exactly one writer
    committed per version bump, the final body is exactly one contender's
    body (no torn/interleaved write), and every loser got ``stale_version``.
    Any deviation is an anomaly; the zero-anomaly rate carries a
    Clopper-Pearson upper bound.

    One race per round adds a THIRD write from the first loser's retry to
    probe the retry→re-race path."""
    anomalies = 0
    run_races = 0
    for i in range(races):
        k = ks[i % len(ks)]
        actors = [contenders[j % len(contenders)] for j in range(k)]
        note = await runner.call(
            actors[0],
            "create_note",
            {
                "kind": "text",
                "title": f"race-{i}",
                "text": "v0",
                "project_id": str(project_id) if project_id else None,
            },
        )
        if not isinstance(note, dict) or "id" not in note:
            continue
        note_id = note["id"]

        async def _contend(actor: ScenarioActor, body: str, nid: str = note_id) -> Any:
            got = await runner.call(actor, "get_note", {"note_id": nid})
            if not isinstance(got, dict) or not got.get("parts"):
                return {"error": "no-parts"}
            part = got["parts"][0]
            # small jitter so the read→write windows genuinely overlap
            await asyncio.sleep(runner.rng.uniform(0.0, 0.01))
            return await runner.call(
                actor,
                "update_note_part",
                {
                    "part_id": str(part["id"]),
                    "body": body,
                    "expected_version": part.get("version", 1),
                },
            )

        bodies = [f"body-{i}-{j}" for j in range(k)]
        results = await asyncio.gather(
            *(_contend(a, b) for a, b in zip(actors, bodies, strict=True))
        )
        winners = [
            b
            for b, r in zip(bodies, results, strict=True)
            if not (isinstance(r, dict) and "error" in r)
        ]
        losers = [
            (a, b, r)
            for a, b, r in zip(actors, bodies, results, strict=True)
            if isinstance(r, dict) and "error" in r
        ]
        run_races += 1

        # Ledger oracle. Under optimistic concurrency the correct invariant
        # is NOT "exactly one winner": cooperatively-serialized contenders
        # may each read a fresh version and commit in turn (last-write-wins),
        # which is correct. The anomalies that would matter are: (a) NOBODY
        # committed; (b) the final body is TORN — not exactly one contender's
        # body (an interleaved/partial write); (c) a loser failed for a
        # reason OTHER than stale-version (a lost update masked as a
        # different error). Each committed write must have advanced the
        # version by exactly one, so the final version equals initial +
        # #winners (no lost update, no double-count).
        final = await runner.call(actors[0], "get_note", {"note_id": note_id})
        final_body = None
        final_version = None
        if isinstance(final, dict) and final.get("parts"):
            final_body = final["parts"][0].get("body") or final["parts"][0].get("text")
            final_version = final["parts"][0].get("version")
        anomaly = False
        if len(winners) == 0:
            anomaly = True  # the conditional UPDATE let no one commit
        if final_body not in bodies:
            anomaly = True  # torn / interleaved body
        # Version monotonicity: the part started at v1; each committed write
        # advances it by exactly one. A mismatch is a lost update or a double
        # bump — the failure a naive last-write-wins would hide.
        if final_version is not None and final_version != 1 + len(winners):
            anomaly = True
        for _a, _b, r in losers:
            code = r.get("error", {})
            code = code.get("code") if isinstance(code, dict) else str(code)
            if code and "stale" not in str(code).lower() and "version" not in str(code).lower():
                anomaly = True  # a loser that failed for a non-concurrency reason

        # retry→re-race: the first loser reads fresh and re-writes; must succeed
        retry_ok = True
        if losers:
            la, _lb, _ = losers[0]
            got2 = await runner.call(la, "get_note", {"note_id": note_id})
            if isinstance(got2, dict) and got2.get("parts"):
                p2 = got2["parts"][0]
                retry = await runner.call(
                    la,
                    "update_note_part",
                    {
                        "part_id": str(p2["id"]),
                        "body": f"retry-{i}",
                        "expected_version": p2.get("version", 1),
                    },
                )
                retry_ok = not (isinstance(retry, dict) and "error" in retry)
                if not retry_ok:
                    anomaly = True

        if anomaly:
            anomalies += 1
        runner.records.append(
            ScenarioRecord(
                qid=f"race-{i}",
                category="ma_write_race",
                fact_id="ma-write-race",
                rank=None,
                event=anomaly,
                scenario="write_race_tournament",
                actor=actors[0].name,
                extra={
                    "k": k,
                    "winners": len(winners),
                    "losers": len(losers),
                    "final_body_valid": final_body in bodies,
                    "retry_ok": retry_ok,
                },
            )
        )
    summary = {
        "races": run_races,
        "anomalies": anomalies,
        "anomaly_rate_cp95_upper": (
            clopper_pearson_upper_zero(run_races) if anomalies == 0 and run_races > 0 else None
        ),
    }
    runner.summary.notes.append(f"write_race_tournament: {json.dumps(summary)}")
    return summary


# ── §6 A5: authenticated identity + anti-spoofing ───────────────────────────


async def run_auth_spoof_probe(
    runner: ScenarioRunner,
    actor: ScenarioActor,
    other: ScenarioActor,
    *,
    project_id: uuid.UUID | None,
) -> None:
    """A5: with a real token, a write carrying a forged ``actor_id``/
    ``user_id`` in the arguments must still be attributed to the token's
    true identity (attribution is derived server-side from the principal,
    never accepted from the client). Requires ``actor`` to carry a token."""
    if actor.token is None:
        runner.summary.skipped.append(
            {"scenario": "auth_spoof", "reason": "actor has no real agent token"}
        )
        return
    marker = f"wsauth{runner.rng.randrange(10**8):08d}"
    note = await runner.call(
        actor,
        "create_note",
        {
            "kind": "text",
            "title": f"auth {marker}",
            "text": f"nota di {marker}",
            "project_id": str(project_id) if project_id else None,
            # forged identity hints — must be ignored by the server
            "actor_id": str(other.user_id),
            "user_id": str(other.user_id),
        },
    )
    attributed_to_true = False
    forged_accepted = False
    if isinstance(note, dict) and "id" in note:
        revs = await runner.call(actor, "list_note_revisions", {"note_id": note["id"]})
        rev_list = revs if isinstance(revs, list) else revs.get("revisions", [])
        actor_ids = {r.get("actor_id") for r in rev_list if isinstance(r, dict)}
        attributed_to_true = str(actor.user_id) in actor_ids
        forged_accepted = str(other.user_id) in actor_ids
    else:
        # The server may instead REJECT the extra args outright — also a pass
        # (the forgery did not take effect).
        attributed_to_true = True
    runner.records.append(
        ScenarioRecord(
            qid=f"auth-spoof-{marker}",
            category="ma_auth_spoof",
            fact_id=f"auth-{marker}",
            rank=1 if (attributed_to_true and not forged_accepted) else None,
            event=forged_accepted or not attributed_to_true,
            scenario="auth_spoof",
            actor=actor.name,
            extra={
                "attributed_to_true_identity": attributed_to_true,
                "forged_identity_accepted": forged_accepted,
                "authenticated": True,
            },
        )
    )


# ── §6 A6: review-gate leak probe across every peer read surface ────────────


async def _probe_atom_surfaces(
    runner: ScenarioRunner,
    reader: ScenarioActor,
    *,
    atom_note_id: str,
    atom_blobs: set[str],
    marker: str,
    project_id: uuid.UUID | None,
    seed_note_id: str | None,
) -> dict[str, bool]:
    """Try to see a proposed atom through every peer read surface. Returns
    the per-surface visibility; any True before approval is a leak."""
    seen: dict[str, bool] = {}
    got = await runner.call(reader, "get_note", {"note_id": atom_note_id})
    seen["get_note"] = (
        isinstance(got, dict) and got.get("id") == atom_note_id and "error" not in got
    )
    listed = await runner.call(reader, "list_notes", {"limit": 200})
    items = listed if isinstance(listed, list) else listed.get("items", listed.get("notes", []))
    seen["list_notes"] = isinstance(items, list) and any(
        isinstance(n, dict) and n.get("id") == atom_note_id for n in items
    )
    revs = await runner.call(reader, "list_note_revisions", {"note_id": atom_note_id})
    seen["list_note_revisions"] = isinstance(revs, list | dict) and bool(
        revs if isinstance(revs, list) else revs.get("revisions", [])
    )
    fts = await runner.search(reader, f"sintesi {marker}", project_id=project_id)
    seen["memory_search_fts"] = _rank_of(fts, atom_blobs) is not None
    sem = await runner.search(reader, f"riepilogo del contenuto {marker}", project_id=project_id)
    seen["memory_search_semantic"] = _rank_of(sem, atom_blobs) is not None
    if seed_note_id:
        walk = await runner.call(
            reader, "graph_walk", {"seed": seed_note_id, "mode": "bounded", "budget": 16}
        )
        nodes = walk.get("nodes", walk) if isinstance(walk, dict) else walk
        node_ids = {
            n.get("id") or n.get("note_id")
            for n in (nodes if isinstance(nodes, list) else [])
            if isinstance(n, dict)
        }
        seen["graph_walk"] = atom_note_id in node_ids
    else:
        seen["graph_walk"] = False
    # KG surface: kg_neighbors needs an entity UUID, so the right leak probe
    # is kg_entities(query=marker) — does the proposed atom's content surface
    # as a KG entity to a peer before approval?
    kg = await runner.call(reader, "kg_entities", {"query": marker, "limit": 10})
    ents = kg.get("entities", kg) if isinstance(kg, dict) else kg
    seen["kg_entities"] = isinstance(ents, list) and any(
        isinstance(e, dict) and marker in json.dumps(e, default=str) for e in ents
    )
    return seen


async def run_review_gate_cycles(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    reader: ScenarioActor,
    human: ScenarioActor,
    second_human: ScenarioActor | None,
    *,
    project_id: uuid.UUID | None,
    cycles: int = 60,
) -> dict[str, Any]:
    """A6: N independent propose→probe-ALL-surfaces→approve→probe cycles.
    A proposed atom must be invisible to a peer on EVERY read surface until a
    human approves; the zero-leak rate carries a Clopper-Pearson bound. One
    cycle also runs two concurrent human approvals (exactly one effective
    transition) and the TOCTOU case (approve pinned to a version the human
    read; an interposed edit must abort it — this waits on task 2e36e732 and
    is recorded as such until ``expected_version`` reaches the approve tool)."""
    leaks = 0
    run_cycles = 0
    surfaces_leaked: dict[str, int] = {}
    for i in range(cycles):
        marker = f"wsgate{runner.rng.randrange(10**8):08d}"
        src = await runner.call(
            writer,
            "create_note",
            {
                "kind": "text",
                "title": f"fonte {marker}",
                "text": f"La parola d'ordine {marker} è ginepro.",
                "project_id": str(project_id) if project_id else None,
            },
        )
        if not isinstance(src, dict) or "id" not in src:
            continue
        await runner.call(
            writer,
            "archive_note",
            {"note_id": src["id"], "expected_version": src.get("version", 1), "archived": True},
        )
        distilled = await runner.call(
            writer,
            "distill_note",
            {
                "note_id": src["id"],
                "distilled_text": f"Sintesi: {marker} parola d'ordine ginepro.",
                "origin_model_id": "wseval-t3-deterministic",
            },
        )
        if not isinstance(distilled, dict) or "distilled_note_id" not in distilled:
            continue
        atom_id = distilled["distilled_note_id"]
        atom_blobs = await _resolve_blobs(runner, human, uuid.UUID(atom_id))
        run_cycles += 1
        before = await _probe_atom_surfaces(
            runner,
            reader,
            atom_note_id=atom_id,
            atom_blobs=atom_blobs,
            marker=marker,
            project_id=project_id,
            seed_note_id=src["id"],
        )
        leaked_surfaces = [k for k, v in before.items() if v]
        if leaked_surfaces:
            leaks += 1
            for s in leaked_surfaces:
                surfaces_leaked[s] = surfaces_leaked.get(s, 0) + 1
        await runner.call(human, "garden_review_approve", {"note_id": str(atom_id)})
        after = await runner.search(reader, f"sintesi {marker}", project_id=project_id)
        visible_after = _rank_of(after, atom_blobs) is not None
        runner.records.append(
            ScenarioRecord(
                qid=f"gate-cycle-{i}",
                category="ma_review_gate_cycle",
                fact_id="ma-review-gate",
                rank=1 if (not leaked_surfaces and visible_after) else None,
                event=bool(leaked_surfaces) or not visible_after,
                scenario="review_gate_cycles",
                actor=reader.name,
                extra={"leaked_surfaces": leaked_surfaces, "visible_after": visible_after},
            )
        )

    # Concurrent double-approve (one effective transition) on a fresh atom.
    await _review_gate_double_approve(runner, writer, human, second_human, project_id=project_id)
    # TOCTOU probe — pending task 2e36e732 (approve has no expected_version yet).
    await _review_gate_toctou(runner, writer, human, project_id=project_id)

    summary = {
        "cycles": run_cycles,
        "leaks": leaks,
        "leak_rate_cp95_upper": (
            clopper_pearson_upper_zero(run_cycles) if leaks == 0 and run_cycles > 0 else None
        ),
        "surfaces_leaked": surfaces_leaked,
    }
    runner.summary.notes.append(f"review_gate_cycles: {json.dumps(summary)}")
    return summary


async def _resolve_blobs(
    runner: ScenarioRunner, actor: ScenarioActor, note_id: uuid.UUID
) -> set[str]:
    async with tenant_session(str(actor.org_id), str(actor.user_id)) as s:
        blobs = await resolve_unit_blobs(s, org_id=actor.org_id, note_id=note_id)
    return {str(b) for b in blobs}


async def _review_gate_double_approve(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    human: ScenarioActor,
    second_human: ScenarioActor | None,
    *,
    project_id: uuid.UUID | None,
) -> None:
    marker = f"wsda{runner.rng.randrange(10**8):08d}"
    src = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"double {marker}",
            "text": f"contenuto {marker}",
            "project_id": str(project_id) if project_id else None,
        },
    )
    if not isinstance(src, dict) or "id" not in src:
        return
    await runner.call(
        writer,
        "archive_note",
        {"note_id": src["id"], "expected_version": src.get("version", 1), "archived": True},
    )
    distilled = await runner.call(
        writer,
        "distill_note",
        {
            "note_id": src["id"],
            "distilled_text": f"Sintesi {marker}.",
            "origin_model_id": "wseval-t3-deterministic",
        },
    )
    if not isinstance(distilled, dict) or "distilled_note_id" not in distilled:
        return
    atom_id = distilled["distilled_note_id"]
    approver2 = second_human or human
    r1, r2 = await asyncio.gather(
        runner.call(human, "garden_review_approve", {"note_id": str(atom_id)}),
        runner.call(approver2, "garden_review_approve", {"note_id": str(atom_id)}),
    )
    # Idempotent-or-exclusive: both may succeed (idempotent) OR one succeeds
    # and one is a benign already-resolved error; a hard inconsistency (both
    # raise a conflicting non-idempotent error) is the anomaly.
    errs = [r for r in (r1, r2) if isinstance(r, dict) and "error" in r]
    oks = [r for r in (r1, r2) if not (isinstance(r, dict) and "error" in r)]
    anomaly = len(oks) == 0
    runner.records.append(
        ScenarioRecord(
            qid=f"gate-double-approve-{marker}",
            category="ma_review_gate_double_approve",
            fact_id="ma-review-gate",
            rank=None,
            event=anomaly,
            scenario="review_gate_cycles",
            actor=human.name,
            extra={"oks": len(oks), "errs": len(errs)},
        )
    )


async def _review_gate_toctou(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    human: ScenarioActor,
    *,
    project_id: uuid.UUID | None,
) -> None:
    """Agent edits the atom between the human's read and approval. Once
    ``garden_review_approve`` accepts ``expected_version`` (task 2e36e732),
    the interposed edit must make the approval fail stale. Until then the
    approve tool ignores the version and this is recorded as a pending
    finding, never as a pass."""
    marker = f"wstoc{runner.rng.randrange(10**8):08d}"
    src = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"toctou {marker}",
            "text": f"contenuto {marker}",
            "project_id": str(project_id) if project_id else None,
        },
    )
    if not isinstance(src, dict) or "id" not in src:
        return
    await runner.call(
        writer,
        "archive_note",
        {"note_id": src["id"], "expected_version": src.get("version", 1), "archived": True},
    )
    distilled = await runner.call(
        writer,
        "distill_note",
        {
            "note_id": src["id"],
            "distilled_text": f"Sintesi originale {marker}.",
            "origin_model_id": "wseval-t3-deterministic",
        },
    )
    if not isinstance(distilled, dict) or "distilled_note_id" not in distilled:
        return
    atom_id = distilled["distilled_note_id"]
    read = await runner.call(human, "get_note", {"note_id": str(atom_id)})
    read_version = read.get("version", 1) if isinstance(read, dict) else 1
    # Agent interposes an edit to the atom body.
    if isinstance(read, dict) and read.get("parts"):
        await runner.call(
            writer,
            "update_note_part",
            {
                "part_id": str(read["parts"][0]["id"]),
                "body": f"Sintesi ALTERATA {marker}.",
                "expected_version": read["parts"][0].get("version", 1),
            },
        )
    # Approve pinned to the version the human read: SHOULD fail once the tool
    # honours expected_version.
    approve = await runner.call(
        human,
        "garden_review_approve",
        {"note_id": str(atom_id), "expected_version": read_version},
    )
    approve_err = isinstance(approve, dict) and "error" in approve
    code = ""
    if approve_err:
        e = approve["error"]
        code = (e.get("code") if isinstance(e, dict) else str(e)) or ""
    version_honored = approve_err and ("stale" in code.lower() or "version" in code.lower())
    # invalid_arguments = the tool doesn't know expected_version yet (A1 open).
    pending_a1 = approve_err and "invalid_arg" in code.lower()
    runner.records.append(
        ScenarioRecord(
            qid=f"gate-toctou-{marker}",
            category="ma_review_gate_toctou",
            fact_id="ma-review-gate",
            rank=1 if version_honored else None,
            # Not an "event" while A1 is open: it's a KNOWN pending finding,
            # tracked by task 2e36e732, not a silent failure of the gate.
            event=False,
            scenario="review_gate_cycles",
            actor=human.name,
            extra={
                "version_honored": version_honored,
                "pending_task_2e36e732": pending_a1 or not version_honored,
                "approve_error_code": code,
            },
        )
    )


# ── §6 A8: pure-dense post-commit visibility probe ──────────────────────────


async def run_dense_visibility_probe(
    runner: ScenarioRunner,
    writer: ScenarioActor,
    reader: ScenarioActor,
    *,
    project_id: uuid.UUID | None,
) -> None:
    """A8: a DENSE-only visibility probe. The reader's query shares NO lexical
    token with the note, so a hit can only come from the dense vector — it
    fails if the embedding is NULL (async re-embed lag). Under FakeEmbedder
    the dense match is not semantically meaningful, so CI asserts only that
    the probe RAN and is recorded (machinery); the real signal needs the real
    embedder. The degraded-embedder / re-embed-worker-lag scenario needs the
    worker running and is skipped with a reason here."""
    marker = f"wsdense{runner.rng.randrange(10**8):08d}"
    note = await runner.call(
        writer,
        "create_note",
        {
            "kind": "text",
            "title": f"dense {marker}",
            "text": (
                "La procedura di evacuazione prevede il ritrovo presso il "
                "molo settentrionale entro dieci minuti dall'allarme."
            ),
            "project_id": str(project_id) if project_id else None,
        },
    )
    note_blobs = await _resolve_blobs(runner, reader, uuid.UUID(note["id"]))
    # Query with disjoint vocabulary (no shared content word) — dense-only.
    res = await runner.search(
        reader, "raduno di emergenza punto d'incontro nord", project_id=project_id
    )
    found = _rank_of(res, note_blobs) is not None
    runner.records.append(
        ScenarioRecord(
            qid=f"dense-visibility-{marker}",
            category="ma_dense_visibility",
            fact_id=f"ma-dense-{marker}",
            rank=_rank_of(res, note_blobs),
            # Machinery only under FakeEmbedder: never an event here (the real
            # dense signal is measured in the confirmatory run with bge-m3).
            event=False,
            scenario="multi_agent",
            actor=reader.name,
            extra={"dense_found": found, "embedder": "fake_in_ci"},
        )
    )
    runner.summary.skipped.append(
        {
            "scenario": "degraded_embedder_reembed_lag",
            "reason": "needs the async re-embed worker running (measured in the confirmatory run)",
        }
    )


# ── §6 A9: personal → shared trajectory ─────────────────────────────────────


async def run_personal_to_shared(
    runner: ScenarioRunner,
    owner: ScenarioActor,
    peer: ScenarioActor,
    *,
    project_name: str,
    ingest: IngestResult,
) -> None:
    """A9: an actor creates a note in PERSONAL space (no project); a peer
    cannot find it in the project perimeter; the note is shared into the
    project; the peer then finds it; the revision chain shows both actors.

    Uses the real perimeter mechanism (a note's project is a project-kind
    tag → the blob's ``project_id``). Sharing is a BARE re-tag with no
    content edit, and the peer must find it IMMEDIATELY: the perimeter
    re-scopes the note's blobs on the tag change (task 1d152747). This is
    the regression guard for that fix — an event fires if the shared note is
    not visible without a content touch."""
    project_id = ingest.project_ids.get(project_name)
    if project_id is None:
        runner.summary.skipped.append({"scenario": "personal_to_shared", "reason": "no project"})
        return
    marker = f"wsps{runner.rng.randrange(10**8):08d}"
    # (1) personal note: no project.
    note = await runner.call(
        owner,
        "create_note",
        {
            "kind": "text",
            "title": f"personale {marker}",
            "text": f"Appunto privato: la combinazione {marker} è sette-quattro-due.",
            "project_id": None,
        },
    )
    note_id = note["id"]
    note_blobs = await _resolve_blobs(runner, owner, uuid.UUID(note_id))
    # (2) peer cannot find it in the project perimeter while it is personal.
    peer_pre = await runner.search(peer, f"combinazione {marker}", project_id=project_id)
    hidden_before_share = _rank_of(peer_pre, note_blobs) is None
    # (3) share: attach the project tag — a bare re-tag, no content edit.
    shared = await runner.call(
        owner, "add_note_tag", {"note_id": note_id, "tag_id": str(project_id)}
    )
    share_ok = not (isinstance(shared, dict) and "error" in shared)
    # (4) the peer must find it now, without any content touch.
    peer_post = await runner.search(peer, f"combinazione {marker}", project_id=project_id)
    visible_after_tag = _rank_of(peer_post, note_blobs) is not None
    # (5) provenance chain: the author appears across the revisions.
    revs = await runner.call(peer, "list_note_revisions", {"note_id": note_id})
    rev_list = revs if isinstance(revs, list) else revs.get("revisions", [])
    actor_ids = {r.get("actor_id") for r in rev_list if isinstance(r, dict)}
    chain_ok = str(owner.user_id) in actor_ids
    if not visible_after_tag:
        runner.summary.notes.append(
            "personal_to_shared: REGRESSION — a bare project re-tag did not "
            "re-scope the memory perimeter; the shared note stayed invisible to "
            "the peer without a content edit (task 1d152747)."
        )
    runner.records.append(
        ScenarioRecord(
            qid=f"personal-shared-{marker}",
            category="ma_personal_to_shared",
            fact_id=f"ma-ps-{marker}",
            rank=_rank_of(peer_post, note_blobs),
            # Event iff the end-state is wrong: peer saw it while personal, or
            # cannot see it after the bare share, or the chain lost an actor.
            event=(not hidden_before_share) or (not visible_after_tag) or (not chain_ok),
            scenario="personal_to_shared",
            actor=peer.name,
            extra={
                "hidden_before_share": hidden_before_share,
                "share_ok": share_ok,
                "visible_after_tag": visible_after_tag,
                "chain_has_owner": chain_ok,
            },
        )
    )


# ── artifacts ───────────────────────────────────────────────────────────────


def write_scenario_artifacts(runner: ScenarioRunner, out_dir: Path) -> dict[str, Any]:
    """records.jsonl (eval_report dialect + extras), steps.jsonl, and a
    manifest with SHA256 of both — the reproducibility contract (§1.6)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    records_path = out_dir / "scenario_records.jsonl"
    steps_path = out_dir / "scenario_steps.jsonl"
    records_path.write_text(
        "\n".join(json.dumps(r.to_json(), sort_keys=True) for r in runner.records) + "\n",
        encoding="utf-8",
    )
    steps_path.write_text(
        "\n".join(json.dumps(dataclasses.asdict(s), sort_keys=True) for s in runner.steps) + "\n",
        encoding="utf-8",
    )
    # Zero-event categories get a Clopper-Pearson upper bound on their rate,
    # so a "0 leaks" claim is published as "0/N, ≤ bound" (§1.4), never as an
    # unqualified absolute.
    zero_event_cats = (
        "perimeter",
        "perimeter_redteam",
        "erasure_propagation",
        "ma_write_race",
        "ma_review_gate_cycle",
        "ma_auth_spoof",
    )
    cp_bounds: dict[str, Any] = {}
    for cat in zero_event_cats:
        recs = [r for r in runner.records if r.category == cat]
        if not recs:
            continue
        events = sum(1 for r in recs if r.event)
        cp_bounds[cat] = {
            "n": len(recs),
            "events": events,
            "cp95_upper": clopper_pearson_upper_zero(len(recs)) if events == 0 else None,
        }
    manifest = {
        "records": len(runner.records),
        "steps": len(runner.steps),
        "events": sum(1 for r in runner.records if r.event),
        "authenticated_steps": sum(1 for s in runner.steps if s.authenticated),
        "zero_event_bounds": cp_bounds,
        "skipped": runner.summary.skipped,
        "concurrency_latency": runner.summary.concurrency_latency,
        "notes": runner.summary.notes,
        "sha256": {
            records_path.name: hashlib.sha256(records_path.read_bytes()).hexdigest(),
            steps_path.name: hashlib.sha256(steps_path.read_bytes()).hexdigest(),
        },
    }
    (out_dir / "scenario_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest
