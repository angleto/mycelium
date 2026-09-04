"""A capture that timed out must not become two.

A create over a flaky connection has an UNKNOWN outcome: the client
cannot tell "it never arrived" from "it arrived and the answer was lost".
Without a claim the only honest thing such a client can offer is "go and
look", and the only dishonest one is a retry button that quietly files a
second task.

The mechanism already existed for the invoice API, where a retry must
never file a second fiscal document. What was not general was the claim's
PRINCIPAL: it required an issuer profile, so a person capturing into a
workspace could not claim anything. These assert the widened claim, and
in particular the two properties that make it worth having rather than
merely present -- that a replay returns the FIRST answer rather than a
second row, and that the same key reused for a different request is
refused instead of answered with somebody else's result.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


async def _owner(c: AsyncClient, name: str = "IDEM") -> dict[str, str]:
    su = (
        await c.post(
            "/auth/signup",
            json={
                "email": f"{uuid.uuid4().hex[:10]}@example.test",
                "password": "pw-strong-123",
                "workspace_name": name,
            },
        )
    ).json()
    return {
        "Authorization": f"Bearer {su['token']}",
        "X-Workspace-Id": su["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_a_retried_task_capture_returns_the_first_task() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        key = uuid.uuid4().hex
        body = {"title": "captured from a page"}

        first = await c.post("/tasks", headers={**h, "Idempotency-Key": key}, json=body)
        assert first.status_code == 200, first.text
        second = await c.post("/tasks", headers={**h, "Idempotency-Key": key}, json=body)
        assert second.status_code == 200, second.text

        assert first.json()["id"] == second.json()["id"]
        # And it is the same ANSWER, not just the same id: the caller gets
        # what it was told the first time.
        assert first.json() == second.json()

        listed = await c.get("/tasks", headers=h, params={"q": "captured from a page"})
        assert len(listed.json()) == 1


async def test_a_retried_note_capture_returns_the_first_note() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        key = uuid.uuid4().hex
        body = {"kind": "text", "title": "a page", "text": "> quoted\n\n[a page](https://x.test/)"}

        first = await c.post("/notes", headers={**h, "Idempotency-Key": key}, json=body)
        assert first.status_code == 200, first.text
        second = await c.post("/notes", headers={**h, "Idempotency-Key": key}, json=body)
        assert first.json()["id"] == second.json()["id"]

        listed = await c.get("/notes", headers=h, params={"q": "quoted"})
        assert len(listed.json()) == 1


async def test_the_same_key_with_a_different_body_is_refused() -> None:
    """Otherwise a client that reuses a key by mistake is answered with a
    result belonging to a different question, which is worse than an
    error: it looks like it worked."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        key = uuid.uuid4().hex
        await c.post("/tasks", headers={**h, "Idempotency-Key": key}, json={"title": "one"})
        clash = await c.post("/tasks", headers={**h, "Idempotency-Key": key}, json={"title": "two"})
        assert clash.status_code == 422, clash.text


async def test_two_different_keys_create_two_things() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        body = {"title": "same title, deliberately"}
        a = await c.post("/tasks", headers={**h, "Idempotency-Key": uuid.uuid4().hex}, json=body)
        b = await c.post("/tasks", headers={**h, "Idempotency-Key": uuid.uuid4().hex}, json=body)
        assert a.json()["id"] != b.json()["id"]


async def test_the_header_is_optional_and_absence_behaves_as_before() -> None:
    """Every client that exists today creates without one. Requiring the
    header would have made this a breaking change for all of them."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        body = {"title": "no key here"}
        a = await c.post("/tasks", headers=h, json=body)
        b = await c.post("/tasks", headers=h, json=body)
        assert a.status_code == 200 and b.status_code == 200
        assert a.json()["id"] != b.json()["id"]


async def test_a_key_is_scoped_to_the_workspace_it_was_used_in() -> None:
    """The actor claim carries org_id, unlike the issuer claim, which does
    not need it because an issuer profile already implies one workspace.
    One person retrying in two workspaces with a colliding client-generated
    key must not be told the second capture was a replay of the first."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        second = (await c.post("/workspaces", headers=h, json={"name": "Second"})).json()
        key = uuid.uuid4().hex
        body = {"title": "same key, two workspaces"}

        here = await c.post("/tasks", headers={**h, "Idempotency-Key": key}, json=body)
        there = await c.post(
            "/tasks",
            headers={**h, "X-Workspace-Id": str(second["id"]), "Idempotency-Key": key},
            json=body,
        )
        assert here.status_code == 200 and there.status_code == 200, there.text
        assert here.json()["id"] != there.json()["id"]


async def test_one_persons_key_is_not_anothers() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        mine = await _owner(c)
        theirs = await _owner(c)
        key = uuid.uuid4().hex
        body = {"title": "a shared key by accident"}
        a = await c.post("/tasks", headers={**mine, "Idempotency-Key": key}, json=body)
        b = await c.post("/tasks", headers={**theirs, "Idempotency-Key": key}, json=body)
        assert a.json()["id"] != b.json()["id"]
