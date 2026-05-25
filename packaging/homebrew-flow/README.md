# homebrew-flow (angleto/flow)

Homebrew tap for the Flow personal-productivity tools. The canonical
contents live in this directory; the actual tap repository at
`github.com/angleto/homebrew-flow` is a public mirror, refreshed by the
[`mirror-homebrew-flow`](../../.github/workflows/mirror-homebrew-flow.yml)
GitHub Actions workflow on every `v*` tag of the monorepo.

## Usage

```sh
brew tap angleto/flow
brew install flow-cli
```

Or in one line:

```sh
brew install angleto/flow/flow-cli
```

## What's here

| Formula | Source | Notes |
| --- | --- | --- |
| `Formula/flow-cli.rb` | `cli/` (monorepo) | **Template**. Installs the `flow` binary in an isolated Python venv under `libexec`. Requires `rust` as a build-only dep (for `pydantic-core`). |
| `bin/render-formula` | — | Resolves the template against a concrete `v*` tag (downloads the tarball, fills in `__TAG__` + `__SHA256__`). |

## Versioning

`flow-cli` shares the monorepo's release tag: when you cut Flow `v2.0.6`,
the CLI is also `2.0.6`. Single source of truth:

- `cli/pyproject.toml` → `version = "2.0.6"`
- `cli/src/flow_cli/__init__.py` → reads the same string via
  `importlib.metadata.version("flow-cli")` at runtime.

No per-tool version files. Bump only `pyproject.toml`.

## Cutting a release

The monorepo workflow is:

1. Bump `version` in `cli/pyproject.toml` (e.g. `2.0.5` → `2.0.6`).
2. Commit on the default branch.
3. Tag the release: `git tag v2.0.6 && git push --follow-tags`.

That tag push triggers, in parallel:

- `ci.yml` (ruff + mypy + tests),
- `build-images.yml` (Docker images → GHCR),
- [`mirror-homebrew-flow`](../../.github/workflows/mirror-homebrew-flow.yml)
  (renders the formula for `v2.0.6` and pushes it into
  `angleto/homebrew-flow`),
- [`mirror-flow-nvim`](../../.github/workflows/mirror-flow-nvim.yml)
  (mirrors `nvim/flow-nvim/` into `angleto/flow-nvim` and tags it
  `v2.0.6` too).

Users `brew install angleto/flow/flow-cli` and get the new version.
No manual mirror step.

## Local smoke (before tagging)

```sh
cd packaging/homebrew-flow

# Resolve the template against an existing tag (any v* works).
bin/render-formula v2.0.5 > /tmp/flow-cli.rb

# brew now requires a tap to be a git repo; ad-hoc init is fine.
mkdir /tmp/smoke-tap && cd /tmp/smoke-tap
git init -q -b main
mkdir Formula && cp /tmp/flow-cli.rb Formula/
git add -A && git -c user.email=. -c user.name=. commit -q -m smoke

HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
  brew tap --force angleto/flow "file://$PWD"
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
  brew install --build-from-source angleto/flow/flow-cli
flow --version && flow auth status
brew test angleto/flow/flow-cli
brew uninstall flow-cli && brew untap angleto/flow
```

## Refreshing Python resources

The `resource "..." do ... end` blocks pin every transitive Python
dep. They need a refresh only when `cli/pyproject.toml` changes its
direct runtime deps. `homebrew-pypi-poet` cannot resolve `flow-cli`
from PyPI (we don't publish there), so install the CLI locally first.
Recent `setuptools` (>=81) dropped `pkg_resources`, so pin it:

```sh
uv venv /tmp/poet-env --python 3.12
VIRTUAL_ENV=/tmp/poet-env uv pip install \
  homebrew-pypi-poet 'setuptools<81' /path/to/flow/cli
/tmp/poet-env/bin/poet --resources typer \
  --also rich --also httpx --also platformdirs \
  --also tomli-w --also pydantic > /tmp/flow-cli-resources.rb
```

Paste the contents into `Formula/flow-cli.rb`, replacing the existing
`resource "..."` blocks. Re-run the local smoke above to verify the
new closure builds.

## Why not `homebrew-core`?

The formula is opinionated (AGPL, narrow audience, fast-moving) so
homebrew-core would reject it on size + popularity grounds anyway.
A personal tap keeps install one-line for users without the friction.
