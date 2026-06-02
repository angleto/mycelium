"""GET /garden/health (ADR-0035).

Thin-adapter test: the metric computations are unit-tested in
``core/tests/test_garden_health.py``; here we pin the HTTP contract --
all seven sensors present, the headline sensor's floor, the explicit
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
