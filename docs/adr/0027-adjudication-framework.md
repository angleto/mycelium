# ADR-0027: Adjudication framework for multi-agent convergence

Status: Proposed
Date: 2026-05-23
Relates to: ADR-0025 (work orchestration, P1-P5 already shipped), ADR-0012
(LLM/Embedder abstraction), ADR-0019 (metering/credits), ADR-0017
(English-only), ADR-0005 (memory).

## Context

ADR-0025 P1-P5 covers **dispatch**: who does what, when, with which
budget, and how the artifact flows to the next executor via the
contract-net handoff on the coordination blackboard. It does not cover
**convergence**: given a single decision with two or more agents
producing differing answers, how to arbitrate between them, when to
ask for a second opinion, how to surface dissent, how to escalate to
the user only when warranted.

Today Mycelium handles the multi-agent case implicitly: either one agent
runs and its artifact is the answer (single-shot), or `approval_required`
forces a human decision. The middle band, "let the agents argue and
converge, escalate only on irreducible deadlock", does not exist.

External benchmark (`ruvnet/ruflo`, ~54k stars, MIT TypeScript) was
reviewed. It defines a four-protocol enum (`neural_voting`,
`iterative_refinement`, `auction`, `contract_net`) in ADR-038 but the
concrete implementations we inspected
(`v3/plugins/prime-radiant/src/tools/consensus-verify.ts`,
`v3/@claude-flow/guidance/src/adversarial.ts:MemoryQuorum`,
`v3/@claude-flow/swarm/src/consensus/{raft,byzantine,gossip}.ts`) are
**single-shot voting / distributed-consensus primitives**, not the
cross-visibility deliberation loop. The reusable design ideas are
coherence-energy via embedding similarity and quorum-threshold voting,
not the code (different stack: TypeScript + WASM, not portable as a
dependency into Mycelium's Python/FastAPI).

Constraints discovered (see `services/agent_runtime.py`,
`services/coordination.py`, `services/dispatch_loop.py`,
`models/agent_run.py`, `models/task_handoff.py`, `embedder.py`,
`ai_providers.py`, `billing.py`):

1. **Agent execution already exists**: bounded ReAct loop, metered,
   killable, RLS-scoped, tool allowlist. We must not re-implement it.
2. **Coordination already exists**: handoff is a typed message bound to
   a DAG edge; contract-net delegation is implemented. We must compose
   with it, not bypass it.
3. **Embedder is prewarmed** (`flow-backend-memory-prewarm`): coherence
   scoring on agent outputs is feasible at low marginal latency.
4. **Governance posture (ADR-0025)**: default `approval_required`,
   credit budgets, billing meter, tenant session + RLS, role checks.
   New mechanisms must inherit these, not invent parallel ones.
5. **English-only (ADR-0017)**: every user-facing message via
   `MessageCode`. Generated agent text is passthrough.
6. **Multiple meaningful arbitration mechanisms exist**: single-shot,
   coherence-vote with escalation, threshold quorum, multi-round
   debate, LLM-as-judge, human-in-loop, contract-net, devil's advocate.
   Picking one and locking out the others is a design loss: different
   decisions need different mechanisms, sometimes composed.

## Decision

### D1. Domain neutral to the strategy

A first-class entity **Adjudication** represents "the process of
reaching a decision on a question", regardless of how. A single
**AdjudicationStep** table records every event in that process, with
a polymorphic `kind` (`turn`, `vote`, `score`, `escalation`,
`synthesis`, `intervention`, `tool_call`). Strategies populate the
kinds they need. UI is one timeline component with kind-specific
renderers. Analytics, audit, export: one query.

### D2. Strategy as a Protocol + registry

```python
class AdjudicationStrategy(Protocol):
    id: ClassVar[str]
    requires: ClassVar[StrategyRequirements]    # min_agents, embedder, human, budget
    composable_with: ClassVar[frozenset[str]]
    mutually_exclusive_with: ClassVar[frozenset[str]]
    cost_model: ClassVar[CostModel]

    def applicable(self, ctx: AdjudicationContext) -> float: ...
    async def run(
        self, ctx: AdjudicationContext, store: StepStore
    ) -> AdjudicationOutcome: ...
```

`applicable` is a soft score `[0, 1]` (0 means not runnable: missing
capability), not a boolean. The router uses it for ranking. `run`
writes steps as a side effect via `StepStore`, never returns a list:
this gives streaming (SSE) to the UI without strategy awareness.

The registry validates at load time: every strategy declares its
`requires` and compatibility set. Strategies are discovered via Python
entry-points group `flow.adjudication.strategies` so out-of-tree
strategies can ship in separate packages without touching core.

Same pattern Mycelium already uses for `LLMProvider`/`EmbedderProvider`
(ADR-0012): `typing.Protocol`, DB/env-driven factory, neutral DTOs.

### D3. Policy router, declarative, with explicit override

Selection inputs in precedence:

1. **Explicit override** at call site (`POST /adjudications {strategy_id:
   "debate", config:{...}}`).
2. **Declarative rules** (YAML or `adjudication_policy_rule` table):
   `when: task.monetary_cost > 1000 and task.type == 'invoice'` →
   `use: FallbackChain([Debate(n=3, r=2), HumanInLoop()])`.
3. **Applicability auto-rank**: among registered strategies, pick the
   highest `applicable(ctx)`, breaking ties by lowest projected cost.

The router does selection + budget check + capability gating only. It
**never executes domain logic**. Domain logic belongs to strategies.

### D4. Composition as meta-strategies, no DSL

Composition primitives implement the same `AdjudicationStrategy`
protocol, so the registry/router/store are unaware of nesting:

```python
FallbackChain([CoherenceVote(threshold=0.85), Debate(n=3, r=3), HumanInLoop()])
Cap(Debate(n=3, r=5), budget=BudgetCap(tokens=50_000, wall_s=120))
Filter(Debate(n=2, r=2), when=lambda ctx: ctx.stakes >= "medium")
Race([Debate(n=3), HumanInLoop()], stop_on_first=True)
```

`composable_with` / `mutually_exclusive_with` are enforced when a
composition is instantiated: e.g. nesting `MemoryQuorum` and
`Debate` on the same decision is rejected (voting and deliberation
are different epistemic stances).

Rejected explicitly: a YAML/JSON DSL for composition. Python classes
are already expressive enough for the single-user / single-tenant
deployment, and the cost of a DSL parser/validator/serializer is not
justified by reuse savings.

### D5. Strategies shipped in tree, more pluggable

In-tree, by phase (see Phasing):

- `SingleShot` (baseline, no arbitration; metric anchor)
- `HumanInLoop` (synchronous block until user resolves; reuses the
  existing approval-gate primitive)
- `ContractNet` (wrap of the existing P4 contract-net handoff so it
  becomes uniformly selectable from the registry)
- `CoherenceVote` (gather N answers, embedding-similarity centroid,
  escalate if below threshold; ruflo `consensus-verify` pattern,
  ported)
- `MemoryQuorum` (propose/vote/resolve with configurable threshold;
  ruflo `MemoryQuorum` pattern, ported)
- `Debate` (multi-round with cross-visibility + judge + convergence
  detector; novel, see ADR-0027 child ticket 3)
- `LLMJudge` (single judge agent reads context and decides; baseline
  for comparison against `Debate`)

Out of tree via entry-points: anything else (rule-based domain
experts, external services, future provider integrations).

### D6. Governance reuses ADR-0025 primitives, no parallel channels

- **Spend**: every step that invokes an LLM goes through
  `billing.meter_if_billable` against the caller's executor
  `credit_budget`. A free local model costs nothing; a premium model
  deducts credits. A per-adjudication cap is enforced as a `Cap`
  wrapper.
- **Authority**: `run` executes inside the caller's `tenant_session`
  with `require_role`; the adjudication cannot exceed the human's
  permissions (same posture as `agent_runtime`).
- **Approval**: `HumanInLoopStrategy` *is* the approval gate, not a
  parallel one. The existing approval-gate code becomes its
  implementation.
- **Tool allowlist**: strategies that spawn agent runs do so via
  `agent_runtime.start_run`, inheriting its hard allowlist. The
  adjudication layer does not have its own tool catalog.

### D7. Outcome shape: decision + confidence + residual dissent

Every `AdjudicationOutcome` carries `(decision_payload,
confidence: float, residual_dissent: list[DissentNote])`. Confidence
is not optional. A judge that produces a "synthesis that pleases
everyone" without explicit residual dissent is treated as a bug,
not a feature: the judge prompt must surface unresolved disagreement
even when synthesising.

## Schema

Two new tables. No breaking changes to existing entities.

```sql
adjudication(
  id              uuid pk,
  org_id          uuid not null,                  -- RLS tenant
  task_id         uuid null,                      -- nullable: not all
                                                  -- adjudications bind to a task
  question_text   text not null,
  context_json    jsonb not null default '{}',
  strategy_id     text not null,                  -- registered strategy id
  strategy_config jsonb not null default '{}',
  status          adjudication_status not null,   -- running|resolved|escalated|aborted
  outcome_json    jsonb null,                     -- {decision, confidence, dissent[]}
  confidence      numeric(4,3) null,              -- denormalised for indexing
  cost_tokens     bigint not null default 0,
  cost_wall_ms    bigint not null default 0,
  started_at      timestamptz not null default now(),
  ended_at        timestamptz null,
  created_by      uuid not null,                  -- user id
  version         integer not null default 0      -- optimistic concurrency
);

adjudication_step(
  id              uuid pk,
  adjudication_id uuid not null references adjudication on delete cascade,
  step_no         integer not null,
  kind            adjudication_step_kind not null, -- turn|vote|score|escalation
                                                   -- |synthesis|intervention|tool_call
  payload_json    jsonb not null,
  agent_id        text null,                       -- executor id, free-form
  embedding       vector(EMBED_DIM) null,          -- when kind produces text
  created_at      timestamptz not null default now(),
  unique (adjudication_id, step_no)
);

-- optional, can stay YAML-only initially
adjudication_policy_rule(
  id              uuid pk,
  org_id          uuid not null,
  condition_expr  text not null,
  strategy_spec   jsonb not null,                  -- serialised composition tree
  priority        integer not null default 100,
  enabled         boolean not null default true,
  version         integer not null default 0
);
```

RLS on all three tables per ADR-0002 (`org_id`-scoped, optimistic
concurrency via `version`). Indexes: `(org_id, task_id)`,
`(adjudication_id, step_no)`, `(org_id, status)`.

## Integration points (existing Mycelium code)

- `services/agent_runtime.py` → strategies that need an LLM call
  invoke `start_run` (or a lighter sibling) per turn; bounded loop,
  metering, killability come for free.
- `services/coordination.py` → `ContractNetStrategy` is a thin wrapper
  over `offer_task`/`claim_task`; same primitive, registered surface.
- `services/dispatch_loop.py` → can opt to adjudicate before dispatch
  (e.g. "which candidate executor for this task?" as an
  Adjudication) but the dispatcher itself does not depend on
  adjudication; clean DAG.
- `embedder.py` → required by `CoherenceVote` and `Debate` convergence
  detector; `requires.needs_embedder=True` makes the dependency
  explicit.
- `billing.py` → unchanged; strategies meter exactly like
  `agent_runtime` does today.
- `i18n.py` / `MessageCode` → strategy-internal status messages,
  escalation prompts.

## Phasing

- **P1 (this ADR)**: design, schema, contract. No code.
- **P2** (figlio ticket 2): `mycelium_core/adjudication/` skeleton:
  `base.py` (Protocol + DTOs), `registry.py` (entry-points discovery),
  `policy.py` (declarative router), `store.py` (StepStore against
  DB), `composition.py` (`FallbackChain`, `Cap`, `Filter`, `Race`),
  `strategies/single_shot.py`, `strategies/human_in_loop.py`,
  `services/adjudication.py` (start/get/stream), Alembic migration
  `0082_adjudication_tables.py`, tests. Pre-flight green.
- **P3** (figlio ticket 3): `strategies/debate.py` (multi-round,
  cross-visibility, stance enum, judge agent, convergence detector
  via embedding coherence + `changed_mind` stability), tests.
- **P4** (future ticket): `strategies/coherence_vote.py`,
  `strategies/memory_quorum.py`, `strategies/llm_judge.py`,
  `strategies/contract_net.py` (wrap of existing P4 handoff),
  telemetry (per-strategy outcome quality, cost, time).
- **P5** (future ticket): UI timeline polymorphic by `kind` with
  three primary renderers (turn, vote, score) + secondary
  (escalation, synthesis, intervention).
- **P6** (optional, far future): policy router learning loop fed
  by telemetry from P4 (per-domain strategy selection trained on
  outcome-quality feedback). Not committed.

## Consequences

- One coherent surface to invoke any arbitration mechanism:
  selection by override / rule / capability auto-rank.
- Strategies coexist (composable where epistemically consistent) and
  are alternatives where not (`composable_with` /
  `mutually_exclusive_with` enforced at instantiation).
- Schema cost: two new tables, no migration of existing data.
- Governance reuses ADR-0025 primitives end-to-end; no second
  spending channel, no second approval primitive.
- The framework is opt-in: existing callers that do not adjudicate
  keep working. Adjudication is a service callable from MCP, REST,
  scheduler, or another strategy.
- Risk: lowest-common-denominator Protocol. Mitigated by keeping the
  Protocol minimal (two methods + metadata) and pushing capability
  needs into `requires`.
- Risk: registry as dependency hell. Mitigated by a hard rule: no
  import edges between `strategies/*.py`; strategies talk only to
  `StepStore`, `LLMProvider`, `EmbedderProvider`, `agent_runtime`,
  `HumanGateway`.
- Risk: router god-object. Mitigated by keeping the router to
  selection + budget check + capability gating; merit logic stays in
  strategies.

## Alternatives considered

- **A. Pick one algorithm (e.g. multi-agent debate) and hardcode it.**
  Rejected: different decisions need different mechanisms. Hardcoding
  collapses the design space and replicates ruflo's enum-of-protocols
  shape, which is exactly the layer the reviewed code does not
  generalise from.
- **B. Depend on `ruvnet/ruflo` as a library.** Rejected: stack
  mismatch (TypeScript + WASM vs Python/FastAPI), interop cost
  exceeds reimplementation cost (~200-300 LOC Python for the patterns
  we actually want), and ruflo's concrete code is single-shot
  primitives, not the loop we need.
- **C. Custom YAML/JSON DSL for composition.** Rejected: over-engineered
  for single-user. Python classes compose perfectly and serialise to
  JSON for the policy-rule table if persistence is needed later.
- **D. Bake adjudication into `agent_runtime` directly.** Rejected:
  `agent_runtime` is the *executor* of one agent run; adjudication is
  *meta* over multiple runs. Conflating them re-creates the
  "task-centric vs conversational" mismatch that ADR-0026 had to fix.
- **E. Single judge LLM, no debate loop.** Kept as `LLMJudge` strategy
  (P4) but not as the framework default: literature (Du et al. 2023,
  Liang et al. 2023) shows debate beats single-judge on reasoning
  benchmarks at small N, R.
