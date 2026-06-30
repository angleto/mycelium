"""Entry point ASGI: `uvicorn mycelium_api.main:app`."""

from __future__ import annotations

from mycelium_api.app import create_app

app = create_app()
