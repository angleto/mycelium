"""A part's title is writable from the agent surfaces, not only from
the SPA.

``note_parts.update_part`` has accepted ``title`` since it was written,
and ``PATCH /notes/{id}/parts/{pid}`` passes it through. The two MCP
tools did not: ``add_note_part`` and ``update_note_part`` took body and
lang and nothing else. So a title written by an agent was write-once
from that agent's side -- a typo in the outline of a long note stayed
there, and the only way to correct it was a surface the agent does not
have.

The second test is the one worth keeping: a title set at creation can be
CORRECTED afterwards. Creation alone would pass with an update path that
silently drops the field.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session
from mycelium_core.services.auth import signup
from mycelium_mcp.server import add_note_part, list_note_parts, update_note_part
from mycelium_mcp.server import create_note as mcp_create_note


async def _tenant() -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PartTitle",
        )
    assert r.token is not None
    return r.token, str(r.org_id)


async def test_an_agent_can_name_a_block_when_it_writes_it() -> None:
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text="seed")
    part = await add_note_part(
        token=token,
        org_id=org,
        note_id=note["id"],
        body="## Misure\n\nquattro righe\n",
        title="Misure",
    )
    assert part["title"] == "Misure"

    # And the outline shows it, which is the point: naming a block is
    # how a caller picks one to read without pulling every body.
    outline = await list_note_parts(token=token, org_id=org, note_id=note["id"])
    assert [p["title"] for p in outline if p["id"] == part["id"]] == ["Misure"]


async def test_an_agent_can_correct_a_title_it_got_wrong() -> None:
    """The half that a create-only fix leaves broken: without this, a
    typo in an outline written by an agent is permanent from every
    surface that agent can reach."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text="seed")
    part = await add_note_part(
        token=token, org_id=org, note_id=note["id"], body="corpo\n", title="Misrue"
    )

    res = await update_note_part(
        token=token,
        org_id=org,
        part_id=part["id"],
        expected_version=part["version"],
        title="Misure",
    )
    assert res["version"] == part["version"] + 1

    outline = await list_note_parts(token=token, org_id=org, note_id=note["id"])
    row = next(p for p in outline if p["id"] == part["id"])
    assert row["title"] == "Misure"


async def test_renaming_a_block_leaves_its_body_alone() -> None:
    """``update_note_part`` takes body and title independently; a rename
    that passes no body must not blank the one that is there. ``body`` is
    ``None`` on that call, and ``None`` means "untouched" for it -- the
    same word that means "untouched" for title and lang."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text="seed")
    body = "una riga che deve sopravvivere\n"
    part = await add_note_part(token=token, org_id=org, note_id=note["id"], body=body)

    await update_note_part(
        token=token,
        org_id=org,
        part_id=part["id"],
        expected_version=part["version"],
        title="Solo il titolo",
    )

    outline = await list_note_parts(token=token, org_id=org, note_id=note["id"])
    row = next(p for p in outline if p["id"] == part["id"])
    assert row["title"] == "Solo il titolo"
    # The outline is body-free, so the digest is what says the body did
    # not move. Cheaper than fetching it, and the reason it is published.
    assert row["bytes"] == len(body.encode("utf-8"))
