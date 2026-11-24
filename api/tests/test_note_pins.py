"""A person's pinned notes (migration 0033).

What a pin promises, each asserted against the running API: it is the
person's and not the workspace's, it outlives the note being archived, it
changes nothing on the note and is not counted as working on it (the garden
must keep ageing a pinned note nobody opens), the pinned list is newest pin
first, and both directions are idempotent because the control is a toggle
pressed from several lists at once.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from mycelium_api.main import app


async def _owner(c: AsyncClient, name: str = "NOTE PINS") -> dict[str, str]:
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


async def _note(c: AsyncClient, h: dict[str, str], title: str) -> dict:
    r = await c.post("/notes", headers=h, json={"kind": "text", "title": title, "text": title})
    assert r.status_code == 200, r.text
    return r.json()


async def _pinned(c: AsyncClient, h: dict[str, str]) -> list[str]:
    r = await c.get("/notes", headers=h, params={"pinned": "true"})
    assert r.status_code == 200, r.text
    return [n["id"] for n in r.json()["items"]]


async def test_pin_lists_newest_first_and_unpin_removes() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a = await _note(c, h, "first pinned")
        b = await _note(c, h, "second pinned")
        await _note(c, h, "never pinned")
        assert await _pinned(c, h) == []

        # Pinned in the reverse of the order they were created in, so the
        # list's order can only come from the pins and not from the notes.
        r = await c.post("/notes/pin", headers=h, json={"note_id": b["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["pinned"] is True and r.json()["pinned_at"]
        await c.post("/notes/pin", headers=h, json={"note_id": a["id"]})
        assert await _pinned(c, h) == [a["id"], b["id"]]

        r = await c.post("/notes/unpin", headers=h, json={"note_id": a["id"]})
        assert r.status_code == 200 and r.json()["pinned"] is False
        assert await _pinned(c, h) == [b["id"]]


async def test_the_pin_order_decides_what_fits_in_the_page() -> None:
    """With more pins than ``limit``, the page is the most recently pinned,
    not the most recently created, and it says it was cut."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        old = await _note(c, h, "old note, pinned last")
        new = await _note(c, h, "new note, pinned first")
        await c.post("/notes/pin", headers=h, json={"note_id": new["id"]})
        await c.post("/notes/pin", headers=h, json={"note_id": old["id"]})
        r = await c.get("/notes", headers=h, params={"pinned": "true", "limit": 1})
        assert r.status_code == 200, r.text
        page = r.json()
        assert [n["id"] for n in page["items"]] == [old["id"]]
        assert page["truncated"] is True and page["next_cursor"] is None


async def test_both_directions_are_idempotent_and_repinning_keeps_the_order() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a = await _note(c, h, "a")
        b = await _note(c, h, "b")
        first = (await c.post("/notes/pin", headers=h, json={"note_id": a["id"]})).json()
        await c.post("/notes/pin", headers=h, json={"note_id": b["id"]})
        again = await c.post("/notes/pin", headers=h, json={"note_id": a["id"]})
        assert again.status_code == 200
        # The first pin's time stands, so pressing it again does not move
        # the note to the top: the order is the order things were pinned in.
        assert again.json()["pinned_at"] == first["pinned_at"]
        assert await _pinned(c, h) == [b["id"], a["id"]]

        for _ in range(2):
            r = await c.post("/notes/unpin", headers=h, json={"note_id": a["id"]})
            assert r.status_code == 200
        assert await _pinned(c, h) == [b["id"]]


async def test_an_archived_note_stays_pinned() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        n = await _note(c, h, "archived but at hand")
        await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
        r = await c.post(
            f"/notes/{n['id']}/archive", headers=h, json={"expected_version": n["version"]}
        )
        assert r.status_code == 200, r.text
        assert await _pinned(c, h) == [n["id"]]
        # And it can be unpinned from there: that is the control the list of
        # pins carries.
        await c.post("/notes/unpin", headers=h, json={"note_id": n["id"]})
        assert await _pinned(c, h) == []


async def test_pinning_changes_nothing_on_the_note_and_is_not_a_touch() -> None:
    """A pin is not an edit: no version bump (an open editor would see a
    conflict), no ``updated_at`` bump (the Recent list would reorder and the
    ``updated_since`` sync would refetch), no revision, and no activity row,
    which is what the garden reads as someone working on the note."""
    from mycelium_core.db import tenant_session
    from mycelium_core.models.activity_log import ActivityLog
    from mycelium_core.models.entity_revision import EntityRevision
    from mycelium_core.models.note import Note
    from mycelium_core.services import note_touch

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = (await c.get("/auth/me", headers=h)).json()
        org = h["X-Workspace-Id"]

        async def trail(note_id: uuid.UUID) -> tuple[int, int, object]:
            # Read in the workspace's own session: the log and the revisions
            # are under row-level security, and a session with no workspace
            # would count zero rows and pass whatever the pin wrote.
            async with tenant_session(org, str(me["user_id"])) as s:
                activity = await s.scalar(
                    select(func.count()).where(ActivityLog.entity_id == note_id)
                )
                revisions = await s.scalar(
                    select(func.count()).where(EntityRevision.entity_id == note_id)
                )
                last = note_touch.last_touch_subquery(uuid.UUID(org), note_id=note_id)
                clock = await s.scalar(
                    select(note_touch.touched_at(last))
                    .select_from(Note)
                    .outerjoin(last, last.c.note_id == Note.id)
                    .where(Note.id == note_id)
                )
            assert clock is not None
            return int(activity or 0), int(revisions or 0), clock

        n = await _note(c, h, "untouched")
        nid = uuid.UUID(n["id"])
        before = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        trail_before = await trail(nid)
        # The reads see this note's trail at all: creating it logged a row.
        assert trail_before[0] >= 1
        r = await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
        assert r.status_code == 200, r.text
        mid = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        trail_mid = await trail(nid)
        await c.post("/notes/unpin", headers=h, json={"note_id": n["id"]})
        after = (await c.get(f"/notes/{n['id']}", headers=h)).json()
        for seen in (mid, after):
            assert seen["version"] == before["version"]
            assert seen["updated_at"] == before["updated_at"]
        assert trail_mid == trail_before
        assert await trail(nid) == trail_before


async def test_a_note_in_the_bin_cannot_be_pinned_and_drops_out_of_the_list() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        n = await _note(c, h, "binned")
        await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
        r = await c.post(
            f"/notes/{n['id']}/delete", headers=h, json={"expected_version": n["version"]}
        )
        assert r.status_code == 200, r.text
        assert await _pinned(c, h) == []
        r = await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
        assert r.status_code == 404, r.text
        # A pin can always be cleared, even on a note in the bin.
        r = await c.post("/notes/unpin", headers=h, json={"note_id": n["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["pinned"] is False


async def test_pins_are_personal() -> None:
    """Another member of the same workspace sees none of my pins, on a note
    both of us can read."""
    from mycelium_core.db import admin_session, tenant_session
    from mycelium_core.models.membership import Membership, Role
    from mycelium_core.services.auth import login, signup

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner_email = f"{uuid.uuid4().hex[:10]}@example.test"
        member_email = f"{uuid.uuid4().hex[:10]}@example.test"
        async with admin_session() as s:
            owner = await signup(s, email=owner_email, password="pw-strong-123", org_name="A")
            member = await signup(s, email=member_email, password="pw-strong-123", org_name="B")
        async with tenant_session(str(owner.org_id), str(owner.user_id)) as s:
            s.add(Membership(org_id=owner.org_id, user_id=member.user_id, role=Role.member))
            await s.flush()
        async with admin_session() as s:
            mine = await login(s, email=owner_email, password="pw-strong-123")
            theirs = await login(s, email=member_email, password="pw-strong-123")
        ws = str(owner.org_id)
        h_me = {"Authorization": f"Bearer {mine.access_token}", "X-Workspace-Id": ws}
        h_them = {"Authorization": f"Bearer {theirs.access_token}", "X-Workspace-Id": ws}

        n = await _note(c, h_me, "shared note, my pin")
        r = await c.get(f"/notes/{n['id']}", headers=h_them)
        assert r.status_code == 200, r.text
        await c.post("/notes/pin", headers=h_me, json={"note_id": n["id"]})
        assert await _pinned(c, h_me) == [n["id"]]
        assert await _pinned(c, h_them) == []
        # Their unpin clears their (absent) pin, never mine.
        await c.post("/notes/unpin", headers=h_them, json={"note_id": n["id"]})
        assert await _pinned(c, h_me) == [n["id"]]


async def test_a_note_of_another_workspace_cannot_be_pinned_or_seen() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c, "MINE")
        other = await _owner(c, "OTHER")
        foreign = await _note(c, other, "not yours")
        r = await c.post("/notes/pin", headers=h, json={"note_id": foreign["id"]})
        assert r.status_code == 404, r.text
        assert await _pinned(c, h) == []
        assert await _pinned(c, other) == []
        # The other workspace's own pins stay out of mine as well.
        await c.post("/notes/pin", headers=other, json={"note_id": foreign["id"]})
        assert await _pinned(c, other) == [foreign["id"]]
        assert await _pinned(c, h) == []


async def test_a_cursor_is_refused_on_the_pinned_list() -> None:
    """The pinned list is one page in pin order; a cursor issued under the
    creation order has nothing to continue, so it is an error, not ignored.
    The cursor is a real one the list issued, so the refusal can only come
    from the pinned list and not from a cursor that fails to decode."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a = await _note(c, h, "a")
        await _note(c, h, "b")
        page = (await c.get("/notes", headers=h, params={"limit": 1})).json()
        cursor = page["next_cursor"]
        assert cursor, page
        # The same cursor is good on the ordinary list.
        r = await c.get("/notes", headers=h, params={"limit": 1, "cursor": cursor})
        assert r.status_code == 200, r.text
        await c.post("/notes/pin", headers=h, json={"note_id": a["id"]})
        r = await c.get("/notes", headers=h, params={"pinned": "true", "cursor": cursor})
        assert r.status_code == 422, r.text
        assert "single page" in r.text


async def test_every_pin_is_read_before_the_page_is_cut() -> None:
    """Three pins, a page of one, and the note pinned last is the OLDEST: it
    can come first only if every pinned row was read and ordered by pin,
    not just the newest-created ``limit + 1``."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        oldest = await _note(c, h, "oldest, pinned last")
        middle = await _note(c, h, "middle")
        newest = await _note(c, h, "newest, pinned first")
        for n in (newest, middle, oldest):
            r = await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
            assert r.status_code == 200, r.text
        r = await c.get("/notes", headers=h, params={"pinned": "true", "limit": 1})
        assert r.status_code == 200, r.text
        assert [n["id"] for n in r.json()["items"]] == [oldest["id"]]
        assert r.json()["truncated"] is True


async def _set_review_state(h: dict[str, str], user_id: str, note_id: str, state: str) -> None:
    from sqlalchemy import update

    from mycelium_core.db import tenant_session
    from mycelium_core.models.note import Note

    async with tenant_session(h["X-Workspace-Id"], user_id) as s:
        await s.execute(
            update(Note).where(Note.id == uuid.UUID(note_id)).values(review_state=state)
        )


async def test_a_proposal_cannot_be_pinned_and_leaves_the_pinned_list() -> None:
    """A note still awaiting review is not openable through the normal read
    path, so it is neither pinnable nor listed among the pins, even when the
    list is asked to include the bin."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = (await c.get("/auth/me", headers=h)).json()
        proposed = await _note(c, h, "a proposal")
        await _set_review_state(h, str(me["user_id"]), proposed["id"], "proposed")
        r = await c.post("/notes/pin", headers=h, json={"note_id": proposed["id"]})
        assert r.status_code == 404, r.text

        later = await _note(c, h, "pinned, then sent back to review")
        await c.post("/notes/pin", headers=h, json={"note_id": later["id"]})
        assert await _pinned(c, h) == [later["id"]]
        await _set_review_state(h, str(me["user_id"]), later["id"], "proposed")
        assert await _pinned(c, h) == []
        r = await c.get("/notes", headers=h, params={"pinned": "true", "include_deleted": "true"})
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []


async def test_a_new_pin_past_the_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap refuses a NEW pin with its own code; re-pinning a pinned note
    stays the idempotent no-op, and unpinning makes room again."""
    from mycelium_core.services import note_pins

    monkeypatch.setattr(note_pins, "MAX_PINS", 2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        notes = [await _note(c, h, x) for x in ("a", "b", "c")]
        a, b, extra = notes
        for n in (a, b):
            r = await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
            assert r.status_code == 200, r.text
        r = await c.post("/notes/pin", headers=h, json={"note_id": extra["id"]})
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "note.pin_limit", r.text
        assert "2 pinned notes" in r.text
        again = await c.post("/notes/pin", headers=h, json={"note_id": a["id"]})
        assert again.status_code == 200, again.text
        assert await _pinned(c, h) == [b["id"], a["id"]]
        await c.post("/notes/unpin", headers=h, json={"note_id": b["id"]})
        r = await c.post("/notes/pin", headers=h, json={"note_id": extra["id"]})
        assert r.status_code == 200, r.text


async def test_recent_by_work_counts_a_part_edit_and_not_a_system_write() -> None:
    """``order_by=touched_at`` is the garden's liveness clock: a person
    editing only inside a part brings the note up, which ``updated_at``
    does not, and a system write to a part does not bring a note up."""
    from mycelium_core.db import tenant_session
    from mycelium_core.services import note_parts

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = (await c.get("/auth/me", headers=h)).json()
        older = await _note(c, h, "older, then worked on")
        newer = await _note(c, h, "newer, then touched by the system")
        ids = [older["id"], newer["id"]]

        async def order(by: str) -> list[str]:
            r = await c.get(
                "/notes",
                headers=h,
                params={"ids": ids, "order_by": by, "order_desc": "true"},
            )
            assert r.status_code == 200, r.text
            return [n["id"] for n in r.json()["items"]]

        assert await order("touched_at") == [newer["id"], older["id"]]

        part = (await c.get(f"/notes/{older['id']}/parts", headers=h)).json()[0]
        r = await c.patch(
            f"/notes/{older['id']}/parts/{part['id']}",
            headers=h,
            json={"expected_version": part["version"], "body": "worked on, inside the part"},
        )
        assert r.status_code == 200, r.text
        assert await order("touched_at") == [older["id"], newer["id"]]
        # The row-write clock did not see that edit: this is why the recent
        # notes do not read it.
        assert await order("updated_at") == [newer["id"], older["id"]]

        npart = (await c.get(f"/notes/{newer['id']}/parts", headers=h)).json()[0]
        async with tenant_session(
            h["X-Workspace-Id"], str(me["user_id"]), actor_kind="system"
        ) as s:
            await note_parts.update_part(
                s,
                org_id=uuid.UUID(h["X-Workspace-Id"]),
                actor_id=uuid.UUID(str(me["user_id"])),
                part_id=uuid.UUID(npart["id"]),
                expected_version=npart["version"],
                body="rewritten by a sweep",
            )
        assert await order("touched_at") == [older["id"], newer["id"]]


async def test_the_mcp_tools_are_the_same_operation() -> None:
    from mycelium_core.db import admin_session
    from mycelium_core.services.auth import signup
    from mycelium_mcp import tools_notes

    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP NOTE PINS",
        )
    token, org = r.token, str(r.org_id)
    assert token is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = {"Authorization": f"Bearer {token}", "X-Workspace-Id": org}
        n = await _note(c, h, "pinned over MCP")

    list_notes = tools_notes.TOOLS["list_notes"]
    pinned = await tools_notes.TOOLS["pin_note"](token=token, org_id=org, note_id=n["id"])
    assert pinned["pinned"] is True
    page = await list_notes(token=token, org_id=org, pinned=True)
    assert [row["id"] for row in page["items"]] == [n["id"]]
    await tools_notes.TOOLS["unpin_note"](token=token, org_id=org, note_id=n["id"])
    page = await list_notes(token=token, org_id=org, pinned=True)
    assert page["items"] == []


async def test_concurrent_new_pins_cannot_pass_the_cap_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six presses at once with room for one pin: exactly one lands.

    The race is made certain rather than likely. Each request's INSERT into
    ``note_pins`` is held until every request is either holding there too or
    waiting on a lock in the database: without the per-person lock all six
    have counted zero pins and all six insert; with it, five are queued
    behind the lock and see the first one's pin when their turn comes."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Insert

    from mycelium_core.services import note_pins

    monkeypatch.setattr(note_pins, "MAX_PINS", 1)
    presses = 6
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        notes = [await _note(c, h, f"n{i}") for i in range(presses)]
        arrived = 0
        execute = AsyncSession.execute

        async def held(self: AsyncSession, stmt: Any, *a: Any, **k: Any) -> Any:
            nonlocal arrived
            if isinstance(stmt, Insert) and stmt.table.name == "note_pins":
                arrived += 1
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    waiting = (
                        await execute(
                            self,
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted"
                            ),
                        )
                    ).scalar_one()
                    if arrived + waiting >= presses:
                        break
                    await asyncio.sleep(0.02)
            return await execute(self, stmt, *a, **k)

        monkeypatch.setattr(AsyncSession, "execute", held)
        answers = await asyncio.gather(
            *[c.post("/notes/pin", headers=h, json={"note_id": n["id"]}) for n in notes]
        )
        monkeypatch.undo()
        codes = sorted(r.status_code for r in answers)
        assert codes == [200] + [409] * (presses - 1), codes
        assert len(await _pinned(c, h)) == 1


async def test_a_pin_on_a_note_in_the_bin_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binned note's pin holds its place under the cap: restoring the note
    brings it back to the pinned list, which must still fit one page."""
    from mycelium_core.services import note_pins

    monkeypatch.setattr(note_pins, "MAX_PINS", 2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        a, b, extra = [await _note(c, h, x) for x in ("a", "b", "c")]
        for n in (a, b):
            await c.post("/notes/pin", headers=h, json={"note_id": n["id"]})
        r = await c.post(f"/notes/{b['id']}/delete", headers=h, json={"expected_version": 1})
        assert r.status_code == 200, r.text
        assert await _pinned(c, h) == [a["id"]]
        r = await c.post("/notes/pin", headers=h, json={"note_id": extra["id"]})
        assert r.status_code == 409, r.text
        r = await c.post(f"/notes/{b['id']}/restore", headers=h, json={"expected_version": 2})
        assert r.status_code == 200, r.text
        assert await _pinned(c, h) == [b["id"], a["id"]]


async def test_the_cap_counts_one_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same person's pins in another workspace take no room here."""
    from mycelium_core.db import admin_session, tenant_session
    from mycelium_core.models.membership import Membership, Role
    from mycelium_core.services import note_pins
    from mycelium_core.services.auth import login, signup

    monkeypatch.setattr(note_pins, "MAX_PINS", 1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        email = f"{uuid.uuid4().hex[:10]}@example.test"
        other_email = f"{uuid.uuid4().hex[:10]}@example.test"
        async with admin_session() as s:
            me = await signup(s, email=email, password="pw-strong-123", org_name="HOME")
            other = await signup(s, email=other_email, password="pw-strong-123", org_name="AWAY")
        async with tenant_session(str(other.org_id), str(other.user_id)) as s:
            s.add(Membership(org_id=other.org_id, user_id=me.user_id, role=Role.member))
            await s.flush()
        async with admin_session() as s:
            mine = await login(s, email=email, password="pw-strong-123")
        home = {"Authorization": f"Bearer {mine.access_token}", "X-Workspace-Id": str(me.org_id)}
        away = {"Authorization": f"Bearer {mine.access_token}", "X-Workspace-Id": str(other.org_id)}
        n_away = await _note(c, away, "away")
        r = await c.post("/notes/pin", headers=away, json={"note_id": n_away["id"]})
        assert r.status_code == 200, r.text
        n_home = await _note(c, home, "home")
        r = await c.post("/notes/pin", headers=home, json={"note_id": n_home["id"]})
        assert r.status_code == 200, r.text


async def _pin_rows(org: str, user: str, note_id: str) -> int:
    from mycelium_core.db import tenant_session
    from mycelium_core.models.note_pin import NotePin

    async with tenant_session(org, user) as s:
        n = await s.scalar(
            select(func.count()).select_from(NotePin).where(NotePin.note_id == uuid.UUID(note_id))
        )
    return int(n or 0)


async def test_a_note_sent_back_to_review_loses_its_pins() -> None:
    """Withdrawing an atom (``restore_source``) sends it back to the review
    gate (demoted, then declined, ADR-0075): it leaves every list, so its
    pins are dropped in the same transaction and stop holding a place under
    the cap. A note merely binned keeps its pin."""
    from mycelium_core.db import tenant_session
    from mycelium_core.models.note import Note, NoteKind
    from mycelium_core.models.note_link import NoteNoteLink
    from mycelium_core.services import garden_review as review
    from mycelium_core.services import note_pins
    from mycelium_core.services import notes as nt

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = str((await c.get("/auth/me", headers=h)).json()["user_id"])
    org, user = uuid.UUID(h["X-Workspace-Id"]), uuid.UUID(me)
    async with tenant_session(str(org), me) as s:
        source = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="src", text="the source"
        )
        await nt.archive_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=source.id,
            archived=True,
            expected_version=source.version,
        )
        atom = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="atom", text="an atom"
        )
        atom.origin_model_id = "model-x"
        atom.review_state = "proposed"
        atom.humus_flag = True
        s.add(
            NoteNoteLink(
                org_id=org, parent_note_id=source.id, child_note_id=atom.id, kind="hypha_of"
            )
        )
        await s.flush()
        await review.approve_node(s, org_id=org, actor_id=user, note_id=atom.id)
        await note_pins.pin(s, org_id=org, user_id=user, note_id=atom.id)
        atom_id = atom.id
    assert await _pin_rows(str(org), me, str(atom_id)) == 1
    async with tenant_session(str(org), me) as s:
        await review.restore_source(s, org_id=org, actor_id=user, atom_note_id=atom_id)
        state = await s.scalar(select(Note.review_state).where(Note.id == atom_id))
        assert state == "declined"
    assert await _pin_rows(str(org), me, str(atom_id)) == 0


async def test_every_reader_of_the_clock_gives_the_same_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page-sized readers of the liveness clock (the newest-first walk,
    in small steps and in one, and the per-note probe and the single pass it
    hands over to) give
    exactly the order of the whole-history aggregate, for every page size,
    ties at one instant included."""
    import datetime as dt

    from mycelium_core.db import tenant_session
    from mycelium_core.models.activity_log import ActivityLog
    from mycelium_core.models.note import Note
    from mycelium_core.services import note_touch
    from mycelium_core.services import notes as notes_svc

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = str((await c.get("/auth/me", headers=h)).json()["user_id"])
        org = h["X-Workspace-Id"]
        notes = [await _note(c, h, f"n{i}") for i in range(7)]
        parts = {
            n["id"]: (await c.get(f"/notes/{n['id']}/parts", headers=h)).json()[0]["id"]
            for n in notes
        }
        # Two notes outside the filter, worked on after all of the others: the
        # walk has to read past their touches, in several steps, to fill a page.
        noise = [await _note(c, h, f"outside {i}") for i in range(2)]
        t0 = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        ids = [uuid.UUID(n["id"]) for n in notes]

        def ev(
            entity: str, eid: str, action: str, secs: int, kind: str = "human_direct"
        ) -> ActivityLog:
            return ActivityLog(
                org_id=uuid.UUID(org),
                entity=entity,
                entity_id=uuid.UUID(eid),
                action=action,
                ts=t0 + dt.timedelta(seconds=secs),
                actor_kind=kind,
            )

        async with tenant_session(org, me) as s:
            s.add_all(
                [
                    ev("note", notes[0]["id"], "update", 5),
                    ev("note_part", parts[notes[1]["id"]], "replace", 5),  # tie with n0
                    ev("note", notes[2]["id"], "update", 9, kind="system"),  # not a touch
                    ev("note", notes[3]["id"], "archive", 9),  # not a touch action
                    ev("note_part", parts[notes[4]["id"]], "create", 2),  # a part added
                    ev("note", notes[5]["id"], "attach_tag", 1),
                    ev("note", notes[5]["id"], "update", 7),
                    *(ev("note", noise[i % 2]["id"], "update", 20 + i) for i in range(6)),
                ]
            )

        async def reference(desc: bool) -> list[uuid.UUID]:
            async with tenant_session(org, me) as s:
                last = note_touch.last_touch_subquery(uuid.UUID(org))
                key = note_touch.touched_at(last)
                rows = await s.execute(
                    select(Note.id)
                    .outerjoin(last, last.c.note_id == Note.id)
                    .where(Note.id.in_(ids))
                    .order_by(
                        (key.desc() if desc else key.asc()).nulls_last(),
                        Note.created_at.desc(),
                        Note.id.asc(),
                    )
                )
                return list(rows.scalars())

        async def page(limit: int, desc: bool) -> list[uuid.UUID]:
            r = await c.get(
                "/notes",
                headers=h,
                params={
                    "ids": [str(i) for i in ids],
                    "order_by": "touched_at",
                    "order_desc": str(desc).lower(),
                    "limit": limit,
                },
            )
            assert r.status_code == 200, r.text
            return [uuid.UUID(n["id"]) for n in r.json()["items"]]

        want_desc, want_asc = await reference(True), await reference(False)
        # The clock by hand: n5 touched last (7s, its 1s touch superseded);
        # n1 and n0 tie at 5s, a part replace and a note update, newer
        # creation first; n4 by a part added at 2s; then the untouched in
        # creation order, n2's system write and n3's archive not counting.
        n = ids
        assert want_desc == [n[5], n[1], n[0], n[4], n[6], n[3], n[2]]
        for setting in (
            {},
            {"_TOUCH_WALK_BATCH": 1},
            {"_TOUCH_WALK_BATCH": 1, "_TOUCH_WALK_BUDGET": 1},
            {"_TOUCH_WALK_BATCH": 1, "_TOUCH_WALK_BUDGET": 1, "_PER_NOTE_MAX": 0},
        ):
            for name, value in setting.items():
                monkeypatch.setattr(notes_svc, name, value)
            for limit in range(1, len(ids) + 1):
                assert await page(limit, True) == want_desc[:limit], (setting, limit)
                assert await page(limit, False) == want_asc[:limit], (setting, limit)
            monkeypatch.undo()


async def test_the_touch_walk_misses_no_touch_that_shares_an_instant() -> None:
    """Paged backwards, the walk returns every touch once: a page cut in the
    middle of one instant brings the rest of that instant with it, and the
    next page starts strictly before it."""
    import datetime as dt

    from mycelium_core.db import tenant_session
    from mycelium_core.models.activity_log import ActivityLog
    from mycelium_core.services import note_touch

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _owner(c)
        me = str((await c.get("/auth/me", headers=h)).json()["user_id"])
        org = h["X-Workspace-Id"]
        notes = [await _note(c, h, f"n{i}") for i in range(4)]
    t0 = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    at = [t0 + dt.timedelta(seconds=3), t0 + dt.timedelta(seconds=2)]
    planted = [(at[0], notes[0]["id"])] + [(at[1], n["id"]) for n in notes[1:]]
    async with tenant_session(org, me) as s:
        s.add_all(
            ActivityLog(
                org_id=uuid.UUID(org),
                entity="note",
                entity_id=uuid.UUID(nid),
                action="update",
                ts=ts,
                actor_kind="human_direct",
            )
            for ts, nid in planted
        )
    async with tenant_session(org, me) as s:
        first = await note_touch.touches_newest_first(s, uuid.UUID(org), before=None, limit=2)
        # Cut after one of the three touches at ``at[1]``: all three come.
        assert [ts for ts, _ in first] == [at[0], at[1], at[1], at[1]]
        assert {nid for _, nid in first} == {uuid.UUID(n["id"]) for n in notes}
        rest = await note_touch.touches_newest_first(s, uuid.UUID(org), before=at[1], limit=50)
        assert all(ts < at[1] for ts, _ in rest)
