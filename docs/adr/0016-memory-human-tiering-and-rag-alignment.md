# ADR-0016 Memory: human-like tiering and RAG-pattern alignment

Status: accepted. Refines ADR-0005/0007. Origin: a check against
`docs/rag-architectures.txt` + the user's request for a memory as
hierarchical as human memory.

## Context

ADR-0005 defined hot/warm/cold tiering by age/size. Two inputs: (1) the
5 RAG patterns (hybrid, GraphRAG, agentic, corrective, multimodal);
(2) the idea: memory must be hierarchical like human memory, concepts
that recur often in a fast tier, rare ones in less performant tiers but
always retrievable because they might be important.

## Decision

### Tiering by frequency/recency/importance (human memory)

- The tier (hot/warm/cold) is driven by an **access score** with
  temporal decay (frequency + recency) and an importance signal, no
  longer just age/size.
- **Invariant**: frequency determines only the **latency tier**, never
  retention nor visibility. Cold stays always queryable; a rare but
  relevant concept resurfaces via hybrid retrieval + grader. Low
  frequency != not important.
- **Recurring concepts** (consolidated clusters, provenance preserved
  via `blob_sources`, always within (org, project) and never
  cross-subject) are promoted to a compact, pre-warm tier; decay ->
  demotion, without deletion.

### Alignment with the 5 RAG patterns

- **Hybrid (01)**: already baseline (ADR-0005). Unchanged.
- **GraphRAG (02)**: exploit the **structural** graph already present
  (dependency DAG, tag/client/project hierarchy, email-task link and
  provenance), not an LLM-extracted knowledge graph from text. Textual
  GraphRAG **deferred** (high cost, partly redundant with the typed
  domain).
- **Agentic (03)**: memory retrieval is exposed as **one MCP tool**
  among the deterministic tools; the LLM/MCP planner chooses
  vector/SQL/structured ("retrieval as a plan"). The decision stays
  deterministic (ADR-0004/0013): the LLM orchestrates, it does not
  decide.
- **Corrective / CRAG (04)**: add a **retrieval grader**: a
  deterministic threshold on the fused RRF score + an optional local
  LLM grader. Branches: ok -> use; uncertain -> rewrite/expand the
  query; insufficient -> widen the scope **within the tenant** or
  answer "insufficient evidence". **No web branch**: memory is private
  posture, the web would violate isolation/GDPR. A conscious deviation
  from the reference pattern.
- **Multimodal (05)**: v1 text-first, text extraction from attachments
  into textual memory; true multimodal (CLIP/ColPali, single index)
  **deferred**.

## Consequences

- `memory_blobs` (F6) adds: an access counter/score with decay, an
  importance signal, a concept-cluster reference; the
  promotion/demotion job uses these, not just age.
- The grader is a new retrieval stage (F6); deterministic by default,
  local LLM optional (consistent with the privacy posture).
- No retrieval branch leaves the perimeter (no web on the private).
- ADR-0007 invariants (per (org, project) isolation) and ADR-0005
  (provenance, no cross-subject merge) remain valid and constrain
  concept promotion.

## Alternatives rejected

- Evicting rare concepts to "free space": violates the invariant
  (rare != not important); demote, do not delete.
- Textual GraphRAG in v1: disproportionate cost, redundant with the
  structural graph already available.
- A CRAG "wrong -> web" branch: incompatible with private memory and
  GDPR.
- LLM-driven tiering: not deterministic/explainable (ADR-0004).
