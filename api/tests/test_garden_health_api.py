"""GET /garden/health (ADR-0035).

Thin-adapter test: the metric computations are unit-tested in
``core/tests/test_garden_health.py``; here we pin the HTTP contract --
all sensors present, the headline sensor's floor, the explicit
null+reason on a fresh workspace (never a faked number), and the empty
trend before any daily snapshot exists.
"""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from flow_api.main import app

_SENSORS = (
    "accept_rate_classify_7d",
    "accept_rate_classify_30d",
    "time_to_first_link",
    "tag_entropy_local",
    "leiden_modularity",
    "fungal_lag",
    "density_delta_7d",
    "embedding_coverage",
    "autonomous_spend_today",
    "autonomous_accept_ratio",
)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient, ws: str = "GH") -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": ws},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def test_garden_health_contract() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/garden/health", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()

    assert "generated_at" in body
    metrics = body["metrics"]
    for key in _SENSORS:
        assert key in metrics, key

    # Headline sensor: declares its floor; a fresh workspace has no
    # classification decisions, so value is null with a reason -- not 0.0.
    ar = metrics["accept_rate_classify_7d"]
    assert ar["floor"] == 0.40
    assert ar["value"] is None
    assert ar["reason"]

    # No daily snapshot yet -> empty sparkline trend.
    assert body["trend"] == []


async def test_garden_health_timeseries_contract() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Default window and a custom one: a fresh workspace has no
        # persisted daily snapshot, so each returns an empty list (never
        # an error, never a faked point).
        r = await c.get("/garden/health/timeseries", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == []
        r2 = await c.get("/garden/health/timeseries?days=7", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json() == []
        # The window is bounded (1..365).
        r3 = await c.get("/garden/health/timeseries?days=0", headers=h)
        assert r3.status_code == 422


async def test_garden_health_events_empty_and_bounds() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Fresh workspace: nothing has changed yet -> empty timeline.
        r = await c.get("/garden/health/events", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == []
        # Window is bounded (1..365).
        r2 = await c.get("/garden/health/events?days=0", headers=h)
        assert r2.status_code == 422


async def test_garden_health_events_surfaces_bulk_create() -> None:
    """End-to-end: a burst of note creations over the API shows up on the
    what-changed timeline as a single corpus_edit, with the fields the
    SPA renders (at / kind / detail)."""
    from flow_core.services.garden_health import BULK_EDIT_THRESHOLD

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        for i in range(BULK_EDIT_THRESHOLD):
            rc = await c.post("/notes", headers=h, json={"kind": "text", "title": f"n{i}"})
            assert rc.status_code == 200, rc.text
        r = await c.get("/garden/health/events", headers=h)
        assert r.status_code == 200, r.text
        events = r.json()

    creates = [
        e for e in events if e["kind"] == "corpus_edit" and e["detail"]["action"] == "create"
    ]
    assert len(creates) == 1
    assert creates[0]["detail"]["count"] >= BULK_EDIT_THRESHOLD
    assert "at" in creates[0]
