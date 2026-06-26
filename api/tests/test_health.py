"""API health smoke (no DB)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mycelium_api.app import create_app


def test_healthz() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
