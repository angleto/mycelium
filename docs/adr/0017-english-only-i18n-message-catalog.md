# ADR-0017 English-only project language; i18n-ready message catalog

Status: accepted. Project rule (user directive).

## Context

The project must be publishable. Mixed-language code/docs are not
publishable, and hardcoded user-facing strings block future
internationalization. Decision needed before the codebase grows.

## Decision

- **English everywhere**: code, identifiers, comments, docstrings,
  documentation (`docs/`, ADRs), commit messages. Single project
  language now; i18n added later, additively.
- **No hardcoded user-facing strings**. User-facing messages go through
  a catalog (`flow_core/i18n.py`): a stable machine `MessageCode` plus
  parameters. Domain errors carry `code` + `params`, never display
  text. Adapters (api/mcp) resolve the locale (e.g. `Accept-Language`,
  default `en`) and render via the catalog. Adding a locale = adding a
  catalog table; no code changes.
- API error responses are `{"code", "detail"}`; tests assert the
  stable `code`, not the text.

## Consequences

- Errors/messages are locale-agnostic at the core; only adapters know a
  locale. i18n is a later, isolated addition.
- Existing Italian artifacts (docs, ADRs, some comments) must be
  translated to English (tracked task).
- Lint/type/test gates stay: `ruff`, `mypy --strict`, `pytest`.

## Alternatives rejected

- Italian or bilingual project: not publishable; raises contributor
  friction.
- Inline message strings with later "extraction": extraction never
  happens cleanly; blocks i18n and stable error contracts.
