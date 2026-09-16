"""The check that an optional argument is not parsed as a required one.

``scripts/lint_optional_uuid.py`` exists because the invariant broke
twice in one release, in ``set_task_state`` and in ``task_leases_list``,
and neither mypy nor ruff can see it: typeshed annotates
``uuid.UUID.__init__`` as ``hex: str | None = None``, so handing an
optional string straight through is well-typed. The consequence was that
no MCP session could move a task at all.

A check nobody has seen fail is not a check, so the red case is the
incident rather than a synthetic one: the guards are stripped back out of
the live ``mcp/src/mycelium_mcp/server.py`` and the check must report
both sites. Run against 12e7b245, the revision that shipped the defect,
it printed exactly this, which is what the perturbation reproduces:

    server.py:1819: set_task_state() converts optional 'worker_id' ...
    server.py:3607: task_leases_list() converts optional 'worker_id' ...

Perturbing the live file rather than reading that revision back is what
keeps the case honest in a shallow clone, and it also keeps it pointed at
the code as it is now: rename the argument or drop a guard and this fails
rather than agreeing with a snapshot nobody has looked at since.

The rest of the file is the false-positive floor. A noisy check gets
disabled, which is worse than never writing it, so the three ways this
codebase already guards a conversion are pinned as silent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "scripts" / "lint_optional_uuid.py"
_MCP_SERVER = _REPO_ROOT / "mcp" / "src" / "mycelium_mcp" / "server.py"
# The guard, and the defect that is the guard with the condition taken
# off. Both sites were written the same way, so one substitution makes
# both defects again.
_GUARD = "(uuid.UUID(worker_id) if worker_id else None)"
_DEFECT = "uuid.UUID(worker_id)"


def _load_linter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lint_optional_uuid", _LINT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_tree_is_clean() -> None:
    """The gate itself: this is what CI runs."""
    lint = _load_linter()
    assert lint.main([]) == 0


def test_the_check_reports_the_defect_it_was_written_for() -> None:
    """The red case: put the defect back and the check must say so."""
    lint = _load_linter()
    live = _MCP_SERVER.read_text()
    # Both guards are still there and still written the same way. If
    # this fails the perturbation below is stripping nothing, and the
    # red case would pass by proving nothing.
    assert live.count(_GUARD) == 2, "the guards this case perturbs have moved"
    findings = lint.check_source("server.py", live.replace(_GUARD, _DEFECT))
    assert len(findings) == 2, findings
    assert "set_task_state() converts optional 'worker_id'" in findings[0]
    assert "task_leases_list() converts optional 'worker_id'" in findings[1]


def test_the_three_guards_this_codebase_uses_are_silent() -> None:
    """Every shape that was a false positive in the first draft.

    The conditional expression is the idiom the MCP tools use; the early
    raise is ``append_note_part`` and the bearer path in ``deps.py``; the
    early return is ``_resolve_project``. A check that flagged these
    would be turned off within a week.
    """
    lint = _load_linter()
    src = """
import uuid

def conditional(tag_id: str | None = None):
    return uuid.UUID(tag_id) if tag_id else None

def early_raise(note_id: str | None = None):
    if note_id is None:
        raise ValueError("anchor required")
    return uuid.UUID(note_id)

def early_return(needle: str | None = None):
    if not needle:
        return None
    return uuid.UUID(needle)

def enclosing_branch(part_id: str | None = None):
    if part_id is not None:
        return uuid.UUID(part_id)
    return None

def required(task_id: str):
    return uuid.UUID(task_id)
"""
    assert lint.check_source("fixture.py", src) == []


def test_an_unguarded_conversion_is_caught_whatever_makes_it_optional() -> None:
    """Optional by default and optional by annotation are both optional.

    A parameter annotated ``str | None`` with no default is the shape a
    keyword-only service signature takes, and it fails the same way.
    """
    lint = _load_linter()
    src = """
import uuid

def by_default(worker_id: str = None):
    return uuid.UUID(worker_id)

def by_annotation(*, worker_id: str | None):
    return uuid.UUID(worker_id)

def guard_comes_after_the_use(worker_id: str | None = None):
    parsed = uuid.UUID(worker_id)
    if worker_id is None:
        raise ValueError("too late")
    return parsed
"""
    findings = lint.check_source("fixture.py", src)
    assert len(findings) == 3, findings
