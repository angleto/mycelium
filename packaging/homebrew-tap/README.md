# homebrew-tap (angleto/tap)

Homebrew tap for the Flow personal-productivity tools. The canonical
contents live in this directory; the actual tap repository at
`github.com/angleto/homebrew-tap` is a mirror updated from CI.

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
| `Formula/flow-cli.rb` | `cli/` (monorepo) | Installs the `flow` binary in an isolated Python venv under `libexec`. |

## Cutting a release

1. Bump `version` in `cli/pyproject.toml` and commit.
2. Tag the release: `git tag cli-v0.1.1 && git push --tags`.
3. Wait for the GitHub release tarball to be available.
4. Run `dist/homebrew-tap/bin/release-formula 0.1.1`; copy the printed
   `url` + `sha256` into `Formula/flow-cli.rb`.
5. If dependencies changed, refresh Python resources:
   ```sh
   pipx run homebrew-pypi-poet -f flow-cli > /tmp/flow-cli-resources.rb
   ```
   then paste the `resource "..." do ... end` blocks into the formula.
6. `brew tap --force angleto/tap` and `brew install --build-from-source angleto/tap/flow-cli`
   locally to smoke-test before pushing.
7. Push the updated formula to `github.com/angleto/homebrew-tap`.

## Why not `homebrew-core`?

The formula is opinionated (AGPL, narrow audience, fast-moving) so
homebrew-core would reject it on size + popularity grounds anyway.
A personal tap keeps install one-line for users without the friction.
