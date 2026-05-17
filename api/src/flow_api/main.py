"""Entry point ASGI: `uvicorn flow_api.main:app`."""

from __future__ import annotations

from flow_api.app import create_app

app = create_app()
