"""Bind registry operations as MCP tools (ADR-0068).

A bound tool is generated from its operation: the input schema is the
operation's argument model, the description its docstring, the annotations
its kind, the scope its access. What stays here is what is genuinely the
transport's: the legacy ``token`` / ``org_id`` parameters, how the session is
opened, and how the answer is wrapped for a client that already reads it in
a given shape.

The tool's scope entry is WRITTEN by the binding, never by hand: binding a
name that ``TOOL_SCOPES`` already lists is refused at import, so the two can
never disagree about what a tool costs.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from mycelium_core import operations as ops
from mycelium_mcp import tool_scopes

#: tool name -> operation id, for every bound tool.
TOOL_OPERATIONS: dict[str, str] = {}

OpenCtx = Callable[[str, str], AbstractAsyncContextManager[ops.OpCtx]]


#: Appended to the description of a tool bound with ``lean_answer=True``, so the
#: rule the reader needs to read the answer travels with it.
LEAN_NOTE = "Fields that are null, false or empty are left out of the answer."


def lean(value: Any, *, top: bool = True, keep: frozenset[str] = frozenset()) -> Any:
    """Drop the keys whose value is null, false or empty, at every depth.

    The cheap default shape of an MCP answer (ADR-0064): an absent key reads
    as its zero value, and a note carries a dozen fields that are null or
    false on most rows. ``0`` is kept: an ``ord`` or a count of zero is
    information, not an absence. So are the empty containers of the answer
    itself (``items``, ``parts``): only those inside a row go. ``keep``
    names top-level keys a contract promises whatever their value, such as
    the ``next_cursor`` and ``truncated`` of every paged list."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = lean(v, top=False)
            empty_container = v == [] or v == {}
            # The answer's own containers stay even when empty: an empty
            # page is ``{"items": []}``, not ``{}``, which a reader of
            # ``items`` would take for a malformed answer.
            if top and k in keep:
                out[k] = v
                continue
            if v is None or v is False or v == "" or (empty_container and not top):
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [lean(v, top=False) for v in value]
    return value


def _parameters(
    op: ops.Operation, defaults: dict[str, Any] | None = None
) -> list[inspect.Parameter]:
    """``token`` and ``org_id`` first, as every hand-written tool declares
    them, then one parameter per field of the argument model, carrying the
    field's bounds and description so the published schema states them."""
    out = [
        inspect.Parameter("token", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        inspect.Parameter("org_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
    ]
    for name, fi in op.params.model_fields.items():
        extra: list[Any] = list(fi.metadata)
        if fi.description:
            extra.append(Field(description=fi.description))
        annotation: Any = Annotated[(fi.annotation, *extra)] if extra else fi.annotation
        if defaults and name in defaults:
            default = defaults[name]
        else:
            default = inspect.Parameter.empty if fi.is_required() else fi.get_default()
        out.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=annotation,
                default=default,
            )
        )
    return out


def bind_tool(
    registry: FastMCP,
    open_ctx: OpenCtx,
    op_id: str,
    *,
    name: str,
    present: Callable[[Any], Any] | None = None,
    defaults: dict[str, Any] | None = None,
    lean_answer: bool = False,
    extras: dict[str, tuple[Any, Any, str]] | None = None,
    lean_keep: frozenset[str] = frozenset(),
) -> Callable[..., Any]:
    """Expose ``op_id`` as the MCP tool ``name``.

    ``present`` reshapes the JSON answer for the tool's existing readers
    (``{"pending": [...]}`` around a list, for instance). It receives the
    operation's dumped result and must not add data the read model does not
    carry: that is how a field reaches one surface and not the other.

    ``defaults`` changes the default of an argument on this surface only,
    where its readers usually want the other value (a model reading
    ``parts`` does not want the joined ``transcript`` too). ``lean_answer``
    leaves out null, false and empty fields, and says so in the
    description.

    ``extras`` are arguments of this surface only, ``{name: (type, default,
    description)}``: they shape the answer (which columns to keep), never
    which rows the operation returns, and they reach ``present`` as keyword
    arguments instead of reaching the operation."""
    op = ops.get(op_id)
    if op.only == "rest":
        raise RuntimeError(f"{op_id} is declared REST-only ({op.why}); it cannot bind {name}")
    if name in TOOL_OPERATIONS:
        raise RuntimeError(f"tool {name} is bound twice")
    if name in tool_scopes.TOOL_SCOPES or name in tool_scopes.DYNAMIC_TOOL_SCOPES:
        raise RuntimeError(
            f"tool {name} binds {op_id}, so its scope comes from the operation: "
            "remove its hand-written TOOL_SCOPES entry"
        )

    unknown = sorted(set(defaults or {}) - set(op.params.model_fields))
    if unknown:
        raise RuntimeError(f"tool {name}: defaults for {unknown}, not arguments of {op_id}")
    params = _parameters(op, defaults)
    for xname, (xtype, xdefault, xdoc) in (extras or {}).items():
        if xname in op.params.model_fields:
            raise RuntimeError(f"tool {name}: extra {xname} shadows an argument of {op_id}")
        params.append(
            inspect.Parameter(
                xname,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Annotated[xtype, Field(description=xdoc)],
                default=xdefault,
            )
        )
    description = f"{op.doc}\n\n{LEAN_NOTE}" if lean_answer else op.doc
    signature = inspect.Signature(params, return_annotation=Any)

    async def tool(*args: Any, **kwargs: Any) -> Any:
        # Positional calls bind like the hand-written tools did, so a Python
        # caller (a test, an eval scenario) can keep calling it either way.
        bound = signature.bind(*args, **kwargs)
        # This surface's defaults, which may differ from the model's.
        bound.apply_defaults()
        kwargs = dict(bound.arguments)
        token = kwargs.pop("token", "")
        org_id = kwargs.pop("org_id", "")
        shaping = {x: kwargs.pop(x) for x in (extras or {})}
        arguments = op.parse(kwargs)
        async with open_ctx(token, org_id) as ctx:
            result = await op(ctx, arguments)
        data = op.dump(result)
        if present is not None:
            data = present(data, **shaping) if extras else present(data)
        return lean(data, keep=lean_keep) if lean_answer else data

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = description
    tool.__signature__ = signature  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": Any}

    registry.tool(
        name=name,
        description=description,
        annotations=ToolAnnotations(
            readOnlyHint=op.kind is ops.Kind.READ,
            destructiveHint=op.kind is ops.Kind.DESTRUCTIVE,
        ),
        structured_output=False,
    )(tool)
    tool_scopes.TOOL_SCOPES[name] = None if op.access is ops.META else op.access
    TOOL_OPERATIONS[name] = op_id
    return tool


__all__ = ["LEAN_NOTE", "TOOL_OPERATIONS", "OpenCtx", "bind_tool", "lean"]
