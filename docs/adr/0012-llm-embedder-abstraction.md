# ADR-0012 LLM/Embedder abstraction, reuse the bitvision pattern

Status: accepted.

## Context

We need pluggable summarization and embedding: a local model by default
(privacy, the body does not leave the perimeter) but replaceable (e.g.
a SOTA model on a GPU cluster in the future). The user already has a
proven pattern in `bitvision_phoenix`.

## Decision

Reuse the `bitvision_phoenix` pattern: a provider via `typing.Protocol`
(not an ABC), a DB-driven factory, neutral DTOs, pydantic settings with
provider keys from env, a DB model registry with `is_active`.
Reference files in [references.md](../references.md). bitvision has NO
embedding abstraction (direct calls): Flow adds an `EmbedderProvider`
mirroring `LLMProvider` (`embed_text`, `dim`, `model_id`). Default: a
small multilingual CPU/ARM model; the concrete choice is made at
implementation among strong open-source candidates (BGE-M3,
multilingual-E5, GTE-multilingual, Qwen3-Embedding) on the current
multilingual MTEB, not a "best" fixed a priori. `model_id`+`dim` per
blob enable re-embedding (ADR-0005).

## Consequences

- Consistency with an already-validated pattern; model replacement
  without redesigning the core.
- Do not copy from bitvision: the Anthropic-specific `ephemeral` cache,
  clinical templates, DICOM handling, medical MCP token scoping.

## Alternatives rejected

- A new ad-hoc abstraction: reinvents a pattern that already works for
  the user.
- A cloud-only model: the email body would leave the perimeter,
  incompatible with the privacy posture (local default).
