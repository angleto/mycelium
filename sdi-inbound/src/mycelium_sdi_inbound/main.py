"""ASGI entry point: `uvicorn mycelium_sdi_inbound.main:app`."""

from __future__ import annotations

from mycelium_sdi_inbound.app import create_app

app = create_app()
