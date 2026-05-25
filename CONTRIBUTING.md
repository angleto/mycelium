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

Every commit must be signed off under the
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

## Contributor License Agreement (CLA)

In addition to the DCO sign-off above, contributions are accepted
under the [Flow Contributor License Agreement](CLA.md). The CLA
grants the maintainer the rights needed to release the project
under both AGPL-3.0-or-later and a separate commercial license
(see [NOTICE](NOTICE) and [LICENSE](LICENSE)). The DCO alone is
not sufficient for that, because under DCO each contributor retains
copyright in their contribution and only licenses it under the
project's then-current license; without the CLA the maintainer
cannot relicense third-party contributions to commercial customers.

Acceptance is one-time per contributor. The first pull request from
a contributor must include the exact line

    I accept the Flow CLA (CLA.md)

in its description or in the body of its top commit. Subsequent
contributions from the same contributor are covered automatically
until acceptance is revoked in writing (see CLA.md section 8).

Pull requests without CLA acceptance will be asked to add it before
merge.
