"""Thin HTTP client wrapped around ``httpx`` with Mycelium-specific defaults.

Single-threaded synchronous client. The CLI does one logical call per
command, so async buys us nothing and complicates ergonomics
(KeyboardInterrupt, $EDITOR shell-outs, sox subprocess).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from mycelium_cli import __version__
from mycelium_cli.credentials import Credential


class CLIError(RuntimeError):
    """User-facing error: rendered without a traceback at the top level."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class AuthRequired(CLIError):
    pass


class MfaRequired(CLIError):
    pass


def _build_client(
    base_url: str,
    *,
    bearer: str | None = None,
    workspace_id: str | None = None,
    role: str | None = None,
    admin_mode: bool = False,
    timeout: float = 30.0,
) -> httpx.Client:
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": f"mycelium-cli/{__version__}",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
    if role:
        headers["X-Workspace-Role"] = role
    if admin_mode:
        headers["X-Admin-Mode"] = "1"
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
    )


@contextmanager
def anon_client(base_url: str) -> Iterator[httpx.Client]:
    """Used for /auth/login (no token yet)."""
    client = _build_client(base_url)
    try:
        yield client
    finally:
        client.close()


@contextmanager
def jwt_client(base_url: str, jwt: str) -> Iterator[httpx.Client]:
    """Used between /auth/login and /agent-tokens during login flow."""
    client = _build_client(base_url, bearer=jwt)
    try:
        yield client
    finally:
        client.close()


@contextmanager
def authed_client(
    base_url: str,
    cred: Credential,
    *,
    role: str | None = None,
    admin_mode: bool = False,
) -> Iterator[httpx.Client]:
    """The default client for all post-login commands."""
    client = _build_client(
        base_url,
        bearer=cred.token,
        workspace_id=cred.workspace_id,
        role=role,
        admin_mode=admin_mode,
    )
    try:
        yield client
    finally:
        client.close()


def raise_for_response(resp: httpx.Response) -> None:
    """Translate the API's error envelope into a CLIError with i18n code.

    Mycelium's error contract surfaces ``{"detail": {"code": "...",
    "message": "..."}}`` (i18n MessageCode). On unexpected payloads we
    fall back to the raw text so the user still sees something useful.
    """
    if resp.is_success:
        return
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    code: str | None = None
    message: str | None = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            code = _opt(detail.get("code"))
            message = _opt(detail.get("message"))
        elif isinstance(detail, list):
            # Pydantic 422 envelope: list of {loc, msg, type, ...}. We
            # drop the leading "body" segment from loc (always there on
            # FastAPI body validation) and join the remaining path so
            # the user sees ``title: Field required`` instead of a wall
            # of JSON.
            parts: list[str] = []
            for item in detail:
                if not isinstance(item, dict):
                    continue
                raw_loc = item.get("loc") or []
                loc = ".".join(
                    str(x) for x in (raw_loc[1:] if raw_loc and raw_loc[0] == "body" else raw_loc)
                )
                msg = str(item.get("msg") or "invalid")
                parts.append(f"{loc}: {msg}" if loc else msg)
            message = "; ".join(parts) or None
        elif isinstance(detail, str):
            message = detail
        message = message or _opt(body.get("message"))
        code = code or _opt(body.get("code"))
    text = message or (resp.text.strip() if resp.text else "")
    label = f"{resp.status_code}"
    if code:
        label = f"{resp.status_code} {code}"
    msg = f"{label}: {text}" if text else label
    if code == "auth.mfa_required":
        raise MfaRequired(msg)
    if resp.status_code in (401, 403):
        raise AuthRequired(msg, hint="Run: mycelium auth login")
    raise CLIError(msg)


def _opt(v: Any) -> str | None:
    return str(v) if isinstance(v, str) and v else None
