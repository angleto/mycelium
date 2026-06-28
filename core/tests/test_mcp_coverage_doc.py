"""``docs/mcp-coverage.md`` is half generated, half curated (task c4f8b534).

These guards make the three stale-doc failure modes the audit found
un-shippable:

1. the tool inventory is generated from the live registry and must not
   drift from it (the doc claimed 140 then 186 tools against a live 235);
2. the headline tool count must equal ``len(_catalog())``;
3. every tool named in the discovery decision table must actually be
   registered (the old doc routed agents at tools that did not exist / had
   moved, telling them a capability was missing).

The generator is a standalone script (``scripts/gen_mcp_coverage.py``); it
is loaded here by path so the same render/splice logic backs both the CI
``--check`` gate and this test.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import mycelium_mcp.server  # noqa: F401  -- registers every @mcp.tool on the registry
from mycelium_mcp.gateway import _catalog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_PATH = _REPO_ROOT / "scripts" / "gen_mcp_coverage.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_mcp_coverage", _GEN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inventory_block_is_not_stale() -> None:
    """The committed doc must equal a fresh render — i.e. ``--check`` passes.

    This is the drift guard: add/rename/remove a tool without rerunning
    ``make mcp-coverage`` and this fails (in the local suite; the CI quality
    job runs the same check)."""
    gen = _load_generator()
    current = gen.DOC_PATH.read_text(encoding="utf-8")
    assert gen.BEGIN in current and gen.END in current
    desired = gen.splice(current, gen.render_inventory())
    assert current == desired, "docs/mcp-coverage.md is stale — run `make mcp-coverage` and commit."


def test_headline_count_matches_registry() -> None:
    gen = _load_generator()
    text = gen.DOC_PATH.read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+) tools across (\d+) domains\.\*\*", text)
    assert m, "headline '**N tools across M domains.**' not found in the doc"
    assert int(m.group(1)) == len(_catalog())


def _decision_table_tools(text: str) -> set[str]:
    """Tool names from the *Tool* column of the discovery decision table.

    Parses only the markdown table rows between the decision-table heading
    and the generated block, taking the second column, so parameter tokens
    (`state_id=`, `q=`) and surrounding prose are never mistaken for tools.
    """
    start = text.index("## Discovery decision table")
    end = text.index("<!-- BEGIN GENERATED")
    tools: set[str] = set()
    for line in text[start:end].splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3:
            continue
        col = cells[2]  # cells[0] is the empty pre-pipe slice; [2] is "Tool"
        if col in ("Tool", "---"):
            continue
        tools.update(re.findall(r"`([a-z][a-z0-9_]+)`", col))
    return tools


def test_decision_table_tools_are_registered() -> None:
    gen = _load_generator()
    text = gen.DOC_PATH.read_text(encoding="utf-8")
    registered = {m["name"] for m in _catalog()}
    table_tools = _decision_table_tools(text)
    assert table_tools, "no tools parsed from the decision table"
    missing = sorted(table_tools - registered)
    assert not missing, f"decision table routes at unregistered tools: {missing}"
    # The canonical routes the audit called out must be present (regression
    # on the 'told a capability does not exist' defect).
    assert {"list_tasks", "search", "list_notes", "memory_search"} <= table_tools
