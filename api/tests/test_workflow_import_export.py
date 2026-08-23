"""Workflow export/import over HTTP (docs/adr/0052).

The service tests cover the rules; this covers the door: that the
document really is downloadable and re-uploadable as a file, that the
write endpoints need the same role the rest of the workflow router
needs, and that pydantic is not quietly repairing the body on its way
in. The last one is the reason the schemas are ``strict``: outside
strict mode ``"is_terminal": "true"`` arrives as ``True``, and a
lifecycle flag flipped by a string is an import that looks like it
worked.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from mycelium_api.main import app


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "kind": "mycelium.workflow",
        "version": 1,
        "name": "Delivery",
        "description": "From intake to delivery",
        "states": [
            {"name": "todo", "is_initial": True, "description": "Not started"},
            {"name": "in_progress"},
            {"name": "done", "is_terminal": True, "is_hidden": True},
        ],
        "transitions": [
            {"from_state": "todo", "to_state": "in_progress"},
            {"from_state": "in_progress", "to_state": "done"},
        ],
    }
    doc.update(over)
    return doc


async def _owner(c: AsyncClient) -> dict[str, str]:
    a = (await c.post("/auth/signup", json={"email": _email(), "password": "pw-strong-123"})).json()
    # Workflow writes need the effective role admin; the header is
    # clamped to the membership and absent means member (least privilege).
    return {
        "Authorization": f"Bearer {a['token']}",
        "X-Workspace-Id": a["workspace_id"],
        "X-Workspace-Role": "owner",
    }


async def test_import_export_round_trip_over_http() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)

        created = await c.post("/workflows/import", headers=h, json=_doc())
        assert created.status_code == 200, created.text
        wf_id = created.json()["id"]
        assert created.json()["name"] == "Delivery"
        # A file never arrives as the workspace default.
        assert created.json()["is_default"] is False

        r = await c.get(f"/workflows/{wf_id}/export", headers=h)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["content-disposition"] == 'attachment; filename="workflow-Delivery.json"'
        exported = json.loads(r.text)

        assert exported["kind"] == "mycelium.workflow"
        assert exported["version"] == 1
        assert [s["name"] for s in exported["states"]] == ["todo", "in_progress", "done"]
        assert exported["states"][2]["is_hidden"] is True
        assert exported["states"][0]["description"] == "Not started"
        assert exported["transitions"] == [
            {"from_state": "todo", "to_state": "in_progress"},
            {"from_state": "in_progress", "to_state": "done"},
        ]
        # No database identity travels: this is what makes the file
        # meaningful in another workspace.
        assert "id" not in exported
        assert all("id" not in s for s in exported["states"])

        # The downloaded bytes are accepted verbatim by the importer.
        again = await c.post("/workflows/import", headers=h, params={"name": "Copy"}, json=exported)
        assert again.status_code == 200, again.text
        back = await c.get(f"/workflows/{again.json()['id']}/export", headers=h)
        assert json.loads(back.text) == {**exported, "name": "Copy"}


async def test_import_into_an_existing_workflow_keeps_state_ids() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        wf_id = (await c.post("/workflows/import", headers=h, json=_doc())).json()["id"]
        before = {
            s["name"]: s["id"]
            for s in (await c.get(f"/workflows/{wf_id}/states", headers=h)).json()
        }

        grown = _doc(
            name="Delivery v2",
            states=[
                {"name": "todo", "is_initial": True},
                {"name": "in_progress"},
                {"name": "in_review"},
                {"name": "done", "is_terminal": True},
            ],
            transitions=[
                {"from_state": "todo", "to_state": "in_progress"},
                {"from_state": "in_progress", "to_state": "in_review"},
                {"from_state": "in_review", "to_state": "done"},
            ],
        )
        r = await c.post(f"/workflows/{wf_id}/import", headers=h, json=grown)
        assert r.status_code == 204, r.text

        after = {
            s["name"]: s["id"]
            for s in (await c.get(f"/workflows/{wf_id}/states", headers=h)).json()
        }
        # Every state the document named again is the same row: the
        # tasks standing in them never moved.
        assert {n: after[n] for n in before} == before
        assert set(after) == {"todo", "in_progress", "in_review", "done"}
        assert (
            next(w for w in (await c.get("/workflows", headers=h)).json() if w["id"] == wf_id)[
                "name"
            ]
            == "Delivery v2"
        )


async def test_a_malformed_document_is_refused_with_the_offending_rule() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)

        cases: list[tuple[dict[str, Any], str]] = [
            ({"kind": "mycelium.note"}, "workflow.doc_kind"),
            ({"version": 2}, "workflow.doc_version"),
            ({"name": "   "}, "workflow.doc_name"),
            ({"states": [], "transitions": []}, "workflow.doc_no_states"),
            (
                {"states": [{"name": "todo"}, {"name": "done"}], "transitions": []},
                "workflow.doc_initial_count",
            ),
            (
                {
                    "states": [{"name": "todo", "is_initial": True}, {"name": "todo"}],
                    "transitions": [],
                },
                "workflow.doc_duplicate_state",
            ),
            (
                {"transitions": [{"from_state": "todo", "to_state": "shipped"}]},
                "workflow.doc_unknown_state",
            ),
            (
                {
                    "transitions": [
                        {"from_state": "todo", "to_state": "in_progress"},
                        {"from_state": "todo", "to_state": "in_progress"},
                    ]
                },
                "workflow.doc_duplicate_transition",
            ),
        ]
        for over, code in cases:
            r = await c.post("/workflows/import", headers=h, json=_doc(**over))
            assert r.status_code == 400, (over, r.status_code, r.text)
            body = r.json()
            assert body["code"] == code, (over, body)
            # The message is rendered, not a bare code, and names what
            # to fix: the caller is holding a file.
            assert body["detail"] and body["detail"] != code

        # Nothing was created by any of the refusals.
        assert [w["name"] for w in (await c.get("/workflows", headers=h)).json()] == ["Default"]


async def test_a_flag_that_is_not_a_flag_is_refused_rather_than_coerced() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        lying = _doc(
            states=[
                {"name": "todo", "is_initial": True},
                {"name": "done", "is_terminal": "true"},
            ],
            transitions=[{"from_state": "todo", "to_state": "done"}],
        )
        r = await c.post("/workflows/import", headers=h, json=lying)
        assert r.status_code == 422, r.text
        # FastAPI's own validation envelope: an array of {loc,msg}. The
        # SPA and the CLI both already render this shape.
        assert any("is_terminal" in str(d.get("loc", "")) for d in r.json()["detail"])


async def test_import_and_export_need_the_workflow_role() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _owner(c)
        wf_id = (await c.post("/workflows/import", headers=h, json=_doc())).json()["id"]

        # Same account, no X-Workspace-Role: clamped to member.
        member = {k: v for k, v in h.items() if k != "X-Workspace-Role"}
        assert (await c.post("/workflows/import", headers=member, json=_doc())).status_code == 403
        assert (
            await c.post(f"/workflows/{wf_id}/import", headers=member, json=_doc())
        ).status_code == 403
        # Export is a read, and a member may read.
        assert (await c.get(f"/workflows/{wf_id}/export", headers=member)).status_code == 200


async def test_a_workflow_from_another_workspace_is_not_exportable() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        mine = await _owner(c)
        theirs = await _owner(c)
        wf_id = (await c.post("/workflows/import", headers=theirs, json=_doc())).json()["id"]
        # RLS scopes the lookup: someone else's id is absent, not readable.
        r = await c.get(f"/workflows/{wf_id}/export", headers=mine)
        # 400, not 404: WORKFLOW_NOT_FOUND is a plain DomainError
        # everywhere in this service (get_default_workflow,
        # update_workflow, delete_workflow, set_default_workflow all
        # answer 400 for it). Export follows the surface it belongs to
        # rather than being the one route that answers differently.
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "workflow.not_found"
