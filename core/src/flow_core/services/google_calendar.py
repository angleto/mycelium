"""Google Calendar subscription service (epic #125 P1).

Domain logic: connect a remote Google calendar to a Flow working
calendar, ingest events idempotently (one row per Google event id), and
push a Flow event up to Google. The HTTP boundary is a Protocol
(``GoogleApiClient``) so tests inject a fake; the Fernet envelope is
reused for the refresh token (ADR-0006).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret, encrypt_secret
from flow_core.errors import DomainError, NotFoundError
from flow_core.google_api import GoogleApiClient, google_api_client
from flow_core.i18n import MessageCode
from flow_core.models.event import Event
from flow_core.models.google_calendar import (
    CalendarSubscription,
    GoogleCalendarStatus,
)
from flow_core.models.membership import Role
from flow_core.services import audit
from flow_core.services.rbac import require_role

_EXTERNAL_PROVIDER = "google"
_log = logging.getLogger("flow.google_calendar")


@dataclass(frozen=True)
class SyncResult:
    subscription_id: uuid.UUID
    ingested: int
    updated: int
    skipped: int
    ok: bool
    error: str | None = None


async def get_subscription(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    subscription_id: uuid.UUID,
) -> CalendarSubscription:
    sub = (
        await session.execute(
            select(CalendarSubscription).where(CalendarSubscription.id == subscription_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(MessageCode.GOOGLE_CALENDAR_SUBSCRIPTION_NOT_FOUND)
    return sub


async def list_subscriptions(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[CalendarSubscription]:
    return list(
        (
            await session.execute(
                select(CalendarSubscription).order_by(CalendarSubscription.created_at)
            )
        )
        .scalars()
        .all()
    )


async def list_active_across_orgs(session: AsyncSession) -> list[CalendarSubscription]:
    """Worker-facing scan: every active subscription across orgs. Caller
    must have passed an ``admin_session`` (no tenant GUCs) or accept the
    RLS-filtered view."""
    return list(
        (
            await session.execute(
                select(CalendarSubscription).where(
                    CalendarSubscription.status == GoogleCalendarStatus.active
                )
            )
        )
        .scalars()
        .all()
    )


async def connect(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    our_calendar_id: uuid.UUID,
    refresh_token: str,
    google_calendar_id: str,
) -> CalendarSubscription:
    """Create (or rotate) a subscription. The refresh token is encrypted
    at rest; an existing subscription for the same triple
    (user, our_calendar, google_calendar) is rotated in place so a
    re-consent does not orphan an old row."""
    await require_role(session, org_id, actor_id, Role.member)
    existing = (
        await session.execute(
            select(CalendarSubscription).where(
                CalendarSubscription.user_id == user_id,
                CalendarSubscription.our_calendar_id == our_calendar_id,
                CalendarSubscription.google_calendar_id == google_calendar_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await optimistic_update(
            session,
            CalendarSubscription,
            pk=existing.id,
            expected_version=existing.version,
            values={
                "refresh_token_encrypted": encrypt_secret(refresh_token),
                "status": GoogleCalendarStatus.active,
                "last_error": None,
            },
        )
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="google_calendar_subscription",
            entity_id=existing.id,
            action="rotate",
        )
        return await get_subscription(session, org_id=org_id, subscription_id=existing.id)

    sub = CalendarSubscription(
        org_id=org_id,
        user_id=user_id,
        our_calendar_id=our_calendar_id,
        google_calendar_id=google_calendar_id,
        refresh_token_encrypted=encrypt_secret(refresh_token),
        status=GoogleCalendarStatus.active,
    )
    session.add(sub)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="google_calendar_subscription",
        entity_id=sub.id,
        action="create",
    )
    return sub


async def disconnect(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    subscription_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    sub = await get_subscription(session, org_id=org_id, subscription_id=subscription_id)
    await session.delete(sub)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="google_calendar_subscription",
        entity_id=subscription_id,
        action="delete",
    )


def _parse_google_ts(raw: str, tz_hint: str | None) -> dt.datetime:
    """Google returns either ``date`` (all-day, YYYY-MM-DD) or
    ``dateTime`` (RFC 3339). Normalise to a timezone-aware UTC datetime
    (the events table is TIMESTAMPTZ). All-day events anchor at 00:00
    in the hint timezone (UTC if absent)."""
    if "T" in raw:
        # RFC 3339; Python 3.12 ``fromisoformat`` accepts trailing "Z".
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        d = dt.date.fromisoformat(raw)
        ts = dt.datetime.combine(d, dt.time.min)
    if ts.tzinfo is None:
        if tz_hint:
            try:
                from zoneinfo import ZoneInfo

                ts = ts.replace(tzinfo=ZoneInfo(tz_hint))
            except Exception:
                ts = ts.replace(tzinfo=dt.UTC)
        else:
            ts = ts.replace(tzinfo=dt.UTC)
    return ts.astimezone(dt.UTC)


async def _refresh_access_token(
    *,
    refresh_token: str,
    client: GoogleApiClient | None = None,
) -> str:
    s = get_settings()
    if not s.google_configured:
        raise DomainError(MessageCode.OAUTH_NOT_CONFIGURED)
    api = client or google_api_client()
    try:
        token = await api.refresh_access_token(
            refresh_token=refresh_token,
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
        )
    except Exception as exc:
        raise DomainError(MessageCode.OAUTH_REFRESH_FAILED, detail=str(exc)) from exc
    return token.access_token


async def sync_subscription(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    subscription_id: uuid.UUID,
    client: GoogleApiClient | None = None,
) -> SyncResult:
    """Ingest events from Google for one subscription. Idempotent on
    ``(external_subscription_id, external_id)``: a known google event
    id updates the existing row, an unknown one inserts. Connector
    failures are recorded (status=error) and the result captures it."""
    await require_role(session, org_id, actor_id, Role.member)
    sub = await get_subscription(session, org_id=org_id, subscription_id=subscription_id)
    api = client or google_api_client()
    try:
        access_token = await _refresh_access_token(
            refresh_token=decrypt_secret(sub.refresh_token_encrypted), client=api
        )
        events, _next_page = await api.list_events(
            access_token=access_token,
            calendar_id=sub.google_calendar_id,
        )
    except DomainError as exc:
        await session.execute(
            update(CalendarSubscription)
            .where(CalendarSubscription.id == subscription_id)
            .values(status=GoogleCalendarStatus.error, last_error=str(exc))
        )
        return SyncResult(
            subscription_id=subscription_id,
            ingested=0,
            updated=0,
            skipped=0,
            ok=False,
            error=str(exc),
        )
    except Exception as exc:
        await session.execute(
            update(CalendarSubscription)
            .where(CalendarSubscription.id == subscription_id)
            .values(status=GoogleCalendarStatus.error, last_error=str(exc))
        )
        return SyncResult(
            subscription_id=subscription_id,
            ingested=0,
            updated=0,
            skipped=0,
            ok=False,
            error=str(exc),
        )

    existing = {
        e.external_id: e
        for e in (
            await session.execute(
                select(Event).where(Event.external_subscription_id == subscription_id)
            )
        )
        .scalars()
        .all()
    }
    ingested = 0
    updated = 0
    skipped = 0
    for ge in events:
        # Skip cancelled events (Google sends a stub row, no start/end).
        if ge.status == "cancelled" or not ge.start or not ge.end:
            skipped += 1
            continue
        start_at = _parse_google_ts(ge.start, ge.start_timezone)
        end_at = _parse_google_ts(ge.end, ge.end_timezone)
        if end_at <= start_at:
            # Defensive: Google sometimes emits zero-length stubs.
            skipped += 1
            continue
        title = (ge.summary or "(no title)")[:300]
        if ge.id in existing:
            row = existing[ge.id]
            if (
                row.title == title
                and row.start_at == start_at
                and row.end_at == end_at
                and row.location == ge.location
            ):
                skipped += 1
                continue
            await optimistic_update(
                session,
                Event,
                pk=row.id,
                expected_version=row.version,
                values={
                    "title": title,
                    "start_at": start_at,
                    "end_at": end_at,
                    "location": ge.location,
                },
            )
            updated += 1
        else:
            session.add(
                Event(
                    org_id=org_id,
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    location=ge.location,
                    external_provider=_EXTERNAL_PROVIDER,
                    external_id=ge.id,
                    external_subscription_id=subscription_id,
                )
            )
            ingested += 1
    await session.execute(
        update(CalendarSubscription)
        .where(CalendarSubscription.id == subscription_id)
        .values(
            status=GoogleCalendarStatus.active,
            last_error=None,
            last_sync_at=dt.datetime.now(tz=dt.UTC),
        )
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="google_calendar_subscription",
        entity_id=subscription_id,
        action="sync",
        diff={
            "ingested": str(ingested),
            "updated": str(updated),
            "skipped": str(skipped),
        },
    )
    return SyncResult(
        subscription_id=subscription_id,
        ingested=ingested,
        updated=updated,
        skipped=skipped,
        ok=True,
    )


async def push_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_id: uuid.UUID,
    subscription_id: uuid.UUID,
    client: GoogleApiClient | None = None,
) -> str:
    """Push a Flow event up to Google under the given subscription.
    Returns the Google event id. Subsequent ingests will reconcile it
    (the row gets ``external_*`` set so future syncs deduplicate)."""
    await require_role(session, org_id, actor_id, Role.member)
    ev = (
        await session.execute(select(Event).where(Event.id == event_id))
    ).scalar_one_or_none()
    if ev is None:
        raise NotFoundError(MessageCode.EVENT_NOT_FOUND)
    sub = await get_subscription(session, org_id=org_id, subscription_id=subscription_id)
    api = client or google_api_client()
    try:
        access_token = await _refresh_access_token(
            refresh_token=decrypt_secret(sub.refresh_token_encrypted), client=api
        )
        body = {
            "summary": ev.title,
            "start": {"dateTime": ev.start_at.astimezone(dt.UTC).isoformat()},
            "end": {"dateTime": ev.end_at.astimezone(dt.UTC).isoformat()},
        }
        if ev.location:
            body["location"] = ev.location
        result = await api.insert_event(
            access_token=access_token,
            calendar_id=sub.google_calendar_id,
            body=body,
        )
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(MessageCode.GOOGLE_CALENDAR_API_ERROR, detail=str(exc)) from exc
    await optimistic_update(
        session,
        Event,
        pk=ev.id,
        expected_version=ev.version,
        values={
            "external_provider": _EXTERNAL_PROVIDER,
            "external_id": result.id,
            "external_subscription_id": subscription_id,
        },
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="event",
        entity_id=event_id,
        action="push_google",
        diff={"google_event_id": result.id},
    )
    return result.id
