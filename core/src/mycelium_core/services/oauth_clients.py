"""Public OAuth clients stored in ``oauth_clients``: registration, lookup,
bounds and the sweep (ADR-0069).

A public client is a name and a list of places an authorization code may be
sent. It holds no secret and grants nothing (see ``models/oauth_client.py``).
Which redirect URIs are acceptable is not decided here: that is the redirect
policy (``mycelium_core.oauth.redirects``), shared by every client strategy.
This module is the storage behind the ``registered`` strategy
(``mycelium_core.oauth.strategies.registered``).
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import security_events
from mycelium_core.errors import NotFoundError, QuotaExceededError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.membership import Role
from mycelium_core.models.oauth_authorization_request import OAuthAuthorizationRequest
from mycelium_core.models.oauth_client import ClientRegistration, OAuthClient
from mycelium_core.services.rbac import require_role

# Minted, never chosen. The prefix keeps a public client id from ever parsing
# as a UUID, which is what the legacy flow uses as ITS client id: the token
# endpoint tells the two apart by the value, before any lookup.
PUBLIC_CLIENT_PREFIX = "mycelium_pc_"

MAX_CLIENT_NAME_LENGTH = 100

# How many dynamic registrations one network origin may have that never
# completed a consent, within the window. Completed ones do not count, for
# the reason ``device_authorization.check_open_rate`` gives: an office or a
# VPN exit is one address, and charging it for its successes would lock out
# whoever connects last. A registration grants nothing, so this bounds table
# growth and noise rather than an attack, and is generous for that reason.
REGISTER_LIMIT_PER_ORIGIN = 30
REGISTER_WINDOW_SECONDS = 3600

# How many unused dynamic registrations the table holds at most. Reaching it
# EVICTS the oldest unused ones; it never refuses the newcomer. An unused
# registration grants nothing, so dropping the oldest costs at most a host
# that registered long ago and never finished a re-registration, while
# refusing would let one caller -- in particular one whose origin cannot be
# attributed, which no per-origin bound reaches -- lock everybody out for a
# day (security review, 2026-11-13). Unused rows are also swept after
# ``UNUSED_CLIENT_TTL_HOURS``, so in steady state the count sits far below.
REGISTER_GLOBAL_CEILING = 5000

# A client that registers and never gets a consent through is abandoned.
# Legitimate hosts register and send the person to the consent page within
# seconds; a day is generous for one that is interrupted.
UNUSED_CLIENT_TTL_HOURS = 24


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def is_public_client_id(client_id: str) -> bool:
    return client_id.startswith(PUBLIC_CLIENT_PREFIX)


def _mint_client_id() -> str:
    return PUBLIC_CLIENT_PREFIX + secrets.token_urlsafe(24)


def clean_client_name(name: object, *, fallback: str) -> str:
    """A display name, trimmed and bounded. Unauthenticated input on the
    dynamic path, so it is treated as text to show and nothing more."""
    if not isinstance(name, str):
        return fallback
    cleaned = " ".join(name.split())
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned[:MAX_CLIENT_NAME_LENGTH] or fallback


async def check_registration_rate(session: AsyncSession, *, origin_ip: str | None) -> None:
    """Refuse an origin piling up registrations nobody completes, then make
    room under the global ceiling by evicting the oldest unused ones.

    ``origin_ip`` is an origin BUCKET (``oauth.origin.origin_bucket``: an
    IPv4 address, or an IPv6 /64). Counted from the rows themselves, as the
    device flow does. An origin the server could not attribute is not
    limited per origin -- behind an unconfigured proxy chain every caller
    would share one bucket and the first noisy one would lock out the rest
    -- and is held only by the ceiling, which evicts rather than refuses."""
    unused = (
        OAuthClient.registration == ClientRegistration.dynamic.value,
        OAuthClient.first_grant_at.is_(None),
    )
    if origin_ip is not None:
        since = _now() - datetime.timedelta(seconds=REGISTER_WINDOW_SECONDS)
        from_origin = (
            await session.execute(
                select(func.count())
                .select_from(OAuthClient)
                .where(*unused, OAuthClient.origin_ip == origin_ip, OAuthClient.created_at >= since)
            )
        ).scalar_one()
        if from_origin >= REGISTER_LIMIT_PER_ORIGIN:
            security_events.emit(
                "oauth.register_rate_limited", origin_ip=origin_ip, limit=REGISTER_LIMIT_PER_ORIGIN
            )
            raise QuotaExceededError(MessageCode.OAUTH_TOO_MANY_PENDING)

    total = (
        await session.execute(select(func.count()).select_from(OAuthClient).where(*unused))
    ).scalar_one()
    excess = total - REGISTER_GLOBAL_CEILING + 1
    if excess > 0:
        evicted = await _evict_unused(session, limit=excess)
        security_events.emit(
            "oauth.register_ceiling_evicted", evicted=evicted, limit=REGISTER_GLOBAL_CEILING
        )


def _evictable() -> ColumnElement[bool]:
    """A dynamic registration nobody relies on: it never completed an
    exchange AND no person has approved a consent for it that is still
    waiting to be redeemed. The second half is the one that matters: an
    approved, unredeemed consent is somebody's decision in flight, and
    evicting the client under it would let an anonymous caller delete it
    (second security review, 2026-11-13)."""
    pending_approval = (
        select(OAuthAuthorizationRequest.id)
        .where(
            OAuthAuthorizationRequest.client_id == OAuthClient.client_id,
            OAuthAuthorizationRequest.approved_at.is_not(None),
            OAuthAuthorizationRequest.redeemed_at.is_(None),
        )
        .exists()
    )
    return and_(
        OAuthClient.registration == ClientRegistration.dynamic.value,
        OAuthClient.first_grant_at.is_(None),
        ~pending_approval,
    )


async def _evict_unused(
    session: AsyncSession,
    *,
    limit: int | None = None,
    created_before: datetime.datetime | None = None,
) -> int:
    """Delete evictable clients, oldest first, and their UNANSWERED requests.

    The predicate is in the DELETE itself, not only in the SELECT that chose
    the candidates: a consent approved between the two is then seen by the
    statement that would have deleted its client, and the client stays.
    Requests are deleted only if still unanswered, by the same reasoning."""
    candidates = select(OAuthClient.id).where(_evictable())
    if created_before is not None:
        candidates = candidates.where(OAuthClient.created_at < created_before)
    candidates = candidates.order_by(OAuthClient.created_at, OAuthClient.id)
    if limit is not None:
        candidates = candidates.limit(limit)
    deleted = (
        (
            await session.execute(
                sa_delete(OAuthClient)
                .where(OAuthClient.id.in_(candidates), _evictable())
                .returning(OAuthClient.client_id)
            )
        )
        .scalars()
        .all()
    )
    if deleted:
        await session.execute(
            sa_delete(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.client_id.in_(list(deleted)),
                OAuthAuthorizationRequest.approved_at.is_(None),
            )
        )
    return len(deleted)


async def register_dynamic(
    session: AsyncSession,
    *,
    client_name: str,
    redirect_uris: Sequence[str],
    origin_ip: str | None,
) -> OAuthClient:
    """Write a dynamic registration. The caller has validated the URIs and
    checked the rate; this mints the id and records the row. Runs in the
    system session with no workspace, the only one the row policy lets
    write a row without one."""
    row = OAuthClient(
        client_id=_mint_client_id(),
        client_name=client_name,
        redirect_uris=list(redirect_uris),
        registration=ClientRegistration.dynamic.value,
        origin_ip=origin_ip,
    )
    session.add(row)
    await session.flush()
    return row


async def register_manual(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_name: str,
    redirect_uris: Sequence[str],
) -> OAuthClient:
    """A client a person registers by hand, in the workspace they are in.

    Any member may: the row is a redirect allowlist and grants nothing, and
    what a credential obtained through it may do is decided at consent, by
    whoever consents, under the same role rule as creating an assistant."""
    await require_role(session, org_id, actor_id, Role.member)
    row = OAuthClient(
        client_id=_mint_client_id(),
        client_name=client_name,
        redirect_uris=list(redirect_uris),
        registration=ClientRegistration.manual.value,
        org_id=org_id,
        created_by_id=actor_id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_manual(session: AsyncSession, *, org_id: uuid.UUID) -> list[OAuthClient]:
    """The hand-registered clients of this workspace. The row policy already
    confines a tenant session to its workspace; the filter says so too."""
    rows = await session.execute(
        select(OAuthClient)
        .where(
            OAuthClient.org_id == org_id,
            OAuthClient.registration == ClientRegistration.manual.value,
        )
        .order_by(OAuthClient.created_at.desc())
    )
    return list(rows.scalars().all())


async def delete_manual(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    client_id: str,
) -> None:
    """Remove a hand-registered client: its author, or the workspace owner.

    Removing a client stops new consents through it. It does NOT revoke the
    assistants earlier consents created -- those are credentials in their own
    right, listed and revoked under AI assistants -- and the page says so."""
    row = (
        await session.execute(
            select(OAuthClient).where(
                OAuthClient.client_id == client_id,
                OAuthClient.org_id == org_id,
                OAuthClient.registration == ClientRegistration.manual.value,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.OAUTH_CLIENT_NOT_FOUND)
    if row.created_by_id != actor_id:
        await require_role(session, org_id, actor_id, Role.owner)
    # Its pending requests go with it, including an approved one whose code
    # has not been exchanged yet: removing a client must stop the next
    # credential, not only the next consent. (No foreign key does this: a
    # request's client id may name a client no table holds.)
    await session.execute(
        sa_delete(OAuthAuthorizationRequest).where(OAuthAuthorizationRequest.client_id == client_id)
    )
    await session.delete(row)
    await session.flush()


async def get_by_client_id(session: AsyncSession, *, client_id: str) -> OAuthClient | None:
    """Any public client by its id. Pre-tenant: the caller is the system
    session the OAuth endpoints run in."""
    if not is_public_client_id(client_id):
        return None
    return (
        await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    ).scalar_one_or_none()


async def mark_granted(session: AsyncSession, *, client_id: str) -> None:
    """Record the first completed exchange, which takes the client out of
    the sweep's reach for good."""
    await session.execute(
        update(OAuthClient)
        .where(OAuthClient.client_id == client_id, OAuthClient.first_grant_at.is_(None))
        .values(first_grant_at=_now())
    )


async def sweep_unused(
    session: AsyncSession, *, older_than_hours: int = UNUSED_CLIENT_TTL_HOURS
) -> int:
    """Delete dynamic registrations older than the window that nobody relies
    on (``_evictable``), with their unanswered requests; manual clients are
    never touched, a person decided to keep those."""
    cutoff = _now() - datetime.timedelta(hours=older_than_hours)
    return await _evict_unused(session, created_before=cutoff)


__all__ = [
    "MAX_CLIENT_NAME_LENGTH",
    "PUBLIC_CLIENT_PREFIX",
    "REGISTER_GLOBAL_CEILING",
    "REGISTER_LIMIT_PER_ORIGIN",
    "REGISTER_WINDOW_SECONDS",
    "UNUSED_CLIENT_TTL_HOURS",
    "check_registration_rate",
    "clean_client_name",
    "delete_manual",
    "get_by_client_id",
    "is_public_client_id",
    "list_manual",
    "mark_granted",
    "register_dynamic",
    "register_manual",
    "sweep_unused",
]
