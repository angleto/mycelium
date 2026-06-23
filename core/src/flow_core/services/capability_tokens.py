"""Capability-token service: mint / authenticate / consume.

See ``models/capability_token.py`` and migration 0038. A capability
token is an ephemeral, single-use, resource-scoped bearer credential:
unlike an :class:`~flow_core.models.agent_token.AgentToken` it grants
exactly one ``action`` on one resource and is consumed on first
successful use. Minted by an already-authenticated principal (a member
of the org); the raw value is returned once and only its SHA-256 digest
is stored.

Raw format: ``flow_cap_<43 url-safe chars>`` -- a discriminator prefix
the verifier branches on (so a capability token never reaches the JWT /
agent-token paths) plus ``secrets.token_urlsafe(32)``.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.db import admin_session
from flow_core.models.capability_token import CapabilityToken
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role

# Discriminator prefix. Distinct from ``flow_at_`` (agent tokens) so the
# verifier can branch on the credential kind before any JWT decode.
RAW_PREFIX: str = "flow_cap_"
_RAW_ENTROPY_BYTES: int = 32
_PREFIX_CHARS: int = 16
# Short by design: a capability token is handed out for one imminent
# operation, not stored. Five minutes covers a stream + a retry.
DEFAULT_TTL_SECONDS: int = 300

# Capability kinds. Named constants so the minting tool, the verifier
# scope-check, and the tests all agree on the exact strings.
#
# 1. note_part_body:write -- single-use, resource = one note_part: the
#    token-free part-body stream, consumed on first successful write.
# 2. attachment:read -- multi-use within the TTL, resource = the PARENT
#    (a note or task): downloads ANY attachment of that parent until the
#    token expires. NOT consumed (a download is idempotent and an agent
#    usually fetches several files in a row), so the short TTL is the
#    only bound.
ACTION_NOTE_PART_BODY_WRITE = "note_part_body:write"
RESOURCE_NOTE_PART = "note_part"
ACTION_ATTACHMENT_READ = "attachment:read"
RESOURCE_NOTE = "note"
RESOURCE_TASK = "task"

# Symmetric block surface (token-free upload / download / patch over MCP).
# The literal scheme stays "<resource>:<verb>" so the verifier keeps its
# one-line `action == ... and resource_kind == ...` triple-check and a
# token can never be replayed across resources. All literals fit the
# action String(64) / resource_kind String(32) columns (migration 0038),
# so NO migration is needed.
#
# Scope semantics:
# - attachment write is PARENT-scoped (resource_id = note/task id), like
#   attachment read; single-use.
# - the three text blocks are EXACT-resource-scoped (resource_id = the
#   part / task / annotation id). read = multi-use (idempotent); write and
#   patch = single-use (consumed on first successful write).
ACTION_ATTACHMENT_WRITE = "attachment:write"

ACTION_NOTE_PART_BODY_READ = "note_part_body:read"
ACTION_NOTE_PART_BODY_PATCH = "note_part_body:patch"

ACTION_TASK_DESCRIPTION_READ = "task_description:read"
ACTION_TASK_DESCRIPTION_WRITE = "task_description:write"
ACTION_TASK_DESCRIPTION_PATCH = "task_description:patch"

ACTION_ANNOTATION_BODY_READ = "annotation_body:read"
ACTION_ANNOTATION_BODY_WRITE = "annotation_body:write"
ACTION_ANNOTATION_BODY_PATCH = "annotation_body:patch"

# The annotation (comment / suggestion) row is its own resource kind so a
# body token cannot be confused with the note_part / task it hangs off.
RESOURCE_ANNOTATION = "annotation"

# A text block is one of the three patch/read/write targets, addressed by
# (kind, verb). One mapping so HTTP mint, MCP tools and the deps agree.
TEXT_BLOCK_KINDS = ("note_part", "task_description", "annotation")
TEXT_BLOCK_VERBS = ("read", "write", "patch")

_TEXT_BLOCK_ACTIONS: dict[tuple[str, str], str] = {
    ("note_part", "read"): ACTION_NOTE_PART_BODY_READ,
    ("note_part", "write"): ACTION_NOTE_PART_BODY_WRITE,
    ("note_part", "patch"): ACTION_NOTE_PART_BODY_PATCH,
    ("task_description", "read"): ACTION_TASK_DESCRIPTION_READ,
    ("task_description", "write"): ACTION_TASK_DESCRIPTION_WRITE,
    ("task_description", "patch"): ACTION_TASK_DESCRIPTION_PATCH,
    ("annotation", "read"): ACTION_ANNOTATION_BODY_READ,
    ("annotation", "write"): ACTION_ANNOTATION_BODY_WRITE,
    ("annotation", "patch"): ACTION_ANNOTATION_BODY_PATCH,
}

# The resource_kind a text-block token is scoped to, per kind. note_part
# -> RESOURCE_NOTE_PART (part id); task_description -> RESOURCE_TASK (task
# id); annotation -> RESOURCE_ANNOTATION (annotation id).
TEXT_BLOCK_RESOURCE_KIND: dict[str, str] = {
    "note_part": RESOURCE_NOTE_PART,
    "task_description": RESOURCE_TASK,
    "annotation": RESOURCE_ANNOTATION,
}


def text_block_action(kind: str, verb: str) -> str:
    """Map a (kind, verb) pair to its capability action literal. Raises
    ValueError on an unknown pair so the HTTP/MCP mint surfaces fail loud
    rather than mint a token no dep will ever accept."""
    try:
        return _TEXT_BLOCK_ACTIONS[(kind, verb)]
    except KeyError:
        raise ValueError(
            f"unknown text block (kind={kind!r}, verb={verb!r}); "
            f"kind in {TEXT_BLOCK_KINDS}, verb in {TEXT_BLOCK_VERBS}"
        ) from None


def text_block_resource_kind(kind: str) -> str:
    """Map a text-block kind to the capability resource_kind it scopes to."""
    try:
        return TEXT_BLOCK_RESOURCE_KIND[kind]
    except KeyError:
        raise ValueError(
            f"unknown text block kind={kind!r}; expected one of {TEXT_BLOCK_KINDS}"
        ) from None


def _hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _generate_raw() -> str:
    return f"{RAW_PREFIX}{secrets.token_urlsafe(_RAW_ENTROPY_BYTES)}"


def is_capability_token(raw: str) -> bool:
    """True iff the bearer looks like a capability token (cheap prefix
    pre-check, same idea as ``agent_tokens.is_agent_token``)."""
    return raw.startswith(RAW_PREFIX)


@dataclass(frozen=True, slots=True)
class MintResult:
    token_id: uuid.UUID
    # The raw value, returned exactly once; the DB holds only the hash.
    raw: str
    expires_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedCapability:
    token_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    action: str
    resource_kind: str
    resource_id: uuid.UUID


async def mint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    resource_kind: str,
    resource_id: uuid.UUID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> MintResult:
    """Mint a single-use capability token scoped to ``action`` on
    ``resource_kind`` / ``resource_id``.

    Member-gated: the caller must be entitled to act in the workspace
    (the same floor the guarded write enforces, so minting grants no
    capability the caller did not already hold). Runs inside the
    caller's tenant session, so the INSERT is RLS-checked against the
    current org."""
    await require_role(session, org_id, actor_id, Role.member)
    if ttl_seconds <= 0:
        ttl_seconds = DEFAULT_TTL_SECONDS
    raw = _generate_raw()
    expires_at = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=ttl_seconds)
    row = CapabilityToken(
        org_id=org_id,
        user_id=actor_id,
        token_hash=_hash(raw),
        prefix=raw[:_PREFIX_CHARS],
        action=action,
        resource_kind=resource_kind,
        resource_id=resource_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="capability_token",
        entity_id=row.id,
        action="mint",
        diff={"action": action, "resource_kind": resource_kind, "resource_id": str(resource_id)},
    )
    return MintResult(token_id=row.id, raw=raw, expires_at=expires_at)


async def authenticate(
    raw: str,
    *,
    session: AsyncSession | None = None,
) -> AuthenticatedCapability | None:
    """Resolve a raw capability token to its principal + resource
    constraint, or ``None`` if unknown / expired / already consumed.

    Does NOT consume: the caller stamps ``consumed_at`` via
    :func:`consume` only after the guarded operation succeeds, so a
    retried 409 (stale version) does not burn the token. The SECURITY
    DEFINER ``authenticate_capability_token`` (migration 0038) crosses
    the tenant boundary for the lookup."""
    if not is_capability_token(raw):
        return None
    token_hash = _hash(raw)
    if session is not None:
        return await _call_authenticate_fn(session, token_hash)
    async with admin_session() as s:
        return await _call_authenticate_fn(s, token_hash)


async def _call_authenticate_fn(
    session: AsyncSession, token_hash: bytes
) -> AuthenticatedCapability | None:
    result = await session.execute(
        text(
            "SELECT out_token_id, out_user_id, out_org_id, out_action, "
            "out_resource_kind, out_resource_id "
            "FROM authenticate_capability_token(:h)"
        ),
        {"h": token_hash},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return AuthenticatedCapability(
        token_id=row[0],
        user_id=row[1],
        org_id=row[2],
        action=row[3],
        resource_kind=row[4],
        resource_id=row[5],
    )


async def consume(session: AsyncSession, *, token_id: uuid.UUID) -> bool:
    """Stamp ``consumed_at`` iff still unconsumed. Returns True if this
    call consumed it, False if it was already consumed (lost the race).
    Runs in the caller's tenant session (RLS-checked on org), in the
    same transaction as the guarded write, so a rolled-back write
    rolls back the consumption too."""
    result = await session.execute(
        text(
            "UPDATE capability_tokens SET consumed_at = now(), updated_at = now() "
            "WHERE id = :id AND consumed_at IS NULL"
        ),
        {"id": token_id},
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


__all__ = [
    "ACTION_ANNOTATION_BODY_PATCH",
    "ACTION_ANNOTATION_BODY_READ",
    "ACTION_ANNOTATION_BODY_WRITE",
    "ACTION_ATTACHMENT_READ",
    "ACTION_ATTACHMENT_WRITE",
    "ACTION_NOTE_PART_BODY_PATCH",
    "ACTION_NOTE_PART_BODY_READ",
    "ACTION_NOTE_PART_BODY_WRITE",
    "ACTION_TASK_DESCRIPTION_PATCH",
    "ACTION_TASK_DESCRIPTION_READ",
    "ACTION_TASK_DESCRIPTION_WRITE",
    "DEFAULT_TTL_SECONDS",
    "RAW_PREFIX",
    "RESOURCE_ANNOTATION",
    "RESOURCE_NOTE",
    "RESOURCE_NOTE_PART",
    "RESOURCE_TASK",
    "TEXT_BLOCK_KINDS",
    "TEXT_BLOCK_RESOURCE_KIND",
    "TEXT_BLOCK_VERBS",
    "AuthenticatedCapability",
    "MintResult",
    "authenticate",
    "consume",
    "is_capability_token",
    "mint",
    "text_block_action",
    "text_block_resource_kind",
]
