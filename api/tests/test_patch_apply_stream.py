"""E2E: the token-free block surface -- raw download, unified-diff patch,
and capability-token attachment upload -- over real HTTP.

Mirrors ``test_capability_token_stream.py`` (ASGITransport + signup). The
patch flow is the keystone: GET ``.../body/raw`` to capture ``X-Version`` +
``X-Body-SHA256``, build a unified diff locally with ``difflib``, POST it to
``.../body/patch`` with a single-use ``mycelium_cap_`` token, and assert the
base gate (409 on drift) and strict apply (422 on a non-applying diff) hold
with no mutation on failure.
"""

from __future__ import annotations

import difflib
import hashlib
import uuid

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.services import capability_tokens as svc


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _udiff(a: str, b: str) -> str:
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True), lineterm="\n"
        )
    )


async def _signup(c: AsyncClient) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    headers = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return headers, uuid.UUID(a["workspace_id"]), uuid.UUID(a["user_id"])


async def _note_with_part(c: AsyncClient, h: dict[str, str], body: str) -> tuple[str, str, int]:
    note = (await c.post("/notes", headers=h, json={"kind": "text", "text": body})).json()
    full = (await c.get(f"/notes/{note['id']}", headers=h)).json()
    p0 = full["parts"][0]
    return note["id"], p0["id"], p0["version"]


async def _mint(
    org: uuid.UUID, user: uuid.UUID, *, action: str, resource_kind: str, resource_id: uuid.UUID
) -> str:
    async with tenant_session(str(org), str(user)) as s:
        res = await svc.mint(
            s,
            org_id=org,
            actor_id=user,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
    return res.raw


# --- note part body: raw + patch round-trip -----------------------------


async def test_part_body_raw_headers_and_patch_roundtrip() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "line one\nline two\nline three\n"
        nid, pid, _v = await _note_with_part(c, h, base)

        # Raw download (bearer): body + base-gate headers.
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        assert raw.status_code == 200, raw.text
        assert raw.text == base
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        assert digest == hashlib.sha256(raw.content).hexdigest()

        # Edit locally, build a unified diff, apply via a capability token.
        new = "line one\nline TWO edited\nline three\nline four\n"
        patch = _udiff(base, new)
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},  # no X-Workspace-Id
            params={"expected_version": version, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == version + 1

        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert next(p for p in full["parts"] if p["id"] == pid)["body"] == new


async def test_patch_version_drift_409() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\nc\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        patch = _udiff(base, "a\nB\nc\n")
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version + 99, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert r.status_code == 409, r.text
        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert next(p for p in full["parts"] if p["id"] == pid)["body"] == base


async def test_patch_sha256_drift_409_patch_stale() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\nc\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        patch = _udiff(base, "a\nB\nc\n")
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version, "base_sha256": "0" * 64},
            content=patch.encode("utf-8"),
        )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "patch.stale"


async def test_patch_non_applying_422() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\nc\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        # Tamper a context line so a hunk no longer matches the live body.
        patch = _udiff(base, "a\nB\nc\n").replace(" a\n", " X\n", 1)
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "patch.does_not_apply"
        full = (await c.get(f"/notes/{nid}", headers=h)).json()
        assert next(p for p in full["parts"] if p["id"] == pid)["body"] == base


async def test_patch_malformed_422() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version, "base_sha256": digest},
            content=b"this is not a unified diff",
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "patch.malformed"


async def test_patch_capability_single_use_and_409_does_not_burn() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\nc\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        patch = _udiff(base, "a\nB\nc\n")
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.UUID(pid),
        )
        cap_h = {"Authorization": f"Bearer {cap}"}

        # A 409 (stale version) must NOT burn the single-use token.
        bad = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers=cap_h,
            params={"expected_version": version + 99, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert bad.status_code == 409, bad.text

        # The same token now applies cleanly...
        ok = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers=cap_h,
            params={"expected_version": version, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert ok.status_code == 200, ok.text

        # ...and is then consumed: a replay is rejected.
        replay = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers=cap_h,
            params={"expected_version": version + 1, "base_sha256": digest},
            content=patch.encode("utf-8"),
        )
        assert replay.status_code == 401, replay.text


async def test_patch_capability_wrong_resource_403() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        base = "a\nb\n"
        nid, pid, _v = await _note_with_part(c, h, base)
        raw = await c.get(f"/notes/{nid}/parts/{pid}/body/raw", headers=h)
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        # Token scoped to a DIFFERENT part id.
        cap = await _mint(
            org,
            user,
            action=svc.ACTION_NOTE_PART_BODY_PATCH,
            resource_kind=svc.RESOURCE_NOTE_PART,
            resource_id=uuid.uuid4(),
        )
        r = await c.post(
            f"/notes/{nid}/parts/{pid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version, "base_sha256": digest},
            content=_udiff(base, "a\nB\n").encode("utf-8"),
        )
        assert r.status_code == 403, r.text


# --- attachment upload via capability (symmetric to download) ------------


async def test_attachment_upload_capability_roundtrip_pg() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _org, _user = await _signup(c)
        task = (await c.post("/tasks", headers=h, json={"title": "files"})).json()
        tid = task["id"]
        payload = b"col1,col2\n1,2\n3,4\n"

        # Mint a single-use write capability over HTTP (no listing).
        mint = await c.post(
            "/attachments/capability/write",
            headers=h,
            json={"parent_kind": "task", "parent_id": tid},
        )
        assert mint.status_code == 201, mint.text
        wtok = mint.json()["token"]

        # Upload with the capability token, no X-Workspace-Id.
        up = await c.post(
            f"/tasks/{tid}/attachments",
            headers={"Authorization": f"Bearer {wtok}"},
            files={"file": ("data.csv", payload, "text/csv")},
        )
        assert up.status_code == 200, up.text
        att_id = up.json()["id"]

        # It is present, and downloads byte-identical via a read capability.
        rtok = (
            await c.post(
                "/attachments/capability",
                headers=h,
                json={"parent_kind": "task", "parent_id": tid},
            )
        ).json()["token"]
        dl = await c.get(
            f"/attachments/{att_id}/download", headers={"Authorization": f"Bearer {rtok}"}
        )
        assert dl.status_code == 200, dl.text
        assert dl.content == payload

        # Single-use: the write token is burned, a second upload is rejected.
        again = await c.post(
            f"/tasks/{tid}/attachments",
            headers={"Authorization": f"Bearer {wtok}"},
            files={"file": ("data2.csv", payload, "text/csv")},
        )
        assert again.status_code == 401, again.text


# --- task description: HTTP mint + raw + patch ---------------------------


async def test_task_description_raw_and_patch_via_http_mint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, _org, _user = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "desc"})).json()["id"]

        # Seed the description via the write-stream (bearer).
        r0 = await c.get(f"/tasks/{tid}/description/raw", headers=h)
        assert r0.status_code == 200, r0.text
        assert r0.text == ""
        v0 = int(r0.headers["X-Version"])
        base = "intro\ndetails\n"
        w = await c.put(
            f"/tasks/{tid}/description/stream",
            headers=h,
            params={"expected_version": v0},
            content=base.encode("utf-8"),
        )
        assert w.status_code == 200, w.text

        # Raw + patch via an HTTP-minted capability token.
        raw = await c.get(f"/tasks/{tid}/description/raw", headers=h)
        assert raw.text == base
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]
        ptok = (
            await c.post(
                "/capability/text-block",
                headers=h,
                json={"kind": "task_description", "resource_id": tid, "verb": "patch"},
            )
        ).json()["token"]
        new = "intro edited\ndetails\nmore\n"
        r = await c.post(
            f"/tasks/{tid}/description/patch",
            headers={"Authorization": f"Bearer {ptok}"},
            params={"expected_version": version, "base_sha256": digest},
            content=_udiff(base, new).encode("utf-8"),
        )
        assert r.status_code == 200, r.text
        assert (await c.get(f"/tasks/{tid}/description/raw", headers=h)).text == new


# --- comment (annotation) body: raw + patch ------------------------------


async def test_annotation_body_raw_and_patch() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h, org, user = await _signup(c)
        tid = (await c.post("/tasks", headers=h, json={"title": "review"})).json()["id"]
        base = "first observation\nsecond observation\n"
        ann = (
            await c.post(
                "/annotations/comment",
                headers=h,
                json={"doc_kind": "task_description", "doc_id": tid, "body": base},
            )
        ).json()
        aid = ann["id"]

        raw = await c.get(f"/annotations/{aid}/body/raw", headers=h)
        assert raw.status_code == 200, raw.text
        assert raw.text == base
        version = int(raw.headers["X-Version"])
        digest = raw.headers["X-Body-SHA256"]

        cap = await _mint(
            org,
            user,
            action=svc.ACTION_ANNOTATION_BODY_PATCH,
            resource_kind=svc.RESOURCE_ANNOTATION,
            resource_id=uuid.UUID(aid),
        )
        new = "first observation edited\nsecond observation\nthird\n"
        r = await c.post(
            f"/annotations/{aid}/body/patch",
            headers={"Authorization": f"Bearer {cap}"},
            params={"expected_version": version, "base_sha256": digest},
            content=_udiff(base, new).encode("utf-8"),
        )
        assert r.status_code == 200, r.text
        assert (await c.get(f"/annotations/{aid}/body/raw", headers=h)).text == new
