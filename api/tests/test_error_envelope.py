"""The error envelope every client reads, and the identifier that makes
a reported error findable.

Two defects are covered, and both were invisible from inside the server.

``params`` was computed and dropped. ``concurrency.optimistic_update``
raises the stale-version conflict WITH the version the caller should have
presented -- it goes to the trouble of reading it back -- and the MCP
adapter has always put it on the wire. This adapter did not, so a browser
client learned only "conflict" and had to issue a blind extra read to
find out what the server already knew. That read cannot distinguish "your
copy was stale" from "someone is writing continuously", so a client
retrying on it can spin.

And there was no correlation identifier anywhere. An unhandled fault
returned Starlette's bare "Internal Server Error" with nothing on it to
quote, which is what makes a person describe an error instead of
reporting one -- and the reflex repair for that is to put the exception
text on the screen, which is the leak the rule exists to prevent.

These assert on a synthetic route rather than on a real 409, so the
envelope is tested without standing up a database. The real conflict path
is covered where the domain is.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mycelium_api import correlation
from mycelium_api.app import create_app
from mycelium_core.errors import ConflictError, NotFoundError
from mycelium_core.i18n import MessageCode

VALID_INBOUND = "0123456789abcdef"


def _build_app() -> FastAPI:
    """Built once: create_app wires every router and is expensive enough
    that per-request construction turned eleven assertions into three
    minutes."""
    app = create_app()

    @app.get("/_t/conflict")
    async def _conflict() -> None:
        raise ConflictError(MessageCode.CONFLICT_STALE_VERSION, current_version=7)

    @app.get("/_t/absent")
    async def _absent() -> None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)

    @app.get("/_t/boom")
    async def _boom() -> None:
        raise RuntimeError("a schema name, a path, and a stack live in here")

    return app


_APP = _build_app()


async def _get(path: str, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=_APP, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path, headers=headers or {})


async def test_conflict_carries_the_version_the_caller_should_have_sent() -> None:
    res = await _get("/_t/conflict")
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "concurrency.stale_version"
    assert body["params"] == {"current_version": 7}
    assert body["detail"]


async def test_an_error_with_no_params_omits_the_key_entirely() -> None:
    # An empty dict on the wire would read as "the server considered the
    # question and had nothing to say"; absence reads as "not applicable".
    body = (await _get("/_t/absent")).json()
    assert "params" not in body
    assert body["code"] == "task.not_found"


async def test_every_error_carries_a_correlation_id_in_body_and_header() -> None:
    res = await _get("/_t/conflict")
    assert res.headers[correlation.HEADER] == res.json()["correlation_id"]
    assert len(res.json()["correlation_id"]) >= 8


async def test_a_successful_response_carries_it_too() -> None:
    # The identifier has to exist on the request that WORKED as well, or a
    # user action that fans out cannot be followed through the log.
    res = await _get("/healthz")
    assert res.status_code == 200
    assert res.headers[correlation.HEADER]


async def test_a_callers_identifier_is_adopted_so_one_action_is_one_thread() -> None:
    res = await _get("/_t/conflict", {correlation.HEADER: VALID_INBOUND})
    assert res.headers[correlation.HEADER] == VALID_INBOUND
    assert res.json()["correlation_id"] == VALID_INBOUND


@pytest.mark.parametrize(
    "hostile",
    [
        "short",
        "x" * 65,
        "has spaces",
        "line\nbreak GET /admin HTTP/1.1",
        "semi;colon",
    ],
)
async def test_an_unusable_identifier_is_replaced_not_echoed(hostile: str) -> None:
    # It reaches a log file, so it is untrusted input: unbounded length
    # blows up a log line and a newline forges an entry. The request is
    # still served -- only the label was bad.
    res = await _get("/_t/conflict", {correlation.HEADER: hostile})
    assert res.status_code == 409
    assert res.headers[correlation.HEADER] != hostile


async def test_an_unhandled_fault_says_nothing_about_itself() -> None:
    res = await _get("/_t/boom")
    assert res.status_code == 500
    body = res.json()
    assert body == {
        "code": "internal",
        "detail": "internal error",
        "correlation_id": body["correlation_id"],
    }
    assert "schema name" not in res.text
    assert "RuntimeError" not in res.text
    assert res.headers[correlation.HEADER] == body["correlation_id"]
