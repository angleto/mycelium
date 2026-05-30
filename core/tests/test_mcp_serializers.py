"""Unit tests for MCP serializer shaping (no DB).

Exercises the ``_compact`` null-dropping convention on the read
serializers (``_task_full`` / ``_client`` / ``_project``) with
duck-typed mocks: an unset nullable column is dropped, but a real falsy
value (``False`` / ``0`` / ``""`` / ``[]``) is kept.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from flow_mcp.server import _client, _compact, _project, _task_full


def test_compact_drops_only_none() -> None:
    src = {
        "a": None,
        "keep_false": False,
        "keep_zero": 0,
        "keep_empty": "",
        "keep_list": [],
        "x": "v",
    }
    assert _compact(src) == {
        "keep_false": False,
        "keep_zero": 0,
        "keep_empty": "",
        "keep_list": [],
        "x": "v",
    }


def _mock_task(**over: Any) -> Any:
    base = dict(
        id="22222222-2222-2222-2222-222222222222",
        title="A task",
        description=None,
        state_id="33333333-3333-3333-3333-333333333333",
        priority=2,
        importance=4,
        urgency=3,
        start_date=None,
        due_date=None,
        billable=True,
        parent_task_id=None,
        estimate_effort_h=None,
        required_capabilities=[],
        monetary_cost=None,
        location=None,
        necessity=SimpleNamespace(value="should"),
        budget_id=None,
        is_archived=False,
        offered=False,
        deleted_at=None,
        version=1,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_task_full_drops_unset_nullables_keeps_falsy() -> None:
    out = _task_full(_mock_task(), [])
    # Unset nullable columns are absent, not null.
    for k in (
        "description",
        "start_date",
        "due_date",
        "parent_task_id",
        "estimate_effort_h",
        "monetary_cost",
        "location",
        "budget_id",
        "deleted_at",
    ):
        assert k not in out, f"{k} should be dropped when None"
    # Real falsy values survive.
    assert out["billable"] is True
    assert out["is_archived"] is False
    assert out["offered"] is False
    assert out["required_capabilities"] == []
    assert out["tags"] == []
    # Identity / required scalars always present.
    assert out["id"] and out["title"] and out["state_id"]
    assert out["priority"] == 2 and out["version"] == 1


def test_task_full_keeps_set_values() -> None:
    out = _task_full(_mock_task(description="real", location="Rome", due_date=None), [])
    assert out["description"] == "real"
    assert out["location"] == "Rome"
    assert "due_date" not in out  # still dropped, it stayed None


def test_client_drops_unset_card_fields_keeps_falsy() -> None:
    tag = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="ACME",
        status="active",
        version=1,
    )
    prof = SimpleNamespace(
        ragione_sociale="ACME S.p.A.",
        id_paese="IT",
        id_codice="12345678901",
        codice_fiscale=None,
        indirizzo=None,
        cap=None,
        comune=None,
        provincia=None,
        nazione=None,
        codice_destinatario=None,
        pec=None,
        description=None,
        default_billable=False,  # a real value, must survive
        tariffa=None,
        valuta="EUR",
    )
    out = _client(tag, prof)
    for k in ("codice_fiscale", "indirizzo", "pec", "description", "tariffa", "nazione"):
        assert k not in out, f"{k} should be dropped when None"
    assert out["ragione_sociale"] == "ACME S.p.A."
    assert out["valuta"] == "EUR"
    assert out["default_billable"] is False  # falsy but real → kept


def test_project_drops_unset_fields() -> None:
    tag = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="Website",
        status="active",
        version=1,
        color=None,
    )
    prof = SimpleNamespace(
        client_tag_id=None,
        budget=None,
        description=None,
        workflow_id=None,
    )
    out = _project(tag, prof)
    for k in ("client_tag_id", "budget", "description", "workflow_id", "color"):
        assert k not in out, f"{k} should be dropped when None"
    assert out["id"] and out["name"] == "Website" and out["version"] == 1
