#!/usr/bin/env python3
"""Generate the MCP tool inventory in ``docs/mcp-coverage.md`` from the live
registry, so tool counts and per-domain listings never drift from the code.

The doc is half curated (the discovery decision table, the scope model and
the gap notes) and half generated (the inventory between the two HTML
markers below). This script owns ONLY the generated block: it introspects
``mycelium_mcp.server.mcp`` -- the same registry the gateway's ``_catalog()``
iterates -- and re-renders the block, leaving everything outside the markers
untouched.

Usage::

    python scripts/gen_mcp_coverage.py            # rewrite the block in place
    python scripts/gen_mcp_coverage.py --check     # exit 1 if the doc is stale

``--check`` is the CI gate (``.github/workflows/ci.yml``) and the local
``make mcp-coverage-check``; it neither needs a database nor an embedder --
importing the server only registers tool callables on an in-memory FastMCP.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mycelium_mcp.server  # noqa: F401  -- import registers every @mcp.tool on the registry
from mycelium_mcp.gateway import _DOMAIN_RULES, _catalog

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "mcp-coverage.md"

BEGIN = "<!-- BEGIN GENERATED: mcp tool inventory (scripts/gen_mcp_coverage.py) -->"
END = "<!-- END GENERATED -->"

# Canonical domain order: the gateway's structural buckets, then "misc".
_DOMAIN_ORDER: tuple[str, ...] = (*(d for d, _ in _DOMAIN_RULES), "misc")


def render_inventory() -> str:
    """Render the inventory block body (between, not including, the markers).

    Deterministic: domains in the gateway's canonical order, tools sorted by
    name within each domain. Pipe characters in summaries are escaped so a
    docstring first line never breaks the markdown table.
    """
    cat = _catalog()
    by_domain: dict[str, list[dict[str, str]]] = {}
    for meta in cat:
        by_domain.setdefault(meta["domain"], []).append(meta)
    domains = [d for d in _DOMAIN_ORDER if d in by_domain]

    lines: list[str] = [
        f"**{len(cat)} tools across {len(domains)} domains.** "
        "This inventory is generated from the live registry by "
        "`scripts/gen_mcp_coverage.py` — do not edit by hand; run "
        "`make mcp-coverage` to refresh. The one-line summary is each tool's "
        "first docstring line, so it cannot drift from the code.",
        "",
    ]
    for domain in domains:
        tools = sorted(by_domain[domain], key=lambda m: m["name"])
        lines.append(f"### {domain} ({len(tools)})")
        lines.append("")
        lines.append("| Tool | Summary |")
        lines.append("|---|---|")
        for meta in tools:
            summary = meta["summary"].replace("|", "\\|").strip()
            lines.append(f"| `{meta['name']}` | {summary} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def splice(doc: str, block: str) -> str:
    """Return ``doc`` with the text between the markers replaced by ``block``."""
    start = doc.index(BEGIN) + len(BEGIN)
    end = doc.index(END)
    return doc[:start] + "\n" + block + doc[end:]


def _read_doc() -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(
            f"{DOC_PATH} is missing the generated-block markers "
            f"({BEGIN!r} / {END!r}); add them before generating."
        )
    if text.index(BEGIN) > text.index(END):
        raise SystemExit(f"{DOC_PATH}: BEGIN marker must precede END marker.")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero (without writing) if the doc block is stale",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="rewrite the block in place (the default action; accepted for clarity)",
    )
    args = ap.parse_args()

    current = _read_doc()
    desired = splice(current, render_inventory())

    if args.check:
        if current != desired:
            print(
                "docs/mcp-coverage.md is stale: the generated tool inventory no "
                "longer matches the live registry.\n"
                "Run `make mcp-coverage` (or `python scripts/gen_mcp_coverage.py`) "
                "and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("docs/mcp-coverage.md inventory is up to date.")
        return 0

    if current == desired:
        print("docs/mcp-coverage.md already up to date.")
    else:
        DOC_PATH.write_text(desired, encoding="utf-8")
        print(f"Rewrote the generated inventory in {DOC_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
