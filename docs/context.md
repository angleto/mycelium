# Context, scope, MVP

## What Flow is

A multi-tenant/team system that unifies five capabilities that are
separate today:

1. A lightweight task manager (the task as the primary unit).
2. A time tracker (timer + manual entries, reports).
3. Task dependencies with a workflow graph and scheduling/Gantt.
4. Multi-account email (read + send) with "mail to task".
5. End-to-end Italian electronic invoicing (SDI).

An MCP layer co-equal to the GUI: same domain logic, two clients.

Terminology: the primary entity is always called **Task** (in informal
discussion sometimes called a "card").

## Goals

- Be a planning copilot: answer contextual advisory queries (what to do
  in a free window, what is needed for an errand/place, priorities
  within a spending budget), with a deterministic decision core and
  LLM/MCP as the natural-language interface.
- Organize information on the user's behalf without mixing contexts: no
  data leak across tenants nor across projects.
- Realistic planning of the user's time: no ubiquity (not two
  concurrent engagements for the same person); tasks the user must do
  in person are not concurrent, unless delegated to an LLM agent.
- Bill tracked time simply, without going through the Agenzia delle
  Entrate portal.

## Scope realism (explicit)

"Everything perfect from day one" is not a v1: these are effectively
several products. The layering is dictated by legal and algorithmic
reality, not by convenience.

### Complete from the start (feasible and correct)

Tasks, taxonomy, configurable workflows, dependencies and graph,
deterministic scheduler, time tracking, appointment-tasks +
no-ubiquity per identity (mig 0094 / ADR-0008 addendum), human/LLM
executor, Gmail + generic IMAP email, memory with per-project
isolation, personal domain + budget, advisory planning assistant, MCP,
multi-tenant with RLS and optimistic concurrency.

### Layered (layered MVP, confirmed choice)

SDI invoicing:

- v1 **B2B/B2C only** at an explicit minimal fiscal profile (TD01/TD04,
  standard rates + a reduced Natura set, optional withholding, stamp
  duty as a flag with manual quarterly export).
- Then the SdICoop channel in a test environment, then in production.
- **Post-v1**: PA/B2G (CAdES/XAdES signature + qualified certificate,
  NE/DT/EC/SE notifications), passive cycle, reverse charge/self-billing
  TD16-TD19, foreign clients, quarterly stamp-duty settlement, CP-SAT
  optimizing leveling.

Proton Mail (via Bridge) after Gmail.

An operational consequence to own consciously: the chosen compliant
conservation is the free AdE service, which requires each tenant's
adhesion in their own tax portal and conserves only what transits SdI.
Invoices issued via the initial manual export are not covered by AdE
and remain the tenant's responsibility until the SdICoop channel is
active. See
[ADR-0010](adr/0010-conservation-ade-free-service.md).
