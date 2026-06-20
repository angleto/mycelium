"""Memory channels: a controlled, seeded vocabulary keyed by a stable
``system_key`` (docs/adr/0005, FR-8).

Why this exists: integrations (email ingest, Telegram) need a
DETERMINISTIC, well-known channel to write into. Arbitrary user-named
``memory_channel`` tags created via the generic ``POST /tags`` gave an
integration no stable target. So channels stay ``memory_channel`` tags
(RLS-scoped) but become seeded + keyed, and management is reserved to
the PLATFORM ADMIN (global ``is_admin`` + active ``X-Admin-Mode``), not
the workspace owner.

Covered:
- bootstrap seeds exactly the 4 canonical channels; re-seeding is
  idempotent and never raises ``tag.duplicate``.
- generic ``POST /tags`` with kind=memory_channel -> 400
  ``channel.not_tag_creatable``.
- ``GET /memory/channels`` lists only the CONFIGURED channels
  (manual/agent/note, each with a description) for a plain member;
  email/telegram are seeded in the DB (deterministic future ingest
  target) but filtered out of the list/select surface until their
  integration ships.
- platform admin (admin-mode) lifecycle: create custom, rename,
  disable, delete; seeded rename OK; seeded key change ->
  ``channel.key_immutable``; seeded delete ->
  ``channel.seeded_undeletable``; seeded disable OK.
- a workspace OWNER who is NOT platform admin -> 403
  ``channel.admin_only`` on POST/PATCH/DELETE.
- memory write/search by ``channel_key``; unknown/disabled key ->
  ``channel.not_found``; ``channel_tag_id`` + mismatching
  ``channel_key`` -> domain error.
- cross-org isolation: ``channel_key`` resolution is RLS-scoped (org B
  cannot resolve org A's channel; a foreign ``channel_tag_id`` is still
  rejected as before).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

from flow_api.main import app
from flow_core.bootstrap_admin import ensure_admin
from flow_core.embedder import set_embedder_override

# Seeded in the DB (determinism for a future ingest): the original
# five integrations plus ``task`` (channel for task-search blobs).
_SEEDED = {"email", "telegram", "manual", "agent", "note", "task"}
# Exposed in the list/select surface. ``email`` is now wired (task
# 2a901dee: per-account ingest into this channel); ``telegram`` stays
# filtered out until its ingestion exists.
_CONFIGURED = {"manual", "agent", "note", "task", "email"}
_ADMIN_PW = "Str0ng-Passw0rd!"
_ELEVATE = {"X-Admin-Mode": "1"}


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _admin_session(c: AsyncClient) -> tuple[dict[str, str], str]:
    """A platform-admin account (capability via bootstrap) logged in;
    returns its base headers (no elevation) and its workspace id."""
    email = _email()
    await ensure_admin(email, _ADMIN_PW)
    login = (await c.post("/auth/login", json={"email": email, "password": _ADMIN_PW})).json()
    me = (await c.get("/auth/me", headers={"Authorization": f"Bearer {login['token']}"})).json()
    # The admin owns a personal workspace from the bootstrap signup.
    orgs = (
        await c.get("/workspaces", headers={"Authorization": f"Bearer {login['token']}"})
    ).json()
    ws = orgs[0]["id"] if orgs else me.get("workspace_id")
    h = {"Authorization": f"Bearer {login['token']}", "X-Workspace-Id": str(ws)}
    return h, str(ws)


async def test_bootstrap_lists_only_configured_idempotent() -> None:
    """First touch seeds the canonical channels but the LIST surface
    exposes only the configured ones (manual/agent/note/task/email, each
    with a description); telegram is seeded yet filtered out until its
    ingest exists. Listing again (which re-runs the idempotent seed)
    does not duplicate and never raises tag.duplicate, even after a
    plain POST /notes which exercises the known ensure_default_client
    quirk path."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)

        first = await c.get("/memory/channels", headers=h)
        assert first.status_code == 200, first.text
        body = first.json()
        assert {ch["system_key"] for ch in body} == _CONFIGURED, body
        assert "email" in {ch["system_key"] for ch in body}
        assert "telegram" not in {ch["system_key"] for ch in body}
        assert all(ch["enabled"] for ch in body)
        assert all(ch["seeded"] for ch in body)
        # Each configured channel carries a short English description.
        by_key = {ch["system_key"]: ch for ch in body}
        assert by_key["manual"]["description"] == "Written by you in the app"
        assert by_key["agent"]["description"] == "Written by the assistant"
        assert by_key["note"]["description"] == "Captured from your notes"
        ids = {ch["id"] for ch in body}

        # A plain note exercises the ensure_default_client path (the
        # documented quirk). It must NOT collide with channel seeding.
        note = await c.post("/notes", headers=h, json={"kind": "text", "text": "hi"})
        assert note.status_code == 200, note.text

        # Re-list: idempotent seed, still exactly the same configured set
        # (no dupes, no tag.duplicate).
        again = await c.get("/memory/channels", headers=h)
        assert again.status_code == 200, again.text
        assert {ch["id"] for ch in again.json()} == ids
        assert {ch["system_key"] for ch in again.json()} == _CONFIGURED


async def test_generic_tag_endpoint_rejects_memory_channel() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.post("/tags", headers=h, json={"kind": "memory_channel", "name": "anything"})
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "channel.not_tag_creatable"

        # A non-channel tag kind is still creatable (unchanged).
        ok = await c.post("/tags", headers=h, json={"kind": "generic", "name": "g1"})
        assert ok.status_code == 200, ok.text


async def test_member_can_list_channels() -> None:
    """Any authenticated member may list (the memory UI needs it)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/memory/channels", headers=h)
        assert r.status_code == 200, r.text
        keys = {ch["system_key"] for ch in r.json()}
        assert keys == _CONFIGURED


async def test_platform_admin_channel_lifecycle() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws = await _admin_session(c)
        el = {**h, **_ELEVATE}

        # Seed first (any member surface). The seeded-channel
        # protections apply to ANY canonical key; exercise them on
        # ``note``, which is a CONFIGURED seeded channel (so it is
        # listed) -- email is seeded but filtered out of the list now.
        seeded = (await c.get("/memory/channels", headers=h)).json()
        note_ch = next(ch for ch in seeded if ch["system_key"] == "note")
        assert note_ch["seeded"] is True

        # Create a custom channel (keyed).
        created = await c.post(
            "/memory/channels",
            headers=el,
            json={"name": "Slack import", "system_key": "slack"},
        )
        assert created.status_code == 200, created.text
        cid = created.json()["id"]
        assert created.json()["seeded"] is False
        assert created.json()["enabled"] is True

        # Rename it.
        renamed = await c.patch(f"/memory/channels/{cid}", headers=el, json={"name": "Slack"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Slack"

        # Disable it.
        disabled = await c.patch(f"/memory/channels/{cid}", headers=el, json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enabled"] is False

        # Delete it (custom => deletable).
        deleted = await c.delete(f"/memory/channels/{cid}", headers=el)
        assert deleted.status_code == 204, deleted.text

        # Seeded channel: rename OK (key preserved).
        sr = await c.patch(
            f"/memory/channels/{note_ch['id']}",
            headers=el,
            json={"name": "Captured"},
        )
        assert sr.status_code == 200, sr.text
        assert sr.json()["name"] == "Captured"
        assert sr.json()["system_key"] == "note"

        # Seeded channel: changing system_key is rejected.
        sk = await c.patch(
            f"/memory/channels/{note_ch['id']}",
            headers=el,
            json={"system_key": "note-2"},
        )
        assert sk.status_code == 400, sk.text
        assert sk.json()["code"] == "channel.key_immutable"

        # Seeded channel: delete is rejected.
        sd = await c.delete(f"/memory/channels/{note_ch['id']}", headers=el)
        assert sd.status_code == 400, sd.text
        assert sd.json()["code"] == "channel.seeded_undeletable"

        # Seeded channel: disable IS allowed (still listed -- the list
        # filters by key, not by status -- just enabled=false).
        sdis = await c.patch(
            f"/memory/channels/{note_ch['id']}",
            headers=el,
            json={"enabled": False},
        )
        assert sdis.status_code == 200, sdis.text
        assert sdis.json()["enabled"] is False
        relisted = (await c.get("/memory/channels", headers=h)).json()
        note_relisted = next(ch for ch in relisted if ch["system_key"] == "note")
        assert note_relisted["enabled"] is False


async def test_custom_channel_is_listed_telegram_excluded() -> None:
    """Item 5 contract: GET /memory/channels returns manual/agent/note/
    task/email (each with a description) and NOT telegram (seeded but not
    yet implemented); a platform-admin-created CUSTOM channel IS listed
    (deliberately created => configured)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _ws = await _admin_session(c)
        el = {**h, **_ELEVATE}

        base = (await c.get("/memory/channels", headers=h)).json()
        assert {ch["system_key"] for ch in base} == _CONFIGURED
        for ch in base:
            assert ch["description"] is not None  # every seeded one has copy

        created = await c.post(
            "/memory/channels",
            headers=el,
            json={"name": "Slack import", "system_key": "slack"},
        )
        assert created.status_code == 200, created.text
        # A custom channel has no canned description.
        assert created.json()["description"] is None

        after = (await c.get("/memory/channels", headers=h)).json()
        keys = {ch["system_key"] for ch in after}
        assert "slack" in keys  # custom => configured => listed
        assert keys == _CONFIGURED | {"slack"}
        assert "email" in keys and "telegram" not in keys


async def test_non_admin_owner_forbidden_on_management() -> None:
    """A workspace OWNER who is not a platform admin gets 403
    channel.admin_only on create/patch/delete, even acting as owner and
    even forging the elevation header (no capability => no escalation)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)  # owner of their own workspace, NOT admin
        owner = {**h, "X-Workspace-Role": "owner"}
        owner_el = {**owner, **_ELEVATE}  # forged elevation, no capability

        seeded = (await c.get("/memory/channels", headers=h)).json()
        some_id = seeded[0]["id"]

        for hdr in (owner, owner_el):
            cr = await c.post(
                "/memory/channels", headers=hdr, json={"name": "x", "system_key": "x"}
            )
            assert cr.status_code == 403, cr.text
            assert cr.json()["code"] == "channel.admin_only"

            pa = await c.patch(f"/memory/channels/{some_id}", headers=hdr, json={"name": "y"})
            assert pa.status_code == 403, pa.text
            assert pa.json()["code"] == "channel.admin_only"

            de = await c.delete(f"/memory/channels/{some_id}", headers=hdr)
            assert de.status_code == 403, de.text
            assert de.json()["code"] == "channel.admin_only"

        # The non-admin owner can still LIST (member-level).
        assert (await c.get("/memory/channels", headers=h)).status_code == 200


async def test_memory_write_search_by_channel_key(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await c.post("/billing/grant", headers=h, json={"amount": "100"})
        await c.post(
            "/billing/rate-cards",
            headers=h,
            json={
                "model_id": FakeEmbedder.model_id,
                "provider": "local",
                "credits_per_input": "0.001",
            },
        )
        # ``email`` is now a wired, listed channel (task 2a901dee);
        # ``telegram`` is still seeded-but-hidden yet MUST stay resolvable
        # by its stable key (the determinism a future ingest needs) -- the
        # mismatch case below exercises that.
        channels = (await c.get("/memory/channels", headers=h)).json()
        keys = {ch["system_key"] for ch in channels}
        assert "email" in keys and "telegram" not in keys

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "invoice arrived in the inbox",
                "operation_id": "w-key-1",
                "channel_key": "email",
            },
        )
        assert w.status_code == 200, w.text
        blob_id = w.json()["id"]
        # Writing by ``channel_key="email"`` resolves to the seeded email
        # channel tag and attaches it. Recover its tag id from the blob's
        # memory_channel tag.
        chan_tags = [t for t in w.json()["tags"] if t["kind"] == "memory_channel"]
        assert len(chan_tags) == 1
        email_tag_id = chan_tags[0]["id"]

        # Search by the same key returns it.
        same = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "invoice",
                "operation_id": "q-key-1",
                "channel_key": "email",
            },
        )
        assert same.status_code == 200, same.text
        assert blob_id in {x["blob"]["id"] for x in same.json()}

        # Search by a different (valid, seeded) key does not.
        other = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "invoice",
                "operation_id": "q-key-2",
                "channel_key": "telegram",
            },
        )
        assert other.status_code == 200, other.text
        assert other.json() == []

        # Unknown key -> channel.not_found.
        unk = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "x",
                "operation_id": "w-key-unk",
                "channel_key": "does-not-exist",
            },
        )
        assert unk.status_code == 404, unk.text
        assert unk.json()["code"] == "channel.not_found"

        # channel_tag_id + matching channel_key -> OK (same tag).
        ok = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "matching pair",
                "operation_id": "w-key-match",
                "channel_tag_id": email_tag_id,
                "channel_key": "email",
            },
        )
        assert ok.status_code == 200, ok.text

        # channel_tag_id + MISMATCHING channel_key -> domain error.
        # ``telegram`` is also filtered from the list; recover its tag id
        # the same way (write by its key, read the memory_channel tag).
        tgw = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "telegram ping",
                "operation_id": "w-key-tg",
                "channel_key": "telegram",
            },
        )
        assert tgw.status_code == 200, tgw.text
        tg_chan = [t for t in tgw.json()["tags"] if t["kind"] == "memory_channel"]
        assert len(tg_chan) == 1
        tg_tag_id = tg_chan[0]["id"]
        mism = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "mismatch",
                "operation_id": "w-key-mismatch",
                "channel_tag_id": tg_tag_id,
                "channel_key": "email",
            },
        )
        assert mism.status_code == 400, mism.text
        assert mism.json()["code"] == "domain.error"


async def test_disabled_channel_key_not_resolvable(_fake_embedder: None) -> None:
    """A disabled channel is treated as absent for write/search by key
    (channel.not_found)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        admin_h, _ws = await _admin_session(c)
        el = {**admin_h, **_ELEVATE}
        chans = (await c.get("/memory/channels", headers=admin_h)).json()
        manual = next(ch for ch in chans if ch["system_key"] == "manual")

        # Disable the seeded "manual" channel.
        dis = await c.patch(
            f"/memory/channels/{manual['id']}",
            headers=el,
            json={"enabled": False},
        )
        assert dis.status_code == 200, dis.text

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=admin_h,
            json={
                "project_id": proj,
                "text": "should not file",
                "operation_id": "w-disabled",
                "channel_key": "manual",
            },
        )
        assert w.status_code == 404, w.text
        assert w.json()["code"] == "channel.not_found"


async def test_channel_key_resolution_is_rls_scoped(_fake_embedder: None) -> None:
    """Org B cannot resolve org A's channel by key, and a foreign
    channel_tag_id is still rejected (RLS isolation preserved). Each
    org's seeded keys live in its own tenant; the keys are the same
    slugs but resolve to DIFFERENT tag rows, and a tag id from org A is
    invisible/invalid in org B."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        ha = await _signup(c)
        hb = await _signup(c)
        proj = str(uuid.uuid4())

        # ``email`` is filtered from the list but still resolvable by
        # key per tenant. Materialise each org's seeded email channel by
        # writing with channel_key="email" and read back its
        # memory_channel tag id.
        wa = await c.post(
            "/memory/blobs",
            headers=ha,
            json={
                "project_id": proj,
                "text": "a-side memo",
                "operation_id": "w-rls-a",
                "channel_key": "email",
            },
        )
        assert wa.status_code == 200, wa.text
        a_email = next(t["id"] for t in wa.json()["tags"] if t["kind"] == "memory_channel")

        # Org B uses its OWN "email" key: resolves to B's tag, fine.
        w = await c.post(
            "/memory/blobs",
            headers=hb,
            json={
                "project_id": proj,
                "text": "b-side memo",
                "operation_id": "w-rls-b",
                "channel_key": "email",
            },
        )
        assert w.status_code == 200, w.text
        b_email = next(t["id"] for t in w.json()["tags"] if t["kind"] == "memory_channel")
        # Same slug, different tenants => different tag rows.
        assert a_email != b_email
        assert b_email in {t["id"] for t in w.json()["tags"]}
        assert a_email not in {t["id"] for t in w.json()["tags"]}

        # Org B cannot target org A's channel by its tag id (RLS makes
        # it invisible -> tag.not_found, behaviour preserved).
        foreign = await c.post(
            "/memory/blobs",
            headers=hb,
            json={
                "project_id": proj,
                "text": "x",
                "operation_id": "w-rls-foreign",
                "channel_tag_id": a_email,
            },
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["code"] == "tag.not_found"
