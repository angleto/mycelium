"""Unit tests for MCP serializer shaping (no DB).

Exercises the ``_compact`` null-dropping convention on the read
serializers (``_task_full`` / ``_client`` / ``_project``) with
duck-typed mocks: an unset nullable column is dropped, but a real falsy
value (``False`` / ``0`` / ``""`` / ``[]``) is kept.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from mycelium_mcp.server import _client, _compact, _project, _task, _task_full


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
        assignee_id=None,
        owner_id=None,
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
        "assignee_id",
        "owner_id",
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
    out = _task_full(
        _mock_task(
            description="real",
            location="Rome",
            due_date=None,
            assignee_id="44444444-4444-4444-4444-444444444444",
            owner_id="55555555-5555-5555-5555-555555555555",
        ),
        [],
    )
    assert out["description"] == "real"
    assert out["location"] == "Rome"
    assert "due_date" not in out  # still dropped, it stayed None
    # Assignment/accountability ids are surfaced for read-back (901f0f9f).
    assert out["assignee_id"] == "44444444-4444-4444-4444-444444444444"
    assert out["owner_id"] == "55555555-5555-5555-5555-555555555555"


def test_task_lean_enriched_drops_unset_keeps_axes() -> None:
    # Enriched lean row (eb874772): the always-present planning axes stay,
    # the sparse fields are dropped when unset so they cost zero tokens.
    out = _task(_mock_task(), [])
    assert out["importance"] == 4
    assert out["urgency"] == 3
    assert out["necessity"] == "should"
    assert out["priority"] == 2 and out["version"] == 1
    assert out["id"] and out["title"] and out["state_id"]
    assert out["collaborators_count"] == 0  # real 0, kept
    assert out["tags"] == []  # empty list, kept
    for k in ("start_date", "due_date", "parent_task_id", "assignee_id", "owner_id"):
        assert k not in out, f"{k} should be dropped when None"


def test_task_lean_enriched_surfaces_set_fields() -> None:
    out = _task(
        _mock_task(
            start_date=dt.date(2026, 7, 1),
            due_date=dt.datetime(2026, 7, 5, tzinfo=dt.UTC),
            parent_task_id="66666666-6666-6666-6666-666666666666",
            assignee_id="44444444-4444-4444-4444-444444444444",
        ),
        [],
        collaborators_count=2,
    )
    assert out["start_date"] == "2026-07-01"
    assert out["due_date"] == "2026-07-05T00:00:00+00:00"
    assert out["parent_task_id"] == "66666666-6666-6666-6666-666666666666"
    assert out["assignee_id"] == "44444444-4444-4444-4444-444444444444"
    assert out["collaborators_count"] == 2


def test_client_drops_unset_card_fields_keeps_falsy() -> None:
    tag = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="ACME",
        status="active",
        version=1,
    )
    prof = SimpleNamespace(
        legal_name="ACME S.p.A.",
        country_code="IT",
        vat_number="12345678901",
        tax_code=None,
        address=None,
        postal_code=None,
        city=None,
        province=None,
        country=None,
        sdi_code=None,
        pec=None,
        description=None,
        default_billable=False,  # a real value, must survive
        hourly_rate=None,
        currency="EUR",
    )
    out = _client(tag, prof)
    for k in ("tax_code", "address", "pec", "description", "hourly_rate", "country"):
        assert k not in out, f"{k} should be dropped when None"
    assert out["legal_name"] == "ACME S.p.A."
    assert out["currency"] == "EUR"
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
