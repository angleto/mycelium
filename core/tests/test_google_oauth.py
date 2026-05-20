"""Google OAuth 2.0 + Calendar (epic #125 P1).

DB-backed; the only seam is the Google HTTP boundary
(``GoogleApiClient`` Protocol), which a fake replaces. Covers:

  - signed state issue/verify (HMAC + freshness window + tampering);
  - OAuth start endpoint shape (URL params, scope filter, fail-closed
    when Google is not configured);
  - OAuth callback success path: scope=gmail creates an EmailAccount;
    scope=calendar creates a CalendarSubscription; scope=both does both;
  - ingest idempotency: same Google event id twice = one Event row;
  - calendar push round-trip: pushed event acquires external_id and the
    next ingest does not duplicate it;
  - RLS isolation: org A cannot see org B's subscription;
  - access_token_for refresh path for a gmail EmailAccount.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from flow_core.config import get_settings
from flow_core.crypto import decrypt_secret
from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.google_api import (
    GoogleEvent,
    TokenResponse,
    set_google_api_client_override,
)
from flow_core.google_oauth_state import (
    DEFAULT_TTL_SECONDS,
    issue_state,
    verify_state,
)
from flow_core.models.calendar import WorkingCalendar
from flow_core.models.email import EmailAccount, EmailProvider
from flow_core.models.event import Event
from flow_core.models.google_calendar import CalendarSubscription
from flow_core.services import google_calendar as gcal_svc
from flow_core.services.auth import signup

# ---- Fake Google client (the HTTP boundary). --------------------------


class FakeGoogleApiClient:
    def __init__(
        self,
        *,
        events: list[GoogleEvent] | None = None,
        access_token: str = "access-1",
        refresh_token: str | None = "refresh-1",
        id_token: str | None = None,
        fail_refresh: bool = False,
        fail_exchange: bool = False,
    ) -> None:
        self.events = events or []
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.id_token = id_token
        self.fail_refresh = fail_refresh
        self.fail_exchange = fail_exchange
        self.exchanges: list[dict[str, str]] = []
        self.refreshes: list[dict[str, str]] = []
        self.inserted: list[dict[str, Any]] = []

    async def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> TokenResponse:
        if self.fail_exchange:
            raise RuntimeError("exchange failed")
        self.exchanges.append({"code": code, "client_id": client_id})
        return TokenResponse(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=3600,
            token_type="Bearer",
            scope="openid email",
            id_token=self.id_token,
        )

    async def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        if self.fail_refresh:
            raise RuntimeError("refresh failed")
        self.refreshes.append({"refresh_token": refresh_token})
        return TokenResponse(
            access_token=self.access_token,
            refresh_token=None,
            expires_in=3600,
            token_type="Bearer",
            scope="",
        )

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        updated_min: str | None = None,
        page_token: str | None = None,
    ) -> tuple[list[GoogleEvent], str | None]:
        return list(self.events), None

    async def insert_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        body: dict[str, Any],
    ) -> GoogleEvent:
        gid = f"g-{len(self.inserted) + 1}"
        self.inserted.append(body)
        return GoogleEvent(
            id=gid,
            summary=body.get("summary"),
            description=None,
            location=body.get("location"),
            start=body["start"]["dateTime"],
            end=body["end"]["dateTime"],
        )


@pytest.fixture(autouse=True)
def _reset_google_client_override():
    yield
    set_google_api_client_override(None)


@pytest.fixture
def _configure_google_oauth(monkeypatch: pytest.MonkeyPatch):
    """Patch get_settings so the OAuth router thinks Google is configured.
    The fixture is opt-in (tests that need the router gate)."""
    s = get_settings()
    monkeypatch.setattr(s, "google_client_id", "fake-client-id")
    monkeypatch.setattr(s, "google_client_secret", "fake-client-secret")
    monkeypatch.setattr(s, "google_redirect_uri", "http://localhost:8000/oauth/google/callback")
    yield


# ---- State signing ----------------------------------------------------


def test_state_round_trip() -> None:
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    tok = issue_state(user_id=user_id, org_id=org_id, scope="both")
    parsed = verify_state(tok)
    assert parsed.user_id == user_id
    assert parsed.org_id == org_id
    assert parsed.scope == "both"
    assert parsed.exp > int(time.time())


def test_state_tampering_rejected() -> None:
    tok = issue_state(user_id=uuid.uuid4(), org_id=uuid.uuid4(), scope="gmail")
    # Flip a byte in the payload segment.
    body, sig = tok.split(".")
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + sig
    with pytest.raises(DomainError):
        verify_state(tampered)


def test_state_expired_rejected() -> None:
    tok = issue_state(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope="calendar",
        ttl_seconds=-1,
    )
    with pytest.raises(DomainError):
        verify_state(tok)


def test_state_garbage_rejected() -> None:
    with pytest.raises(DomainError):
        verify_state("not.a.real.token")


# ---- OAuth callback ---------------------------------------------------


async def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup_owner(name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=await _email(), password="pw-strong-123", org_name=name)
    return r.org_id, r.user_id


def _id_token(email: str) -> str:
    """A minimal unsigned JWT shape: header.payload.signature; only the
    payload's email claim is read."""
    import base64
    import json

    body = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": "google-1"}).encode()
    ).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


async def _seed_default_calendar(org_id: uuid.UUID, user_id: uuid.UUID) -> WorkingCalendar:
    async with tenant_session(str(org_id), str(user_id)) as s:
        existing = (
            await s.execute(select(WorkingCalendar).where(WorkingCalendar.is_default.is_(True)))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        cal = WorkingCalendar(
            org_id=org_id,
            name="Default",
            is_default=True,
            timezone="Europe/Rome",
            weekly_hours={"mon": [["09:00", "17:00"]]},
        )
        s.add(cal)
        await s.flush()
        return cal


def _client_for_app(app, configure_google: bool = True):
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_callback_creates_gmail_account_and_calendar_subscription(
    _configure_google_oauth,
) -> None:
    """End-to-end: scope=both -> the callback stores both the gmail
    EmailAccount (refresh_token in Fernet envelope) and the
    CalendarSubscription (also Fernet)."""
    from flow_api.app import create_app

    org_id, user_id = await _signup_owner("OauthBoth")
    await _seed_default_calendar(org_id, user_id)

    fake = FakeGoogleApiClient(
        refresh_token="rt-abc",
        id_token=_id_token("me@example.test"),
    )
    set_google_api_client_override(lambda: fake)

    state = issue_state(user_id=user_id, org_id=org_id, scope="both")
    app = create_app()
    async with _client_for_app(app) as cx:
        r = await cx.get(
            "/oauth/google/callback",
            params={"code": "auth-code-1", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "/settings?google=connected" in r.headers["location"]
    assert len(fake.exchanges) == 1

    async with tenant_session(str(org_id), str(user_id)) as s:
        acc = (
            await s.execute(
                select(EmailAccount).where(EmailAccount.email_address == "me@example.test")
            )
        ).scalar_one()
        assert acc.provider is EmailProvider.gmail
        assert acc.secret_encrypted != "rt-abc"
        assert decrypt_secret(acc.secret_encrypted) == "rt-abc"
        sub = (await s.execute(select(CalendarSubscription))).scalar_one()
        assert sub.user_id == user_id
        assert decrypt_secret(sub.refresh_token_encrypted) == "rt-abc"


async def test_callback_rejects_bad_state(_configure_google_oauth) -> None:
    from flow_api.app import create_app

    app = create_app()
    async with _client_for_app(app) as cx:
        r = await cx.get(
            "/oauth/google/callback",
            params={"code": "auth-code-1", "state": "tampered.state"},
            follow_redirects=False,
        )
    # DomainError -> 400 via the app exception handler.
    assert r.status_code == 400
    assert r.json()["code"] == "oauth.state_invalid"


async def test_callback_fails_when_google_not_configured() -> None:
    from flow_api.app import create_app

    app = create_app()
    org_id, user_id = await _signup_owner("OauthUnconf")
    state = issue_state(user_id=user_id, org_id=org_id, scope="gmail")
    async with _client_for_app(app) as cx:
        r = await cx.get(
            "/oauth/google/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 400
    assert r.json()["code"] == "oauth.not_configured"


async def test_callback_propagates_exchange_failure(_configure_google_oauth) -> None:
    from flow_api.app import create_app

    set_google_api_client_override(lambda: FakeGoogleApiClient(fail_exchange=True))
    org_id, user_id = await _signup_owner("OauthExFail")
    state = issue_state(user_id=user_id, org_id=org_id, scope="calendar")
    app = create_app()
    async with _client_for_app(app) as cx:
        r = await cx.get(
            "/oauth/google/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 400
    assert r.json()["code"] == "oauth.exchange_failed"


# ---- Calendar sync / push --------------------------------------------


def _ge(gid: str, summary: str, start: str, end: str, **extra: Any) -> GoogleEvent:
    return GoogleEvent(
        id=gid,
        summary=summary,
        description=extra.get("description"),
        location=extra.get("location"),
        start=start,
        end=end,
        start_timezone=extra.get("start_timezone"),
        end_timezone=extra.get("end_timezone"),
        status=extra.get("status"),
    )


async def _connect_subscription(
    org_id: uuid.UUID, user_id: uuid.UUID, refresh_token: str = "rt-1"
) -> uuid.UUID:
    async with tenant_session(str(org_id), str(user_id)) as s:
        cal = WorkingCalendar(
            org_id=org_id,
            name=f"WC-{uuid.uuid4().hex[:6]}",
            is_default=False,
            timezone="Europe/Rome",
            weekly_hours={"mon": [["09:00", "17:00"]]},
        )
        s.add(cal)
        await s.flush()
        sub = await gcal_svc.connect(
            s,
            org_id=org_id,
            actor_id=user_id,
            user_id=user_id,
            our_calendar_id=cal.id,
            refresh_token=refresh_token,
            google_calendar_id="primary",
        )
        return sub.id


async def test_ingest_is_idempotent(_configure_google_oauth) -> None:
    org_id, user_id = await _signup_owner("OauthIngest")
    sub_id = await _connect_subscription(org_id, user_id)
    fake = FakeGoogleApiClient(
        events=[
            _ge(
                "evt-1",
                "Standup",
                "2026-05-21T09:00:00+00:00",
                "2026-05-21T09:30:00+00:00",
            ),
            _ge(
                "evt-2",
                "Lunch",
                "2026-05-21T12:00:00+00:00",
                "2026-05-21T13:00:00+00:00",
            ),
        ]
    )
    set_google_api_client_override(lambda: fake)

    async with tenant_session(str(org_id), str(user_id)) as s:
        r1 = await gcal_svc.sync_subscription(
            s, org_id=org_id, actor_id=user_id, subscription_id=sub_id
        )
        r2 = await gcal_svc.sync_subscription(
            s, org_id=org_id, actor_id=user_id, subscription_id=sub_id
        )
        rows = (await s.execute(select(Event))).scalars().all()
    assert (r1.ingested, r1.updated, r1.skipped) == (2, 0, 0)
    assert (r2.ingested, r2.updated, r2.skipped) == (0, 0, 2)
    assert len(rows) == 2
    assert {r.external_id for r in rows} == {"evt-1", "evt-2"}
    assert all(r.external_provider == "google" for r in rows)
    assert all(r.external_subscription_id == sub_id for r in rows)


async def test_ingest_updates_changed_event(_configure_google_oauth) -> None:
    org_id, user_id = await _signup_owner("OauthIngestUpd")
    sub_id = await _connect_subscription(org_id, user_id)

    fake = FakeGoogleApiClient(
        events=[
            _ge(
                "evt-1",
                "Old title",
                "2026-05-21T09:00:00+00:00",
                "2026-05-21T09:30:00+00:00",
            ),
        ]
    )
    set_google_api_client_override(lambda: fake)
    async with tenant_session(str(org_id), str(user_id)) as s:
        await gcal_svc.sync_subscription(
            s, org_id=org_id, actor_id=user_id, subscription_id=sub_id
        )

    fake.events = [
        _ge(
            "evt-1",
            "New title",
            "2026-05-21T10:00:00+00:00",
            "2026-05-21T10:30:00+00:00",
        ),
    ]
    async with tenant_session(str(org_id), str(user_id)) as s:
        r2 = await gcal_svc.sync_subscription(
            s, org_id=org_id, actor_id=user_id, subscription_id=sub_id
        )
        row = (await s.execute(select(Event).where(Event.external_id == "evt-1"))).scalar_one()
    assert r2.updated == 1
    assert row.title == "New title"


async def test_push_event_round_trip(_configure_google_oauth) -> None:
    org_id, user_id = await _signup_owner("OauthPush")
    sub_id = await _connect_subscription(org_id, user_id)

    fake = FakeGoogleApiClient()
    set_google_api_client_override(lambda: fake)

    async with tenant_session(str(org_id), str(user_id)) as s:
        ev = Event(
            org_id=org_id,
            title="Demo",
            start_at=dt.datetime(2026, 5, 21, 9, 0, tzinfo=dt.UTC),
            end_at=dt.datetime(2026, 5, 21, 10, 0, tzinfo=dt.UTC),
        )
        s.add(ev)
        await s.flush()
        google_event_id = await gcal_svc.push_event(
            s,
            org_id=org_id,
            actor_id=user_id,
            event_id=ev.id,
            subscription_id=sub_id,
        )
        refreshed = (await s.execute(select(Event).where(Event.id == ev.id))).scalar_one()
    assert google_event_id == "g-1"
    assert refreshed.external_id == "g-1"
    assert refreshed.external_subscription_id == sub_id
    assert refreshed.external_provider == "google"
    # The next ingest of the same event-id must NOT duplicate the row.
    fake.events = [
        _ge(
            google_event_id,
            "Demo",
            "2026-05-21T09:00:00+00:00",
            "2026-05-21T10:00:00+00:00",
        )
    ]
    async with tenant_session(str(org_id), str(user_id)) as s:
        r = await gcal_svc.sync_subscription(
            s, org_id=org_id, actor_id=user_id, subscription_id=sub_id
        )
        rows = (await s.execute(select(Event))).scalars().all()
    assert r.ingested == 0
    assert len(rows) == 1


async def test_subscription_rls_isolation() -> None:
    org_a, user_a = await _signup_owner("OauthRlsA")
    org_b, user_b = await _signup_owner("OauthRlsB")
    sub_a = await _connect_subscription(org_a, user_a, refresh_token="rt-a")

    async with tenant_session(str(org_a), str(user_a)) as s:
        rows = (await s.execute(select(CalendarSubscription))).scalars().all()
        assert [r.id for r in rows] == [sub_a]
    async with tenant_session(str(org_b), str(user_b)) as s:
        rows = (await s.execute(select(CalendarSubscription))).scalars().all()
        assert rows == []


# ---- Email service refactor: gmail access_token_for -------------------


async def test_gmail_account_uses_refresh_token_for_access_token(
    _configure_google_oauth,
) -> None:
    """For provider=gmail the connector must receive a freshly-refreshed
    access_token, not the stored refresh_token."""
    from flow_core.crypto import encrypt_secret
    from flow_core.services import email as email_svc

    org_id, user_id = await _signup_owner("OauthGmailRefresh")
    fake = FakeGoogleApiClient(access_token="freshly-minted-token")
    set_google_api_client_override(lambda: fake)
    async with tenant_session(str(org_id), str(user_id)) as s:
        acc = EmailAccount(
            org_id=org_id,
            provider=EmailProvider.gmail,
            email_address="gmail-user@example.test",
            secret_encrypted=encrypt_secret("rt-gmail"),
            imap_host="imap.gmail.com",
            imap_port=993,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
        )
        s.add(acc)
        await s.flush()
        token = await email_svc.access_token_for(acc)
    assert token == "freshly-minted-token"
    assert fake.refreshes == [{"refresh_token": "rt-gmail"}]


async def test_imap_generic_account_uses_stored_secret() -> None:
    """For non-gmail providers ``access_token_for`` is the identity:
    return the decrypted IMAP password unchanged. No Google call."""
    from flow_core.crypto import encrypt_secret
    from flow_core.services import email as email_svc

    org_id, user_id = await _signup_owner("OauthImapNoop")
    fake = FakeGoogleApiClient()  # not configured by default
    set_google_api_client_override(lambda: fake)
    async with tenant_session(str(org_id), str(user_id)) as s:
        acc = EmailAccount(
            org_id=org_id,
            provider=EmailProvider.imap_generic,
            email_address="imap-user@example.test",
            secret_encrypted=encrypt_secret("imap-pw"),
        )
        s.add(acc)
        await s.flush()
        token = await email_svc.access_token_for(acc)
    assert token == "imap-pw"
    assert fake.refreshes == []


async def test_callback_fails_on_missing_refresh_token(_configure_google_oauth) -> None:
    """A returning consent without ``prompt=consent`` would omit
    refresh_token; the callback must reject that explicitly rather than
    silently store nothing usable."""
    from flow_api.app import create_app

    set_google_api_client_override(
        lambda: FakeGoogleApiClient(refresh_token=None, id_token=_id_token("x@example.test"))
    )
    org_id, user_id = await _signup_owner("OauthMissingRt")
    state = issue_state(user_id=user_id, org_id=org_id, scope="gmail")
    app = create_app()
    async with _client_for_app(app) as cx:
        r = await cx.get(
            "/oauth/google/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 400
    assert r.json()["code"] == "oauth.exchange_failed"


# Suppress unused-import warning for httpx (it is used by the test client).
_ = httpx
_ = DEFAULT_TTL_SECONDS
