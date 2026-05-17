# Contributing

## Project language (rule)

- English everywhere: code, identifiers, comments, docstrings, docs,
  ADRs, commit messages. See [ADR-0017](docs/adr/0017-english-only-i18n-message-catalog.md).
- No hardcoded user-facing strings. Use the message catalog
  (`flow_core/i18n.py`): a stable `MessageCode` + params. Domain errors
  carry `code` + `params`, never display text. Adapters render per
  locale (default `en`); adding a locale is additive.

## Quality gates

Run before committing:

```
make sync       # uv workspace
make lint       # ruff check
make fmt        # ruff format
make type       # mypy --strict
make test       # pytest
```

## Local stack

```
make up                                   # postgres+pgvector, redis (arm64)
FLOW_DB_APP_PASSWORD=... make db-bootstrap # runtime role + password
make migrate                              # alembic upgrade head
```

## Commits

Conventional, English, imperative mood (e.g.
`feat(scheduler): add working-calendar CPM pass`). No
`Co-Authored-By` trailer.
