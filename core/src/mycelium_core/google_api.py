"""Google API HTTP boundary (epic #125 P1).

A single ``Protocol`` covers the only Google endpoints Mycelium touches:
token exchange / refresh, calendar.list, events.list, events.insert.
The concrete ``HttpxGoogleApiClient`` uses ``httpx.AsyncClient``; tests
inject a fake (same legitimate seam as ``EmailConnector`` / LLM
provider). No heavy SDK (google-api-python-client): three REST calls do
not justify the dependency. All payloads are simple dicts (``Any``);
the service layer pulls only the fields it documents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

# Google endpoints (constants, never user input).
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# OAuth scopes Mycelium requests. ``openid email`` to identify the Google
# account; ``gmail.modify`` to fetch + mark + send via Gmail XOAUTH2;
# ``calendar`` to read/write events.
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_OPENID = "openid"
SCOPE_EMAIL = "email"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    token_type: str
    scope: str
    id_token: str | None = None


@dataclass(frozen=True)
class GoogleEvent:
    """The subset of a calendar.events resource Mycelium consumes."""

    id: str
    summary: str | None
    description: str | None
    location: str | None
    start: str  # ISO 8601 string (date or dateTime; Google's shape)
    end: str
    start_timezone: str | None = None
    end_timezone: str | None = None
    status: str | None = None


@runtime_checkable
class GoogleApiClient(Protocol):
    async def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> TokenResponse: ...

    async def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse: ...

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        updated_min: str | None = None,
        page_token: str | None = None,
    ) -> tuple[list[GoogleEvent], str | None]: ...

    async def insert_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        body: dict[str, Any],
    ) -> GoogleEvent: ...


class HttpxGoogleApiClient:
    """Concrete REST client. Not exercised in CI: tests use a fake."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self._timeout = timeout

    async def _post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(url, data=data)
            r.raise_for_status()
            payload: dict[str, Any] = r.json()
            return payload

    async def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> TokenResponse:
        body = await self._post_form(
            TOKEN_URL,
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return _token_response(body)

    async def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> TokenResponse:
        body = await self._post_form(
            TOKEN_URL,
            {
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
        return _token_response(body)

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        updated_min: str | None = None,
        page_token: str | None = None,
    ) -> tuple[list[GoogleEvent], str | None]:
        params: dict[str, str] = {"singleEvents": "true", "maxResults": "250"}
        if updated_min:
            params["updatedMin"] = updated_min
        if page_token:
            params["pageToken"] = page_token
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.get(
                f"{CALENDAR_API}/calendars/{calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
        events = [_google_event(item) for item in data.get("items", [])]
        return events, data.get("nextPageToken")

    async def insert_event(
        self,
        *,
        access_token: str,
        calendar_id: str,
        body: dict[str, Any],
    ) -> GoogleEvent:
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(
                f"{CALENDAR_API}/calendars/{calendar_id}/events",
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
        return _google_event(data)


def _token_response(body: dict[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=str(body["access_token"]),
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in", 0)),
        token_type=str(body.get("token_type", "Bearer")),
        scope=str(body.get("scope", "")),
        id_token=body.get("id_token"),
    )


def _google_event(item: dict[str, Any]) -> GoogleEvent:
    start = item.get("start", {}) or {}
    end = item.get("end", {}) or {}
    start_ts = str(start.get("dateTime") or start.get("date") or "")
    end_ts = str(end.get("dateTime") or end.get("date") or "")
    return GoogleEvent(
        id=str(item["id"]),
        summary=item.get("summary"),
        description=item.get("description"),
        location=item.get("location"),
        start=start_ts,
        end=end_ts,
        start_timezone=start.get("timeZone"),
        end_timezone=end.get("timeZone"),
        status=item.get("status"),
    )


_FactoryFn = Callable[[], GoogleApiClient]
_override: _FactoryFn | None = None


def set_google_api_client_override(fn: _FactoryFn | None) -> None:
    """Test seam: replace the network factory with a fake. Prod leaves
    this None and uses the real REST client. Analogous to the email
    connector / attachment-store overrides; never set in production
    code."""
    global _override
    _override = fn


def google_api_client() -> GoogleApiClient:
    if _override is not None:
        return _override()
    return HttpxGoogleApiClient()
