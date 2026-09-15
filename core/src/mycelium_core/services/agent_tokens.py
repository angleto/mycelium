"""Agent-token service: mint / list / revoke / authenticate.

See ``models/agent_token.py`` and migration 0056 for the rationale.

Raw token format
----------------
``mycelium_at_<43 url-safe chars>``: a fixed ``mycelium_at_``
discriminator prefix that makes the type identifiable to the verifier
without trying JWT decode first, followed by ``secrets.token_urlsafe(32)``
(256 bits of entropy, URL-safe alphabet, no padding).

The stored ``prefix`` column is the first 20 characters of the raw
value (``mycelium_at_`` + 8 chars of random material) -- enough to give a
UI an unambiguous handle and short enough that it does not weaken the
secret.

Mint / revoke are owner-gated (minting credentials is a sensitive
operation; same gate model as the executor / billing tools).
``authenticate`` is the one verifier-side helper; the API surface does
not expose it directly, but the MCP / SPA bearer paths call it via
``decode_token_async``.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken, WorkspaceBinding
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

# Discriminator prefix. The verifier checks this BEFORE attempting JWT
# decode so we can branch on the credential kind without a try/except
# on cryptographic errors. Plain ASCII, never appears in a Mycelium JWT
# (those are ``<base64>.<base64>.<base64>``).
RAW_PREFIX: str = "mycelium_at_"
# Length of the random url-safe portion (in bytes of entropy; the
# resulting string is ~43 chars).
_RAW_ENTROPY_BYTES: int = 32
# How many leading chars of the raw value to persist as the
# (non-secret) ``prefix`` column. 20 = "mycelium_at_" (12) + 8 of randomness.
_PREFIX_CHARS: int = 20
# Default time-to-live for a freshly minted token (1 year, matching
# the design intent of "long-lived but expires by default so a
# forgotten credential is bounded").
DEFAULT_TTL_DAYS: int = 365


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _generate_raw() -> str:
    return f"{RAW_PREFIX}{secrets.token_urlsafe(_RAW_ENTROPY_BYTES)}"


@dataclass(frozen=True, slots=True)
class MintResult:
    token: AgentToken
    # The raw value. Returned exactly once. The caller hands it to the
    # operator and forgets it; the DB only ever holds the hash.
    raw: str


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    token_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    scope: str
    # Populated when the token is bound to an AI assistant (post-0059):
    # ``assistant_id`` identifies the assistant row, ``assistant_scope``
    # is the JSONB list of tool scopes the assistant is allowed to run
    # (the MCP gate uses it to filter @mcp.tool calls). NULL on legacy
    # bare tokens (pre-assistant), which keep their previous all-tools
    # access — the UI funnels new mints through the assistant flow.
    assistant_id: uuid.UUID | None = None
    assistant_scope: list[str] | None = None
    # Which workspaces this credential may act in. Read at
    # authentication time rather than looked up afterwards: a
    # credential's tenancy is part of what authenticating it establishes.
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace


async def mint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    scope: str = "mcp",
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    assistant_id: uuid.UUID | None = None,
    workspace_binding: WorkspaceBinding = WorkspaceBinding.workspace,
    minimum_role: Role = Role.owner,
) -> MintResult:
    """Mint a fresh long-lived bearer token. Owner-gated by default.

    ``ttl_days=None`` disables expiry (a never-expiring credential);
    the default 365 days is a deliberate floor on forgotten secrets.

    ``assistant_id`` binds the token to an ``ai_assistants`` row when
    set (post-migration 0059); NULL keeps the legacy bare-token shape
    for back-compat with pre-assistant integrations.

    ``workspace_binding`` says which workspaces the credential may act
    in; ``org_id`` remains the workspace it was minted in either way.

    ``minimum_role`` is the threshold for MINTING, and the caller passes
    a lower one only where the credential cannot exceed what its holder
    can already do (``ai_assistants.create_assistant``, which derives it
    from the requested scope). It is not a parameter any request
    controls: no route reads it, and the one caller that lowers it
    computes it from the catalogue.
    """
    await require_role(session, org_id, actor_id, minimum_role)
    raw = _generate_raw()
    token_hash = _hash(raw)
    expires_at: datetime.datetime | None = None
    if ttl_days is not None and ttl_days > 0:
        expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=ttl_days)
    row = AgentToken(
        org_id=org_id,
        user_id=actor_id,
        name=name,
        prefix=raw[:_PREFIX_CHARS],
        token_hash=token_hash,
        scope=scope,
        expires_at=expires_at,
        assistant_id=assistant_id,
        workspace_binding=workspace_binding,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_token",
        entity_id=row.id,
        action="mint",
        diff={
            "name": name,
            "scope": scope,
            "assistant_id": str(assistant_id) if assistant_id else None,
            "workspace_binding": workspace_binding.value,
        },
    )
    return MintResult(token=row, raw=raw)


async def list_tokens(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
) -> list[AgentToken]:
    """RLS-scoped listing of all tokens in the current workspace
    (active + revoked, the UI distinguishes via ``revoked_at``).
    Member-level read so an operator can see what the workspace owner
    has minted -- the raw value is never in the row, so visibility is
    safe."""
    result = await session.execute(
        select(AgentToken).where(AgentToken.org_id == org_id).order_by(AgentToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    token_id: uuid.UUID,
) -> None:
    """Owner-gated. Mark a token revoked; idempotent (a re-revoke is a
    no-op that preserves the original ``revoked_at`` timestamp)."""
    await require_role(session, org_id, actor_id, Role.owner)
    result = await session.execute(
        select(AgentToken).where(AgentToken.id == token_id, AgentToken.org_id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.AGENT_TOKEN_NOT_FOUND)
    if row.revoked_at is not None:
        return
    row.revoked_at = datetime.datetime.now(tz=datetime.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_token",
        entity_id=row.id,
        action="revoke",
    )


def is_agent_token(raw: str) -> bool:
    """True iff the bearer looks like an agent token (discriminator
    prefix). Cheap pre-check so the verifier branches without paying a
    failed JWT decode."""
    return raw.startswith(RAW_PREFIX)


async def authenticate(
    raw: str,
    *,
    session: AsyncSession | None = None,
) -> AuthenticatedAgent | None:
    """Resolve a raw agent token to its principal, or ``None`` if it
    is unknown / revoked / expired.

    Crosses the tenant boundary: at the moment the MCP server / API
    bearer authenticator runs, the caller has not yet selected a
    workspace, so RLS would block a direct lookup. The SECURITY
    DEFINER ``authenticate_agent_token`` function (migration 0056)
    does the lookup, validates expiry / revocation, bumps
    ``last_used_at``, and returns the principal -- all while saving
    and restoring the caller's GUCs around the operation.

    ``session`` may be ``None`` (the helper opens its own
    ``admin_session``) or an already-open session whose GUCs the
    function will save & restore around its work. Either is correct;
    keeping it injectable lets the API bearer dep reuse its session.
    """
    if not is_agent_token(raw):
        return None
    token_hash = _hash(raw)
    if session is not None:
        return await _call_authenticate_fn(session, token_hash)
    async with admin_session() as s:
        return await _call_authenticate_fn(s, token_hash)


async def _call_authenticate_fn(
    session: AsyncSession, token_hash: bytes
) -> AuthenticatedAgent | None:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT out_token_id, out_user_id, out_org_id, out_scope, "
            "out_assistant_id, out_assistant_scope, out_workspace_binding "
            "FROM authenticate_agent_token(:h)"
        ),
        {"h": token_hash},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    # The SECURITY DEFINER function gates a token bound to an
    # ``is_active=false`` assistant by returning no row, so by the time
    # we get here either the assistant is active or there is no
    # assistant binding (legacy bare token).
    raw_scope = row[5]
    assistant_scope: list[str] | None
    if raw_scope is None:
        # No assistant binding (legacy bare token) -> no per-tool restriction.
        # This is the ONLY value that may mean "full access".
        assistant_scope = None
    elif isinstance(raw_scope, list):
        assistant_scope = [str(s) for s in raw_scope]
    else:
        # A bound assistant whose scope column is malformed (not a JSONB list).
        # Fail CLOSED -- deny-all -- rather than reusing the None sentinel,
        # which would silently upgrade a corrupt row to full access. Mirrors
        # ``AiAssistant.scope_list()``, which already treats a non-list as no
        # scopes.
        assistant_scope = []
    try:
        binding = WorkspaceBinding(str(row[6]))
    except ValueError:
        # An unrecognised value is a row this code does not understand,
        # and the safe reading of a tenancy it cannot parse is the
        # narrow one. Never the wide one, which is why this is not a
        # default on the enum.
        binding = WorkspaceBinding.workspace
    return AuthenticatedAgent(
        token_id=row[0],
        user_id=row[1],
        org_id=row[2],
        scope=row[3],
        assistant_id=row[4],
        assistant_scope=assistant_scope,
        workspace_binding=binding,
    )


#: Actor kinds that are an assistant acting, whatever subject they carry.
#: ``agent_run`` is the dispatch runtime driving a task, whose subject is
#: an ``agent_runs`` id and therefore resolves to no agent token at all;
#: ``mcp_token`` is a redeemed capability, whose subject is the
#: capability-token id and whose MINTING credential is not reconstructible
#: (``capability_tokens`` records only ``user_id``, and every agent token
#: in this workspace resolves to one user).
_ASSISTANT_ACTOR_KINDS = frozenset({"agent_run", "mcp_token"})


@dataclass(frozen=True, slots=True)
class MinterAuthority:
    """What a minting credential may still do.

    ``scope is None`` means a bare token: bound to no assistant, so no
    per-route restriction. A LIST is the assistant's scope, possibly
    empty (deny-all, the fail-closed reading of a malformed column).

    Returned inside an object rather than as a bare ``list | None``
    because the CALLER has a third case to tell apart -- the credential
    is gone -- and the bare shape invites conflating "gone" with "bare".
    Those two must not agree: one means no restriction, the other means
    the authority behind this grant no longer exists.
    """

    scope: list[str] | None


async def minter_authority(session: AsyncSession, token_id: uuid.UUID) -> MinterAuthority | None:
    """The live authority of the agent token that minted a grant, or None
    when that credential no longer authorises anything -- revoked,
    expired, or bound to a deactivated assistant.

    Used to apply a MINTING credential's authority to a capability it
    handed on (task 1428a184), where the raw token is not available:
    only the id recorded on the grant.

    Takes the caller's TENANT session on purpose. These tables are
    org-scoped and a no-tenant session is fail-closed on them, so the
    same lookup run there would find nothing and -- if None meant "no
    restriction" -- read as full access. A security check that fails open
    because of WHERE it ran is the worst kind, because it passes review.

    Fails CLOSED on a malformed scope column, the same reading
    ``authenticate`` uses: a second normalisation of one column is how
    two answers to one question come to disagree.
    """
    row = (
        await session.execute(
            select(AiAssistant.scope, AgentToken.assistant_id)
            .outerjoin(AiAssistant, AgentToken.assistant_id == AiAssistant.id)
            .where(
                AgentToken.id == token_id,
                AgentToken.revoked_at.is_(None),
                AgentToken.expires_at > datetime.datetime.now(tz=datetime.UTC),
            )
        )
    ).first()
    if row is None:
        return None  # revoked, expired, or gone: authorises nothing
    raw_scope, assistant_id = row
    if assistant_id is None:
        return MinterAuthority(scope=None)  # bare token
    if raw_scope is None:
        return MinterAuthority(scope=None)
    if isinstance(raw_scope, list):
        return MinterAuthority(scope=[str(x) for x in raw_scope])
    return MinterAuthority(scope=[])


async def session_writes_as_assistant(session: AsyncSession) -> bool:
    """Whether this session must be treated as an assistant writing.

    Three shapes, and the last two are the ones a narrower predicate
    would have let through -- which is why this answers the question the
    GUARD asks rather than the one the credential answers:

    1. a bound agent token (``agent_tokens.assistant_id`` NOT NULL);
    2. the dispatch runtime (``actor_kind='agent_run'``), the most
       autonomous writer there is;
    3. a redeemed capability (``actor_kind='mcp_token'``). It refuses
       because the minting credential cannot be recovered, so the
       alternative is to fail OPEN on a path an assistant can reach: the
       two mint tools ride ordinary ``notes:write`` over MCP. Opening it
       honestly would cost a column on the capability recording who
       minted it; until then a person's way through is the bearer.

    Open BY CONSTRUCTION, and these are decisions too: ``admin_session``
    publishes no subject and kind ``system``, so the indexer, the
    migrations, the backfills and the distiller pass, and must. A human's
    SPA or CLI bearer is ``human_api`` with no subject, and passes.
    """
    if await session_bound_assistant_id(session) is not None:
        return True
    kind = (
        await session.execute(text("SELECT current_setting('app.current_actor_kind', true)"))
    ).scalar()
    return str(kind or "") in _ASSISTANT_ACTOR_KINDS


async def session_bound_assistant_id(session: AsyncSession) -> uuid.UUID | None:
    """The AI assistant this SESSION's credential is bound to, or None.

    The question a write guard has to ask is "what kind of credential is
    writing", and the only honest answer is the one on the credential
    itself: ``agent_tokens.assistant_id``, NOT NULL exactly when the
    token belongs to a bound assistant.

    NOT ``actor_kind``, which cannot answer it. Its ``human_api`` bucket
    holds every agent token that speaks HTTP -- the REST adapter opens
    EVERY bearer request as ``human_api``, and an agent token resolves
    to JWT-shaped claims that take that same branch -- so a guard
    written on it would fence MCP and leave the REST twin wide open,
    which is the cross-surface asymmetry the scope maps exist to
    prevent. It also defaults to ``human`` on an empty GUC, so it fails
    OPEN, which is the wrong direction for a guard.

    Reads ``app.current_actor_subject``, which ``tenant_session`` sets
    in the same statement as ``actor_kind``. A subject that is not a
    uuid, or not an agent-token id (the capability path publishes a
    capability-token id, the agent runtime an ``agent_runs`` id),
    resolves to None here: this function answers ONE question and its
    callers must not read a None as "not an agent".
    """
    raw = (
        await session.execute(text("SELECT current_setting('app.current_actor_subject', true)"))
    ).scalar()
    if not raw:
        return None
    try:
        token_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    return (
        await session.execute(select(AgentToken.assistant_id).where(AgentToken.id == token_id))
    ).scalar_one_or_none()


__all__ = [
    "DEFAULT_TTL_DAYS",
    "RAW_PREFIX",
    "AuthenticatedAgent",
    "MintResult",
    "MinterAuthority",
    "authenticate",
    "is_agent_token",
    "list_tokens",
    "mint",
    "minter_authority",
    "revoke",
    "session_bound_assistant_id",
    "session_writes_as_assistant",
]
