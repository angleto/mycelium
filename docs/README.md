# Flow, project documentation

Flow is a multi-tenant task and workflow management system that unifies
a task manager, a time tracker, a scheduler, email-to-task, Italian
electronic invoicing (SDI) and a hierarchical memory with semantic
retrieval, with an MCP layer co-equal to the GUI.

Status: requirements and architecture **decided** (post critical
review). Scope: **layered MVP** (everything complete from the start
except SDI invoicing, introduced in phases). Last updated: 2026-05-17.

These documents are the source of truth and supersede any earlier
planning draft.

## Index

- [Context, scope, MVP](context.md)
- [Decisions](decisions.md)
- [Domain model](domain-model.md)
- [Data model](data-model.md)
- [Functional requirements](functional-requirements.md)
- [Non-functional requirements](non-functional-requirements.md)
- [Architecture](architecture.md)
- [Phased roadmap and verification criteria](roadmap.md)
- [References](references.md)
- [Architecture Decision Records](adr/README.md)

## How to read

To understand **what** is built: context, functional and non-functional
requirements. To understand **how**: domain model, data model,
architecture. To understand **why** a non-obvious choice was made (and
which alternatives were rejected): the ADRs. To understand **when**:
the roadmap.

## Non-negotiable principles

- Single service layer in `core/`; `api/` (REST/WS) and `mcp/` are thin
  adapters with no business logic.
- Multi-tenant with hard isolation: mandatory RLS, and for memory
  per-(org, project) isolation, never relevance only.
- Optimistic concurrency, no last-write-wins.
- Propose the correct architectural solution, not the most convenient
  one; the correct choices already made must not be regressed (see the
  ADRs).
