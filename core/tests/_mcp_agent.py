"""Call MCP tools the way an agent token reaches them: over HTTP.

The bearer middleware (``server_http``) authenticates the agent token and
publishes the principal and its scope list for the duration of the request;
the tools then read them instead of their ``token`` argument. stdio is a
person's transport and refuses an agent token, so a test that passes one
positionally is testing a door that does not exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mycelium_core.services import agent_tokens as agent_tokens_svc
from mycelium_mcp.server import _PRINCIPAL, _PRINCIPAL_SCOPE


@asynccontextmanager
async def as_agent(raw: str) -> AsyncIterator[None]:
    principal = await agent_tokens_svc.authenticate(raw)
    assert principal is not None, "the agent token did not authenticate"
    reset = _PRINCIPAL.set((principal.user_id, principal.org_id, principal.token_id))
    reset_scope = _PRINCIPAL_SCOPE.set(principal.assistant_scope)
    try:
        yield
    finally:
        _PRINCIPAL_SCOPE.reset(reset_scope)
        _PRINCIPAL.reset(reset)
