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
`feat(scheduler): add working-calendar CPM pass`).

## Developer Certificate of Origin (DCO)

To preserve the project's ability to evolve its licensing later (for
example to offer a dual license, or to relicense under another
OSI-compatible license), every commit must be signed off under the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).

The sign-off is a single trailer line at the end of the commit message:

```
Signed-off-by: Jane Doe <jane@example.com>
```

Add it automatically with:

```
git commit -s
```

By signing off you certify that you wrote the patch yourself, or
otherwise have the right to submit it under the project's license, as
stated in the DCO. The name and email must match a real identity (no
anonymous or pseudonymous sign-offs) and should match your
`git config user.email`.

Pull requests with unsigned commits will be asked to rebase with
sign-offs before merge.
