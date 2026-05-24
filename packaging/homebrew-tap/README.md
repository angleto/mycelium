# homebrew-tap (angleto/tap)

Homebrew tap for the Flow personal-productivity tools. The canonical
contents live in this directory; the actual tap repository at
`github.com/angleto/homebrew-tap` is a mirror, refreshed by the
[`mirror-homebrew-tap`](../../.github/workflows/mirror-homebrew-tap.yml)
GitHub Actions workflow on every `cli-v*` tag.

## Usage

```sh
brew tap angleto/tap
brew install flow-cli
```

Or in one line:

```sh
brew install angleto/tap/flow-cli
```

## What's here

| Formula | Source | Notes |
| --- | --- | --- |
| `Formula/flow-cli.rb` | `cli/` (monorepo) | Installs the `flow` binary in an isolated Python venv under `libexec`. Requires `rust` as a build-only dep (for `pydantic-core`). |

## Cutting a release

1. Bump `version` in `cli/pyproject.toml` and commit.
2. Tag the release: `git tag cli-v0.1.1 && git push --tags`.
3. Once the GitHub release tarball is available, get its URL + sha256:
   ```sh
   packaging/homebrew-tap/bin/release-formula 0.1.1
   ```
   Paste the printed `url` + `sha256` into `Formula/flow-cli.rb`.
4. If runtime deps changed, refresh the `resource` blocks. The standard
   `homebrew-pypi-poet` invocation cannot resolve `flow-cli` from PyPI
   (we don't publish there), so we install it locally and run `poet`
   against its dependency closure. Note that recent `setuptools` (>=81)
   dropped `pkg_resources`, so pin it:
   ```sh
   uv venv /tmp/poet-env --python 3.12
   VIRTUAL_ENV=/tmp/poet-env uv pip install \
     homebrew-pypi-poet 'setuptools<81' /path/to/flow/cli
   /tmp/poet-env/bin/poet --resources typer \
     --also rich --also httpx --also platformdirs \
     --also tomli-w --also pydantic > /tmp/flow-cli-resources.rb
   ```
   Then replace the `resource "..." do ... end` blocks in the formula
   with the contents of `/tmp/flow-cli-resources.rb`.
5. Smoke locally:
   ```sh
   cd packaging/homebrew-tap
   git init && git add -A && git commit -m smoke   # brew needs a git repo
   brew tap --force angleto/tap "file://$PWD"
   HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
     brew install --build-from-source angleto/tap/flow-cli
   flow --version && flow auth status               # round-trip
   brew test angleto/tap/flow-cli                   # formula's own test block
   rm -rf .git && brew untap angleto/tap            # cleanup
   ```
6. Commit the updated formula on the monorepo and push the tag. The
   `mirror-homebrew-tap` workflow then syncs `packaging/homebrew-tap/`
   into `github.com/angleto/homebrew-tap` automatically.

## Why not `homebrew-core`?

The formula is opinionated (AGPL, narrow audience, fast-moving) so
homebrew-core would reject it on size + popularity grounds anyway.
A personal tap keeps install one-line for users without the friction.
