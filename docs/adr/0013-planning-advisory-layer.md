# ADR-0013 Planning advisory layer, deterministic core

Status: accepted. User's choice (v1 core).

## Context

The product's reason for being is to help with effective planning:
answering questions like "I have half an hour, what useful thing can I
do?", "I'm going to the hardware store, what do I need?", "budget X for
home expenses, what are the priorities?". These are contextual decision
queries, not CRUD.

## Decision

A first-class advisory layer in v1, in the service layer, exposed via
REST + MCP tools. The decision core is **deterministic and
explainable**: a feasibility filter + ranking + constrained selection
(priority knapsack). The LLM/MCP is the **natural-language frontend**:
it translates the request into a structured query, composes with
memory and narrates the result; it is not the oracle that decides.
Three archetypes: what-can-I-do-now (free window), errand/context
(place), prioritization within budget. Verifiable determinism (same
input, same output).

## Consequences

- Consistent with ADR-0004 (deterministic core, LLM as interface, no
  opaque magic): explainability and user trust.
- Built on top of the scheduler (F3), time tracking (F4) and
  personal/budget attributes (ADR-0014); phase F4b.
- Operates on the tasks accessible to the user within an org, even
  multi-project: this is NOT a memory-isolation violation (ADR-0007),
  which governs RAG/email content, not the user's task list. A
  distinction to document and test.

## Alternatives rejected

- A later / post-core layer: it is the product's reason for being;
  deferring it would gut v1.
- LLM-driven decision (ranking/selection left to the model): not
  explainable nor deterministic, inconsistent with ADR-0004.
