"""ASGI entry point: `uvicorn flow_sdi_inbound.main:app`."""

from __future__ import annotations

from flow_sdi_inbound.app import create_app

app = create_app()
