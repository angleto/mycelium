"""Bind registry operations as REST routes (ADR-0068).

A bound route is generated from its operation: the path and query
parameters (or the request body) from the operation's argument model, the
response model from its read model, the description from its docstring. What
the route declares is what is genuinely HTTP's: the verb, the path, which
fields travel in the path, and the route name the OpenAPI document uses.

Scope: ``route_scopes.bind_operations(app)`` writes the entry of every bound
route from the operation's access when the app is built, and refuses a
hand-written entry for a bound route, so the two can never disagree.
"""

from __future__ import annotations

import inspect
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_core import operations as ops

_PATH_PARAM = re.compile(r"{(\w+)}")

#: The attribute a bound endpoint carries: the id of its operation.
OPERATION_ATTR = "__operation__"


def op_ctx(ctx: TenantCtx) -> ops.OpCtx:
    return ops.OpCtx(
        session=ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        project_id=ctx.project_id,
        agent_token_id=ctx.agent_token_id,
    )


def _field_param(name: str, fi: Any, marker: Any) -> inspect.Parameter:
    extra: list[Any] = [*fi.metadata, marker]
    return inspect.Parameter(
        name,
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Annotated[(fi.annotation, *extra)],
        default=inspect.Parameter.empty if fi.is_required() else fi.get_default(),
    )


def bind_route(router: APIRouter, method: str, path: str, op_id: str, *, name: str) -> None:
    """Expose ``op_id`` at ``method path`` on ``router``.

    A GET takes every argument from the path or the query string. Any other
    verb takes them as one JSON body, the argument model itself, and may not
    also have path parameters (no garden route needs both, and splitting a
    model across the two is the place a field would go missing)."""
    op = ops.get(op_id)
    if op.only == "mcp":
        raise RuntimeError(f"{op_id} is declared MCP-only ({op.why}); it cannot bind {path}")
    method = method.upper()
    in_path = set(_PATH_PARAM.findall(path))
    fields = op.params.model_fields
    missing = sorted(in_path - set(fields))
    if missing:
        raise RuntimeError(f"{path}: path parameters {missing} are not arguments of {op_id}")
    if method != "GET" and in_path:
        raise RuntimeError(f"{method} {path}: a body route cannot also take path parameters")

    params: list[inspect.Parameter] = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
        )
    ]
    if method == "GET":
        for fname, fi in fields.items():
            marker = (
                Path(description=fi.description)
                if fname in in_path
                else Query(description=fi.description)
            )
            params.append(_field_param(fname, fi, marker))
    else:
        params.append(
            inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY, annotation=op.params)
        )

    async def endpoint(**kwargs: Any) -> Any:
        ctx: TenantCtx = kwargs.pop("ctx")
        args = kwargs.pop("body") if "body" in kwargs else op.params.model_validate(kwargs)
        return await op(op_ctx(ctx), args)

    endpoint.__name__ = endpoint.__qualname__ = name
    endpoint.__doc__ = op.doc
    endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    setattr(endpoint, OPERATION_ATTR, op_id)
    router.add_api_route(path, endpoint, methods=[method], response_model=op.result, name=name)


__all__ = ["OPERATION_ATTR", "bind_route", "op_ctx"]
