"""One declaration per operation, bound by REST and MCP (ADR-0068).

An operation is what the system can do, independent of how it is reached:
its id, its domain, whether it reads or writes, who may call it, the shape of
its arguments and the shape of its answer. The REST adapter and the MCP
adapter each *bind* an operation to a transport -- an HTTP verb and path, a
tool name -- and keep only what is genuinely theirs: how the caller is
authenticated, how the session is opened, how the answer is wrapped.

Everything the two surfaces used to declare twice by hand is declared here
once and read from here by both:

- the scope an operation costs (both scope maps derive their entry for a
  bound route or tool from ``access``; a hand-written entry for a bound name
  is refused at import);
- the argument model, validated once, so a malformed id is a domain error
  with a code on both surfaces instead of a traceback on one of them;
- the read model, so a field cannot exist on one surface and not the other
  by accident;
- the domain and kind ``search_tools`` and the MCP tool annotations report.

An operation reachable from one surface only says so in its declaration
(``only=`` and ``why=``), where a reviewer reads it, instead of in a list
nobody opens.
"""

from __future__ import annotations

import enum
import importlib
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import HUMAN_ONLY, VALID_SCOPE_KEYS, SurfaceGate


class Kind(enum.Enum):
    """What an operation does to the workspace. ``DESTRUCTIVE`` is a write
    whose effect cannot be undone from the product (a purge), not a write
    that merely removes something restorable."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class Gate(enum.Enum):
    """Access values that are not a scope key and not ``HUMAN_ONLY``."""

    #: Callable under any scope, the empty one included: the questions a
    #: client must be able to ask before it knows what it may do.
    META = "meta"


META = Gate.META

#: A scope key, an any-of set of keys, ``HUMAN_ONLY`` or ``META``.
Access = str | frozenset[str] | SurfaceGate | Gate

Surface = Literal["rest", "mcp"]

#: Domains an operation may declare. Grows as domains move onto the
#: registry; ``search_tools`` reads a bound tool's domain from here.
DOMAINS: frozenset[str] = frozenset({"garden", "notes", "tasks"})

#: The modules that declare operations. Importing one registers its
#: operations; :func:`load_all` imports every one, which is what a parity
#: check needs before it can say what is unbound.
OPERATION_MODULES: tuple[str, ...] = (
    "mycelium_core.operations.garden",
    "mycelium_core.operations.notes",
    "mycelium_core.operations.tasks",
)


@dataclass(frozen=True, slots=True)
class OpCtx:
    """What an operation receives from the transport that called it.

    ``actor_id`` is the user the credential acts for. ``agent_token_id`` is
    set when the credential is an agent token, on either surface, so an
    operation that records who CARRIED OUT an act can tell an assistant
    executing a person's decision from the person acting directly."""

    session: AsyncSession
    org_id: uuid.UUID
    actor_id: uuid.UUID
    project_id: uuid.UUID | None = None
    agent_token_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Operation:
    id: str
    domain: str
    kind: Kind
    access: Access
    params: type[BaseModel]
    result: Any
    fn: Callable[[OpCtx, Any], Awaitable[Any]]
    only: Surface | None = None
    why: str | None = None

    @property
    def doc(self) -> str:
        """The operation's description, the one text both surfaces publish."""
        return inspect.cleandoc(self.fn.__doc__ or "")

    @property
    def summary(self) -> str:
        return self.doc.strip().splitlines()[0] if self.doc.strip() else self.id

    def parse(self, raw: Mapping[str, Any]) -> BaseModel:
        """The typed arguments, or a domain error naming what is wrong.

        Raised as ``UnprocessableError`` so it reaches a REST client as a 422
        and an MCP client as ``{"error": {"code": ...}}``, the envelope every
        other refusal already uses."""
        try:
            return self.params.model_validate(dict(raw))
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(x) for x in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors(include_url=False)
            )
            raise UnprocessableError(
                MessageCode.OPERATION_INVALID_ARGUMENT, operation=self.id, detail=detail
            ) from None

    async def __call__(self, ctx: OpCtx, params: BaseModel) -> Any:
        return await self.fn(ctx, params)

    def dump(self, value: Any) -> Any:
        """The JSON shape of a result, as both surfaces serialise it."""
        return _adapter(self.result).dump_python(value, mode="json")


_ADAPTERS: dict[int, TypeAdapter[Any]] = {}


def _adapter(tp: Any) -> TypeAdapter[Any]:
    key = id(tp)
    if key not in _ADAPTERS:
        _ADAPTERS[key] = TypeAdapter(tp)
    return _ADAPTERS[key]


REGISTRY: dict[str, Operation] = {}


def _check_access(op_id: str, access: Access) -> None:
    if access is HUMAN_ONLY or access is META:
        return
    keys = access if isinstance(access, frozenset) else {access}
    unknown = sorted(str(k) for k in keys if k not in VALID_SCOPE_KEYS)
    if unknown:
        raise ValueError(f"operation {op_id}: access names unknown scope keys {unknown}")


def operation(
    op_id: str,
    *,
    domain: str,
    kind: Kind,
    access: Access,
    params: type[BaseModel],
    result: Any,
    only: Surface | None = None,
    why: str | None = None,
) -> Callable[[Callable[[OpCtx, Any], Awaitable[Any]]], Callable[[OpCtx, Any], Awaitable[Any]]]:
    """Declare an operation. Refused at import, not at call time: an
    unknown domain or scope key, a duplicate id, a one-surface operation
    with no reason, or a non-async function."""

    def register(
        fn: Callable[[OpCtx, Any], Awaitable[Any]],
    ) -> Callable[[OpCtx, Any], Awaitable[Any]]:
        if op_id in REGISTRY:
            raise ValueError(f"operation {op_id} is declared twice")
        if domain not in DOMAINS:
            raise ValueError(f"operation {op_id}: unknown domain {domain!r}")
        if not op_id.startswith(domain + "."):
            raise ValueError(f"operation {op_id}: id must start with its domain {domain!r}")
        if (only is None) != (why is None):
            raise ValueError(f"operation {op_id}: 'only' and 'why' go together")
        if not inspect.iscoroutinefunction(fn):
            raise ValueError(f"operation {op_id}: must be an async function")
        _check_access(op_id, access)
        REGISTRY[op_id] = Operation(
            id=op_id,
            domain=domain,
            kind=kind,
            access=access,
            params=params,
            result=result,
            fn=fn,
            only=only,
            why=why,
        )
        return fn

    return register


def get(op_id: str) -> Operation:
    load_all()
    try:
        return REGISTRY[op_id]
    except KeyError:
        raise KeyError(f"no operation {op_id!r}") from None


def load_all() -> dict[str, Operation]:
    for module in OPERATION_MODULES:
        importlib.import_module(module)
    return REGISTRY


async def executor_identity(ctx: OpCtx) -> uuid.UUID | None:
    """The AI-assistant identity behind the calling agent token, or None for
    a person's session and for a bare token with no assistant. Resolved the
    same way for both surfaces, so the audit names the same executor
    whichever door the assistant came through."""
    if ctx.agent_token_id is None:
        return None
    from mycelium_core.models.agent_token import AgentToken
    from mycelium_core.models.ai_assistant import AiAssistant
    from mycelium_core.models.identity import Identity

    row = await ctx.session.execute(
        select(Identity.id)
        .join(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
        .join(AgentToken, AgentToken.assistant_id == AiAssistant.id)
        .where(AgentToken.id == ctx.agent_token_id, Identity.org_id == ctx.org_id)
    )
    return row.scalar_one_or_none()


__all__ = [
    "DOMAINS",
    "META",
    "OPERATION_MODULES",
    "REGISTRY",
    "Access",
    "Gate",
    "Kind",
    "OpCtx",
    "Operation",
    "Surface",
    "executor_identity",
    "get",
    "load_all",
    "operation",
]
