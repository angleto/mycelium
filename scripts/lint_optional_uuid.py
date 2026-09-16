"""An optional argument must not be parsed as if it were required.

The MCP tools take ids as strings and convert them by hand, so a tool
whose argument is optional has to say so twice: once in the signature,
once at the conversion. ``set_task_state`` and ``task_leases_list`` said
it once. ``uuid.UUID(None)`` raises ``TypeError`` in the prologue of the
tool, before any domain rule runs, and the caller sees a Python message
with no error code in it -- which is what an agent saw on 2026-09-16
when no MCP session could move a task at all.

Why a script, and not the type checker or ruff: typeshed annotates
``uuid.UUID.__init__`` as ``hex: str | None = None``, so passing an
optional string through is well-typed and mypy is right to accept it.
The obligation being checked is not a typing one. It is that this
codebase converts at the adapter boundary, which makes ``None`` a value
the conversion has to handle rather than one the caller cannot supply.

What it does not cover: a conversion of something that is not a bare
parameter name (a dict lookup, an attribute, a local rebound from an
optional), and a parameter that is optional in fact but annotated as
required. Both are outside the shape that broke, and widening to reach
them is what turns a quiet check into one somebody disables.

Usage: ``python scripts/lint_optional_uuid.py [path ...]`` -- with no
argument, every first-party source tree. Prints one line per finding and
exits 1 when there is any.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The trees that convert at a boundary. Tests are excluded on purpose: a
# test that builds a bad call deliberately is not a defect.
DEFAULT_TREES = ("api/src", "cli/src", "core/src", "mcp/src", "sdi-inbound/src", "worker/src")


def _optional_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters that can arrive as ``None``: a ``None`` default, or an
    annotation admitting ``None``."""
    a = fn.args
    out: set[str] = set()
    positional = a.posonlyargs + a.args
    with_defaults = positional[len(positional) - len(a.defaults) :]
    for arg, default in zip(with_defaults, a.defaults, strict=True):
        if isinstance(default, ast.Constant) and default.value is None:
            out.add(arg.arg)
    for arg, kwdefault in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        if isinstance(kwdefault, ast.Constant) and kwdefault.value is None:
            out.add(arg.arg)
    for arg in positional + a.kwonlyargs:
        ann = arg.annotation
        if (
            isinstance(ann, ast.BinOp)
            and isinstance(ann.op, ast.BitOr)
            and isinstance(ann.right, ast.Constant)
            and ann.right.value is None
        ):
            out.add(arg.arg)
    return out


def _terminates(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Raise, ast.Return, ast.Continue))


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _uuid_calls(fn: ast.AST) -> list[ast.Call]:
    """``uuid.UUID(...)`` and a bare imported ``UUID(...)``, which are the
    two spellings this codebase uses."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        named_uuid = (isinstance(called, ast.Attribute) and called.attr == "UUID") or (
            isinstance(called, ast.Name) and called.id == "UUID"
        )
        if named_uuid:
            out.append(node)
    return out


def check_source(path: str, src: str) -> list[str]:
    findings: list[str] = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        optional = _optional_params(fn)
        if not optional:
            continue
        # A name is guarded from a point onward by an earlier statement
        # that tests it and leaves the function, and locally by a
        # conditional that tests it and encloses the conversion.
        early: dict[str, int] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and (_terminates(node.body) or _terminates(node.orelse)):
                for name in _names_in(node.test) & optional:
                    early[name] = min(early.get(name, node.lineno), node.lineno)
        enclosing: list[tuple[ast.AST, set[str]]] = [
            (node, _names_in(node.test) & optional)
            for node in ast.walk(fn)
            if isinstance(node, (ast.If, ast.IfExp))
        ]
        for call in _uuid_calls(fn):
            arg = call.args[0] if call.args else None
            if not (isinstance(arg, ast.Name) and arg.id in optional):
                continue
            if arg.id in early and early[arg.id] < call.lineno:
                continue
            if any(arg.id in names and call in set(ast.walk(node)) for node, names in enclosing):
                continue
            findings.append(
                f"{path}:{call.lineno}: {fn.name}() converts optional "
                f"'{arg.id}' with an unguarded UUID(); "
                f"write UUID({arg.id}) if {arg.id} else None"
            )
    return findings


def _sources(trees: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for tree in trees
        for path in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _label(path: Path) -> str:
    """Repo-relative where that is meaningful, as given otherwise: the
    check is also pointed at a file outside the tree, which is how its
    own red case runs."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] if argv else _sources(DEFAULT_TREES)
    findings: list[str] = []
    for path in paths:
        findings += check_source(_label(path), path.read_text())
    for line in findings:
        print(line)
    if findings:
        print(f"\n{len(findings)} unguarded conversion(s) of an optional argument.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
