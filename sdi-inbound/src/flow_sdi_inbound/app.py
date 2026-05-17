"""SDI inbound service skeleton.

Per docs/adr/0011 this is an always-on, mutual-TLS SOAP endpoint that
SdI pushes notifications to (not a worker poll). F0 ships only a health
endpoint; the SOAP receiver lands in the SDI phases (F7b+).
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Flow SDI inbound", version="0.0.0")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
