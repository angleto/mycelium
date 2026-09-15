# Contributing

## Project language (rule)

- English everywhere: code, identifiers, comments, docstrings, docs,
  ADRs, commit messages. See [ADR-0017](docs/adr/0017-english-only-i18n-message-catalog.md).
- No hardcoded user-facing strings. Use the message catalog
  (`mycelium_core/i18n.py`): a stable `MessageCode` + params. Domain errors
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
make web-check  # the SPA: lint, shared purity, i18n, button ink, tsc -b, vitest
make extension-check  # the browser extension: lint, messages, tokens, tsc -b, vitest
```

The Python targets do not reach the interface. The two `-check` targets run
the same sequences CI's `web` and `extension` jobs do, in the same order, so
a green local run means the same thing a green job does; the browser suite
(`pnpm e2e` in `web/`) needs the stack up and is not part of either.

### The database `make test` runs against

`make test` needs one, and the suite **refuses to start** against a database
that has not been declared disposable. That is not ceremony: the fixtures
create organizations, and one of them rewrites the target's function ACLs to
reproduce the production execute posture. Neither is undone.

```
make test-db-up     # throwaway container, roles, migrations, ACLs, marker
make test           # about 31 minutes on a clean database
make test-db-down
```

`test-db-up` prints the three variables to export. **Do not point the suite
at the local stack below**: `make up` is a database whose contents you would
miss, and the refusal exists because the natural way to recover from "nothing
listening on 5432" is to look for the Postgres that is running and find that
one.

Two things worth knowing before the first run, both measured. The suite takes
about 31 minutes on a virgin database, so it is not hung. And the rows
accumulate: the same suite on the same container a second time took 51
minutes, and on a development database after eight runs it had not finished in
90. Recreate the container per run rather than reusing it.

To mark a different throwaway database by hand:

```
psql "$MYCELIUM_DATABASE_URL_SYNC" -f deploy/local/mark_test_database.sql
```

## Local stack

```
make up                                   # postgres+pgvector, redis (arm64)
MYCELIUM_DB_APP_PASSWORD=... make db-bootstrap # runtime role + password
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
under the [Mycelium Contributor License Agreement](CLA.md). The CLA
grants the maintainer the rights needed to release the project
under both AGPL-3.0-or-later and a separate commercial license
(see [NOTICE](NOTICE) and [LICENSE](LICENSE)). The DCO alone is
not sufficient for that, because under DCO each contributor retains
copyright in their contribution and only licenses it under the
project's then-current license; without the CLA the maintainer
cannot relicense third-party contributions to commercial customers.

Acceptance is one-time per contributor. The first pull request from
a contributor must include the exact line

    I accept the Mycelium CLA (CLA.md)

in its description or in the body of its top commit. Subsequent
contributions from the same contributor are covered automatically
until acceptance is revoked in writing (see CLA.md section 8).

Pull requests without CLA acceptance will be asked to add it before
merge.
