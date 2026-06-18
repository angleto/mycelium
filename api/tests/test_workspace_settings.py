"""Per-workspace settings: configurable task-estimate presets.
Tenant-scoped GET/PATCH with optimistic concurrency + validation.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_estimate_presets_default_update_validation() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        # Owner with full entitlement: PATCH /workspaces/me/settings
        # needs the effective role admin, which is X-Workspace-Role
        # clamped to the membership (absent header => member).
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        me = (await c.get("/workspaces/me", headers=h)).json()
        # Defaults when nothing was configured yet.
        assert me["settings"]["estimate_presets"] == ["0.5", "1", "4", "8"]
        ver = me["version"]

        # Update; the validator sorts + dedups.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": ver, "estimate_presets": [2, 0.25, 2, 1]},
        )
        assert r.status_code == 200
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["estimate_presets"] == ["0.25", "1", "2"]

        # Stale version -> 409.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": ver, "estimate_presets": [1]},
        )
        assert r.status_code == 409

        # Non-positive preset -> 422 (schema validator).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": me["version"], "estimate_presets": [0]},
        )
        assert r.status_code == 422


async def test_retrieval_semantic_floor_roundtrip_and_merge() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        me = (await c.get("/workspaces/me", headers=h)).json()
        # Default: gate off.
        assert me["settings"]["retrieval_semantic_min_similarity"] == 0.0
        ver = me["version"]

        # Set the floor.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": ver,
                "estimate_presets": [1],
                "retrieval_semantic_min_similarity": 0.55,
            },
        )
        assert r.status_code == 200, r.text
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["retrieval_semantic_min_similarity"] == 0.55

        # A presets-only save must NOT clobber the floor (merge).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": me["version"], "estimate_presets": [2, 4]},
        )
        assert r.status_code == 200
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["retrieval_semantic_min_similarity"] == 0.55

        # Out of range -> 422 (schema validator, le=1.0).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": me["version"],
                "estimate_presets": [1],
                "retrieval_semantic_min_similarity": 1.5,
            },
        )
        assert r.status_code == 422


async def test_attachment_max_bytes_roundtrip_merge_and_clamp() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        me = (await c.get("/workspaces/me", headers=h)).json()
        # Default surfaced as the effective value (config default, clamped),
        # plus the hard ceiling that bounds the admin input.
        default = me["settings"]["attachment_max_bytes"]
        ceiling = me["settings"]["attachment_max_bytes_ceiling"]
        assert default > 0
        assert ceiling >= default
        ver = me["version"]

        # Raise the per-workspace cap (below the ceiling).
        override = 25 * 1024 * 1024
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": ver,
                "estimate_presets": [1],
                "attachment_max_bytes": override,
            },
        )
        assert r.status_code == 200, r.text
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["attachment_max_bytes"] == override

        # A presets-only save must NOT clobber the cap (merge).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={"expected_version": me["version"], "estimate_presets": [2, 4]},
        )
        assert r.status_code == 200
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["attachment_max_bytes"] == override

        # Above the ceiling -> clamped on write (the buffered path holds
        # the whole file in memory, so the ceiling is a hard bound).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": me["version"],
                "estimate_presets": [1],
                "attachment_max_bytes": ceiling + 999 * 1024 * 1024,
            },
        )
        assert r.status_code == 200, r.text
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["attachment_max_bytes"] == ceiling

        # Below the floor (0) -> 422 (schema validator, ge=1).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": me["version"],
                "estimate_presets": [1],
                "attachment_max_bytes": 0,
            },
        )
        assert r.status_code == 422


async def test_retrieval_grader_floor_roundtrip_and_merge() -> None:
    """WS-B1: the per-org grader/abstain floor round-trips through the
    workspace settings surface (default off, set, presets-only save must not
    clobber it, out-of-range rejected) -- same contract as the semantic
    floor, so the SPA can tune it live."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }

        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["retrieval_grader_min_rrf"] == 0.0  # default off
        ver = me["version"]

        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": ver,
                "estimate_presets": [1],
                "retrieval_grader_min_rrf": 0.03,
            },
        )
        assert r.status_code == 200, r.text
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["retrieval_grader_min_rrf"] == 0.03

        # A presets-only save must NOT clobber it (merge); the semantic
        # floor and the grader floor coexist independently.
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": me["version"],
                "estimate_presets": [2, 4],
                "retrieval_semantic_min_similarity": 0.5,
            },
        )
        assert r.status_code == 200
        me = (await c.get("/workspaces/me", headers=h)).json()
        assert me["settings"]["retrieval_grader_min_rrf"] == 0.03
        assert me["settings"]["retrieval_semantic_min_similarity"] == 0.5

        # Out of range -> 422 (schema validator, le=1.0).
        r = await c.patch(
            "/workspaces/me/settings",
            headers=h,
            json={
                "expected_version": me["version"],
                "estimate_presets": [1],
                "retrieval_grader_min_rrf": 1.5,
            },
        )
        assert r.status_code == 422
