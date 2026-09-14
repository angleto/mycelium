"""``body_sha256`` on the note-part projections, and the identity that
makes it worth anything.

A conditional write to a part (``PATCH .../body/patch``) is gated on the
digest of the body the client diffed against. Until now nothing served
that digest next to the part: a caller had to hash the body itself,
which is where the two sides stop agreeing -- one trailing newline, one
normalisation pass, and a correct client is indistinguishable from a
drifted one. Worse for the body-FREE outline, whose whole purpose is
telling what changed without fetching anything: a six-part note cost an
outline plus six body reads to learn that nothing had moved.

The test that matters is the last one. Publishing *a* digest is easy and
useless; publishing the one ``text_patch.assert_base`` accepts is the
requirement, and the near miss is real and named in the task:
``note_search.content_hash`` hashes a different preimage (title, a blank
line, then the stripped body), so shipping it here would answer
"unchanged" and "drifted" wrongly on exactly the racing path this field
exists to serve.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.db import admin_session
from mycelium_core.services import note_search, text_patch
from mycelium_core.services.auth import signup
from mycelium_mcp.server import create_note as mcp_create_note
from mycelium_mcp.server import get_note as mcp_get_note
from mycelium_mcp.server import list_note_parts as mcp_list_note_parts

# Non-ASCII plus a trailing newline: the two things a client-side hash
# gets wrong independently of each other.
_BODY = "# Titolo\n\nUn corpo con àccenti e una riga finale.\n"


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _tenant() -> tuple[str, str]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="Digest")
    assert r.token is not None
    return r.token, str(r.org_id)


async def _rest(c: AsyncClient) -> tuple[dict[str, str], str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    h = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    nid = (await c.post("/notes", headers=h, json={"kind": "text", "title": "d"})).json()["id"]
    return h, str(nid)


async def test_the_outline_carries_the_digest_without_the_body() -> None:
    """``list_note_parts``: the body-free projection answers "did this
    change" on its own."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text=_BODY)
    parts = await mcp_list_note_parts(token=token, org_id=org, note_id=note["id"])

    assert parts, parts
    p = parts[0]
    assert "body" not in p, "the outline must stay body-free"
    assert p["body_sha256"] == text_patch.body_sha256(_BODY)


async def test_the_full_projection_carries_the_same_digest() -> None:
    """Both projections or neither: a field on the cheap one and absent
    from the full one is an asymmetry a caller discovers by failing."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text=_BODY)

    with_bodies = await mcp_get_note(
        token=token, org_id=org, note_id=note["id"], include_part_bodies=True
    )
    outline = await mcp_get_note(
        token=token, org_id=org, note_id=note["id"], include_part_bodies=False
    )
    full_part = with_bodies["parts"][0]
    thin_part = outline["parts"][0]

    assert full_part["body"] == _BODY
    assert full_part["body_sha256"] == thin_part["body_sha256"]
    assert thin_part["body_sha256"] == text_patch.body_sha256(_BODY)


async def test_the_rest_projection_carries_it_too() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, nid = await _rest(c)
        await c.post(f"/notes/{nid}/parts", headers=h, json={"body": _BODY})
        listed = (await c.get(f"/notes/{nid}/parts", headers=h)).json()
        assert listed[0]["body_sha256"] == text_patch.body_sha256(_BODY)


async def test_the_digest_moves_when_the_body_does() -> None:
    """Otherwise a constant would pass every test above."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text=_BODY)
    before = (await mcp_list_note_parts(token=token, org_id=org, note_id=note["id"]))[0]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        # Same workspace, over REST, so the two surfaces are shown to
        # agree about one row rather than each about its own.
        h = {"Authorization": f"Bearer {token}", "X-Workspace-Id": org}
        r = await c.patch(
            f"/notes/{note['id']}/parts/{before['id']}",
            headers=h,
            json={"expected_version": before["version"], "body": _BODY + "coda\n"},
        )
        assert r.status_code == 200, r.text

    after = (await mcp_list_note_parts(token=token, org_id=org, note_id=note["id"]))[0]
    assert after["body_sha256"] != before["body_sha256"]
    assert after["body_sha256"] == text_patch.body_sha256(_BODY + "coda\n")


async def test_the_published_digest_is_the_one_the_gate_accepts() -> None:
    """The requirement, and the reason the near miss is worth naming.

    ``assert_base`` is the server-side half of the conditional-write
    gate. What we publish must be exactly what it accepts -- not merely
    a stable hash of something -- or a client doing precisely what the
    field invites it to do gets PATCH_STALE on a body that never moved.

    ``note_search.content_hash`` is the other digest in the tree and it
    is the wrong one: same algorithm, different preimage. Asserted here
    so that swapping one for the other is caught by a test rather than
    by a failed write."""
    token, org = await _tenant()
    note = await mcp_create_note(token=token, org_id=org, kind="text", text=_BODY)
    part = (await mcp_list_note_parts(token=token, org_id=org, note_id=note["id"]))[0]
    published = part["body_sha256"]

    # The gate accepts it: no raise is the assertion.
    text_patch.assert_base(_BODY, expected_sha256=published)

    # And it is NOT the indexing hash, which would look interchangeable:
    # same algorithm, different preimage. ``render_part_for_search``
    # strips the body and prepends the title, so its digest of THIS body
    # is a different string -- which is what would silently turn every
    # conditional write into a false PATCH_STALE.
    indexing = note_search.content_hash(_BODY.strip())
    assert published != indexing
    # Narrowly: the difference is the stripping, so the two agree only
    # on a body that needs none. Stated so the reason is asserted and
    # not merely believed.
    assert text_patch.body_sha256(_BODY.strip()) == indexing
