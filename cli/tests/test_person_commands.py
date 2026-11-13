"""The commands reserved to a person authenticate as the person, every time.

The stored token is an agent credential: every agent session on the machine can
read the file it lives in, so the server refuses it the routes reserved to a
person (account, MFA, workspaces, credentials). Those commands therefore ask
for the password, run on the session it opens, and close that session after.

What is asserted is the wire, not the prompt text: the stored token must never
be the bearer of a person-only call, and the session opened for the command
must be closed. The API is stubbed with httpx.MockTransport, offline.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import httpx
from typer.testing import CliRunner

from mycelium_cli.cmds import _common
from mycelium_cli.config import Profile
from mycelium_cli.credentials import Credential
from mycelium_cli.main import app

runner = CliRunner()

_PAT = "mycelium_at_stored-on-disk"
_JWT = "jwt-for-this-command-only"


def _wire(monkeypatch, responses: dict[tuple[str, str], httpx.Response]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/auth/login"):
            return httpx.Response(200, json={"token": _JWT})
        if key == ("POST", "/auth/logout"):
            return httpx.Response(204)
        return responses.get(key, httpx.Response(404, json={"detail": "unstubbed"}))

    def _factory(bearer: str | None):
        @contextlib.contextmanager
        def _cm(*_a, **_k) -> Iterator[httpx.Client]:
            headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
            with httpx.Client(
                transport=httpx.MockTransport(handler), base_url="http://test", headers=headers
            ) as c:
                yield c

        return _cm

    cred = Credential(
        token=_PAT,
        token_id="tok-1",
        user_id="u-1",
        email="person@example.test",
        workspace_id="ws-1",
        expires_at=None,
    )
    monkeypatch.setattr(
        _common, "active_profile", lambda: ("default", Profile(base_url="http://test"), cred)
    )
    monkeypatch.setattr(_common, "anon_client", _factory(None))
    monkeypatch.setattr(_common, "jwt_client", lambda _base, jwt: _factory(jwt)())
    monkeypatch.setattr(_common, "authed_client", _factory(_PAT))
    return seen


def _bearers(seen: list[httpx.Request], path: str) -> list[str]:
    return [r.headers.get("Authorization", "") for r in seen if r.url.path == path]


def test_mfa_status_runs_on_a_fresh_person_session(monkeypatch) -> None:
    seen = _wire(
        monkeypatch, {("GET", "/mfa/status"): httpx.Response(200, json={"enabled": False})}
    )
    res = runner.invoke(app, ["auth", "mfa", "status"], input="the-password\n")
    assert res.exit_code == 0, res.output

    login = [r for r in seen if r.url.path == "/auth/login"]
    assert len(login) == 1
    assert b"the-password" in login[0].content
    assert b"person@example.test" in login[0].content

    assert _bearers(seen, "/mfa/status") == [f"Bearer {_JWT}"]
    assert seen[-1].url.path == "/auth/logout"
    assert seen[-1].headers.get("Authorization") == f"Bearer {_JWT}"
    assert not any(_PAT in r.headers.get("Authorization", "") for r in seen)


def test_workspace_list_runs_on_a_fresh_person_session(monkeypatch) -> None:
    seen = _wire(
        monkeypatch,
        {("GET", "/workspaces"): httpx.Response(200, json=[{"id": "ws-1", "name": "P"}])},
    )
    res = runner.invoke(app, ["--json", "workspace", "list"], input="the-password\n")
    assert res.exit_code == 0, res.output
    assert _bearers(seen, "/workspaces") == [f"Bearer {_JWT}"]
    assert seen[-1].url.path == "/auth/logout"
    assert not any(_PAT in r.headers.get("Authorization", "") for r in seen)


def test_a_failed_command_still_closes_the_session(monkeypatch) -> None:
    seen = _wire(
        monkeypatch,
        {("POST", "/mfa/setup"): httpx.Response(500, json={"detail": "boom"})},
    )
    res = runner.invoke(app, ["auth", "mfa", "setup"], input="the-password\n")
    assert res.exit_code != 0
    assert seen[-1].url.path == "/auth/logout"


def test_ordinary_commands_keep_using_the_stored_token(monkeypatch) -> None:
    """The step-up is for the person-only commands, not for everything."""
    seen = _wire(monkeypatch, {("GET", "/tasks"): httpx.Response(200, json=[])})
    with _common.client() as c:
        c.get("/tasks")
    assert _bearers(seen, "/tasks") == [f"Bearer {_PAT}"]
    assert not any(r.url.path == "/auth/login" for r in seen)
