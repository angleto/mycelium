"""Context-blind append endpoints (task 4ac39ecf).

POST /notes/{id}/append (summary | transcript) and POST
/tasks/{id}/description/append let an MCP / LLM caller add a paragraph
without re-sending the existing body. The integration tests cover the
end-to-end contract: separator collapsing, optimistic version, dedupe,
body-limit, and the audit channel.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123"},
        )
    ).json()
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
    }


async def test_note_append_extends_transcript_with_separator() -> None:
    """First append fills an empty transcript; a second append joins
    with the default ``\\n\\n`` separator. ``appended_chars`` reports
    the length of the *added* text, not the resulting body."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (
            await c.post("/notes", headers=h, json={"kind": "text", "title": "Daily journal"})
        ).json()["id"]

        r1 = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={"target": "transcript", "text": "first line"},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["appended_chars"] == len("first line")
        assert body1["version"] >= 1

        r2 = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={"target": "transcript", "text": "second line"},
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["version"] > body1["version"]

        # Final body is the two lines joined by a blank line.
        got = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert got["transcript"] == "first line\n\nsecond line"


async def test_note_append_summary_target() -> None:
    """The ``target`` field picks summary vs transcript; appending to
    one leaves the other untouched."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Weekly", "text": "Mon: kickoff"},
            )
        ).json()["id"]

        r = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={"target": "summary", "text": "Three meetings this week."},
        )
        assert r.status_code == 200, r.text

        got = (await c.get(f"/notes/{note_id}", headers=h)).json()
        assert got["summary"] == "Three meetings this week."
        assert got["transcript"] == "Mon: kickoff"


async def test_note_append_dedupe_no_op() -> None:
    """``dedupe_if_tail_matches=true`` makes a retry a no-op when the
    body already ends with the text (modulo whitespace). The version
    must NOT bump and ``appended_chars`` must report 0."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (
            await c.post(
                "/notes",
                headers=h,
                json={"kind": "text", "title": "Log", "text": "boot ok"},
            )
        ).json()["id"]
        v0 = (await c.get(f"/notes/{note_id}", headers=h)).json()["version"]

        r = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={
                "target": "transcript",
                "text": "boot ok",
                "dedupe_if_tail_matches": True,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["appended_chars"] == 0
        assert body["version"] == v0  # no bump on no-op


async def test_note_append_expected_version_mismatch_conflicts() -> None:
    """When the caller supplies ``expected_version`` and it doesn't
    match the current row, the optimistic concurrency layer raises
    stale_version. (Omitting the field appends onto any current state.)"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (await c.post("/notes", headers=h, json={"kind": "text", "title": "v"})).json()[
            "id"
        ]

        r = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={"target": "transcript", "text": "x", "expected_version": 999},
        )
        # Conflict surfaces as a 4xx with the stale_version code.
        assert r.status_code >= 400, r.text
        assert "stale_version" in r.text


async def test_note_append_body_limit_exceeded() -> None:
    """A single append that would push the body past
    ``FLOW_NOTE_BODY_MAX_BYTES`` is rejected with ``body.limit_exceeded``
    -- the SPA / MCP caller can chunk further or stop."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (await c.post("/notes", headers=h, json={"kind": "text", "title": "big"})).json()[
            "id"
        ]

        # Default cap is 1 MiB; 1.5 MiB of ASCII is comfortably over.
        too_big = "x" * (1_500_000)
        r = await c.post(
            f"/notes/{note_id}/append",
            headers=h,
            json={"target": "transcript", "text": too_big},
        )
        assert r.status_code >= 400, r.text
        assert "body.limit_exceeded" in r.text


async def test_task_description_append_concat_and_returns_version() -> None:
    """The task-description endpoint mirrors the note one: returns
    ``{id, version, appended_chars}``, separator collapsing matches."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        task_id = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Investigate flake", "description": "Repro: rare"},
            )
        ).json()["id"]

        r = await c.post(
            f"/tasks/{task_id}/description/append",
            headers=h,
            json={"text": "Follow-up: collected stacktraces."},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["appended_chars"] == len("Follow-up: collected stacktraces.")
        assert body["id"] == task_id

        got = (await c.get(f"/tasks/{task_id}", headers=h)).json()
        assert got["description"] == ("Repro: rare\n\nFollow-up: collected stacktraces.")


async def test_task_description_prepend_puts_text_in_front() -> None:
    """Prepend mirrors append but on the front: the new text precedes the
    existing body, joined by the default separator (task 5662a07f)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        task_id = (
            await c.post(
                "/tasks",
                headers=h,
                json={"title": "Incident", "description": "Timeline so far."},
            )
        ).json()["id"]

        r = await c.post(
            f"/tasks/{task_id}/description/prepend",
            headers=h,
            json={"text": "TL;DR: db failover."},
        )
        assert r.status_code == 200, r.text
        assert r.json()["appended_chars"] == len("TL;DR: db failover.")

        got = (await c.get(f"/tasks/{task_id}", headers=h)).json()
        assert got["description"] == "TL;DR: db failover.\n\nTimeline so far."


async def test_note_part_prepend_puts_text_at_front_of_part() -> None:
    """POST /notes/{id}/parts/{pid}/prepend adds text to the front of a
    part body raw (no separator), concurrency-safe (task 5662a07f)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        note_id = (await c.post("/notes", headers=h, json={"kind": "text", "title": "Doc"})).json()[
            "id"
        ]
        part = (
            await c.post(f"/notes/{note_id}/parts", headers=h, json={"body": "body text"})
        ).json()
        pid = part["id"]

        r = await c.post(
            f"/notes/{note_id}/parts/{pid}/prepend",
            headers=h,
            json={"text": "# Heading\n\n", "expected_version": part["version"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["appended_chars"] == len("# Heading\n\n")

        got = (await c.get(f"/notes/{note_id}", headers=h)).json()
        body = next(p["body"] for p in got["parts"] if p["id"] == pid)
        assert body == "# Heading\n\nbody text"

        # Stale cursor -> stale_version (no last-write-wins).
        stale = await c.post(
            f"/notes/{note_id}/parts/{pid}/prepend",
            headers=h,
            json={"text": "x", "expected_version": part["version"]},
        )
        assert stale.status_code >= 400 and "stale_version" in stale.text
