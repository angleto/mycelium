"""Issuer-API-key service: mint / list / rotate / revoke / authenticate.

See ``models/issuer_api_key.py`` and migration 0077. The key is scoped to ONE
issuer profile (the cedente), not a user, and authenticates the public Invoice
REST API.

Raw format
----------
``mycelium_ik_<12 hex>``... no: the raw is ``mycelium_ik_`` + 256 bits of
url-safe randomness (``secrets.token_urlsafe(32)``). Stored only as a KEYED hash
``HMAC-SHA256(ISSUER_KEY_PEPPER, raw)`` -- a DB-only dump is inert without the
pepper. A separate, INDEPENDENT ``key_public_id`` (its own random draw) is the
non-secret UI handle; the shown prefix is ``mycelium_ik_`` + ``key_public_id``.
It is never a slice of the secret.

Mint / rotate / revoke are owner-gated (minting a fiscal credential is
sensitive; same gate as agent tokens). ``authenticate`` is the verifier-side
helper the REST bearer dep calls; it crosses the tenant boundary via the
SECURITY DEFINER ``authenticate_issuer_api_key`` function.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.db import admin_session
from mycelium_core.errors import NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import IssuerProfile
from mycelium_core.models.issuer_api_key import IssuerApiKey
from mycelium_core.models.membership import Role
from mycelium_core.services import audit
from mycelium_core.services.rbac import require_role

# Discriminator prefix; checked before any other credential-kind branch.
RAW_PREFIX: str = "mycelium_ik_"
_RAW_ENTROPY_BYTES: int = 32
# Independent public-id length (hex chars). 12 hex = 48 bits of collision space,
# fits String(24) with room for the prefix in the display handle.
_PUBLIC_ID_BYTES: int = 6

# The least-privilege permission vocabulary. ``invoice:download`` is separate
# from ``invoice:read`` (a monitoring-only key cannot fetch the fiscal
# artifacts). ``invoice:credit_note`` and ``invoice:compose`` do not imply
# ``invoice:send``; enforcement is per-endpoint via ``require_perm`` (phase 3).
PERM_READ: str = "invoice:read"
PERM_COMPOSE: str = "invoice:compose"
PERM_SEND: str = "invoice:send"
PERM_CREDIT_NOTE: str = "invoice:credit_note"
PERM_DOWNLOAD: str = "invoice:download"
# Distinct capability for inline recipient resolve-or-create (the confused-deputy
# fix): a compose-only key must reference an existing ``client_tag_id``; creating
# / resolving a counterpart from inline cessionario data requires this.
PERM_CLIENT_WRITE: str = "invoice:client_write"
VALID_PERMISSIONS: frozenset[str] = frozenset(
    {PERM_READ, PERM_COMPOSE, PERM_SEND, PERM_CREDIT_NOTE, PERM_DOWNLOAD, PERM_CLIENT_WRITE}
)
_DEFAULT_PERMISSIONS: tuple[str, ...] = (PERM_READ,)


def _pepper() -> bytes:
    return get_settings().issuer_key_pepper.encode("utf-8")


def _hash(raw: str) -> bytes:
    """Keyed hash. Deterministic (indexed equality lookup), pepper-bound so a
    stolen DB alone cannot verify a candidate secret."""
    return hmac.new(_pepper(), raw.encode("utf-8"), hashlib.sha256).digest()


def _generate_raw() -> str:
    return f"{RAW_PREFIX}{secrets.token_urlsafe(_RAW_ENTROPY_BYTES)}"


def _generate_public_id() -> str:
    """Independent CSPRNG draw -- NOT derived from the raw secret."""
    return secrets.token_hex(_PUBLIC_ID_BYTES)


def is_issuer_api_key(raw: str) -> bool:
    return raw.startswith(RAW_PREFIX)


def _normalize_permissions(permissions: Sequence[str]) -> list[str]:
    perms = set(permissions)
    invalid = perms - VALID_PERMISSIONS
    if invalid:
        raise UnprocessableError(
            MessageCode.ISSUER_API_KEY_PERMISSION_INVALID,
            invalid=sorted(invalid),
            valid=sorted(VALID_PERMISSIONS),
        )
    if not perms:
        perms = set(_DEFAULT_PERMISSIONS)
    return sorted(perms)


def _resolve_expiry(ttl_days: int | None) -> datetime.datetime:
    """Expiry is MANDATORY: never a never-expiring key. ``None`` (or a
    non-positive value) defaults to the max lifetime; a longer request is
    clamped to it."""
    max_days = max(1, get_settings().issuer_key_max_lifetime_seconds // 86400)
    requested = ttl_days if (ttl_days is not None and ttl_days > 0) else max_days
    days = min(requested, max_days)
    return datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=days)


@dataclass(frozen=True, slots=True)
class MintResult:
    key: IssuerApiKey
    # The raw value; returned exactly once (create + rotate).
    raw: str


@dataclass(frozen=True, slots=True)
class AuthenticatedIssuerKey:
    key_id: uuid.UUID
    org_id: uuid.UUID
    issuer_profile_id: uuid.UUID
    permissions: list[str]
    # True when the GRACE (previous) secret matched -- telemetry / rotation
    # observability for the caller.
    matched_previous: bool = False


async def mint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    name: str,
    permissions: Sequence[str] = _DEFAULT_PERMISSIONS,
    ttl_days: int | None = None,
) -> MintResult:
    """Owner-gated. Mint a fresh issuer-scoped key. The secret is
    system-generated; the caller supplies no key material. ``ttl_days=None``
    defaults to (and is capped at) the max lifetime -- there is no never-expiring
    key."""
    await require_role(session, org_id, actor_id, Role.owner)
    perms = _normalize_permissions(permissions)
    issuer = (
        await session.execute(
            select(IssuerProfile).where(
                IssuerProfile.id == issuer_profile_id,
                IssuerProfile.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if issuer is None:
        raise NotFoundError(MessageCode.ISSUER_PROFILE_NOT_FOUND)
    raw = _generate_raw()
    row = IssuerApiKey(
        org_id=org_id,
        issuer_profile_id=issuer_profile_id,
        created_by=actor_id,
        name=name,
        key_public_id=_generate_public_id(),
        secret_hash=_hash(raw),
        permissions=perms,
        expires_at=_resolve_expiry(ttl_days),
    )
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_api_key",
        entity_id=row.id,
        action="mint",
        diff={
            "name": name,
            "issuer_profile_id": str(issuer_profile_id),
            "permissions": perms,
        },
    )
    return MintResult(key=row, raw=raw)


async def list_keys(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    issuer_profile_id: uuid.UUID | None = None,
) -> list[IssuerApiKey]:
    """RLS-scoped listing (active + revoked; the UI distinguishes via
    ``revoked_at``). Member-level read -- the secret is never in the row."""
    stmt = select(IssuerApiKey).where(IssuerApiKey.org_id == org_id)
    if issuer_profile_id is not None:
        stmt = stmt.where(IssuerApiKey.issuer_profile_id == issuer_profile_id)
    stmt = stmt.order_by(IssuerApiKey.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def rotate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    key_id: uuid.UUID,
    grace_seconds: int | None = None,
) -> MintResult:
    """Owner-gated. Issue a new secret for an existing key (same
    ``key_public_id``). ``grace_seconds`` (default = the configured value, 0 =
    hard rotation) keeps the PREVIOUS secret valid for a bounded window; it is
    clamped to the server ceiling. Bumps ``version`` (optimistic concurrency)."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = (
        await session.execute(
            select(IssuerApiKey).where(IssuerApiKey.id == key_id, IssuerApiKey.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.ISSUER_API_KEY_NOT_FOUND)
    settings = get_settings()
    requested = (
        grace_seconds if grace_seconds is not None else settings.issuer_key_rotation_grace_seconds
    )
    grace = max(0, min(requested, settings.issuer_key_rotation_grace_max_seconds))
    now = datetime.datetime.now(tz=datetime.UTC)
    if grace > 0:
        row.previous_secret_hash = row.secret_hash
        row.previous_secret_expires_at = now + datetime.timedelta(seconds=grace)
    else:
        # Hard rotation: the old secret dies immediately.
        row.previous_secret_hash = None
        row.previous_secret_expires_at = None
    row.previous_secret_last_used_at = None
    new_raw = _generate_raw()
    row.secret_hash = _hash(new_raw)
    row.rotated_at = now
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_api_key",
        entity_id=row.id,
        action="rotate",
        diff={"grace_seconds": grace},
    )
    return MintResult(key=row, raw=new_raw)


async def revoke(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    key_id: uuid.UUID,
) -> None:
    """Owner-gated. Mark a key revoked (kills BOTH the current and any grace
    secret at once, enforced in the verify function). Idempotent."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = (
        await session.execute(
            select(IssuerApiKey).where(IssuerApiKey.id == key_id, IssuerApiKey.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.ISSUER_API_KEY_NOT_FOUND)
    if row.revoked_at is not None:
        return
    row.revoked_at = datetime.datetime.now(tz=datetime.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="issuer_api_key",
        entity_id=row.id,
        action="revoke",
    )


async def authenticate(
    raw: str,
    *,
    session: AsyncSession | None = None,
) -> AuthenticatedIssuerKey | None:
    """Resolve a raw issuer key to its principal, or ``None`` if unknown /
    revoked / expired / outside the grace window. Crosses the tenant boundary
    via the SECURITY DEFINER ``authenticate_issuer_api_key`` (migration 0077),
    which two-probes (current-hash-wins), gates revoke/expiry, and bumps the
    throttled last-used telemetry."""
    if not is_issuer_api_key(raw):
        return None
    secret_hash = _hash(raw)
    if session is not None:
        return await _call_authenticate_fn(session, secret_hash)
    async with admin_session() as s:
        return await _call_authenticate_fn(s, secret_hash)


async def scan_issuer_key_expiry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    now: datetime.datetime | None = None,
    thresholds: Sequence[int] = (30, 7),
) -> int:
    """Enqueue idempotent expiry warnings (G2). For each active (non-revoked)
    key whose ``expires_at`` falls within a threshold window (default 30 and 7
    days), a notification is enqueued ONCE per (key, threshold, channel) -- via
    the notifications dedupe key -- to the minting user's enabled channels.
    Returns how many notifications were enqueued. Meant to be called per-org on a
    daily cadence by the worker, alongside the due-date reminder scan."""
    from mycelium_core.models.notification import NotificationPref
    from mycelium_core.services import notifications as notif

    ref = now or datetime.datetime.now(tz=datetime.UTC)
    horizon = ref + datetime.timedelta(days=max(thresholds))
    keys = (
        (
            await session.execute(
                select(IssuerApiKey).where(
                    IssuerApiKey.org_id == org_id,
                    IssuerApiKey.revoked_at.is_(None),
                    IssuerApiKey.expires_at > ref,
                    IssuerApiKey.expires_at <= horizon,
                )
            )
        )
        .scalars()
        .all()
    )
    enqueued = 0
    for k in keys:
        if k.created_by is None:
            continue
        days_left = (k.expires_at - ref).days
        prefs = (
            (
                await session.execute(
                    select(NotificationPref).where(
                        NotificationPref.org_id == org_id,
                        NotificationPref.user_id == k.created_by,
                        NotificationPref.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for threshold in sorted(thresholds):
            if days_left > threshold:
                continue
            for p in prefs:
                await notif.enqueue(
                    session,
                    org_id=org_id,
                    actor_id=k.created_by,
                    user_id=k.created_by,
                    channel=p.channel,
                    kind="issuer_key_expiry",
                    title="Issuer API key expiring soon",
                    body=(
                        f"The issuer API key '{k.name}' expires in {days_left} day(s). "
                        "Rotate it to avoid an interruption."
                    ),
                    dedupe_key=f"issuer_key_expiry:{k.id}:{threshold}:{p.channel.value}",
                )
                enqueued += 1
    return enqueued


async def _call_authenticate_fn(
    session: AsyncSession, secret_hash: bytes
) -> AuthenticatedIssuerKey | None:
    result = await session.execute(
        text(
            "SELECT out_key_id, out_org_id, out_issuer_profile_id, "
            "out_permissions, out_matched_previous "
            "FROM authenticate_issuer_api_key(:h)"
        ),
        {"h": secret_hash},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    perms = [str(p) for p in (row[3] or [])]
    return AuthenticatedIssuerKey(
        key_id=row[0],
        org_id=row[1],
        issuer_profile_id=row[2],
        permissions=perms,
        matched_previous=bool(row[4]),
    )


__all__ = [
    "PERM_CLIENT_WRITE",
    "PERM_COMPOSE",
    "PERM_CREDIT_NOTE",
    "PERM_DOWNLOAD",
    "PERM_READ",
    "PERM_SEND",
    "RAW_PREFIX",
    "VALID_PERMISSIONS",
    "AuthenticatedIssuerKey",
    "MintResult",
    "authenticate",
    "is_issuer_api_key",
    "list_keys",
    "mint",
    "revoke",
    "rotate",
    "scan_issuer_key_expiry",
]
