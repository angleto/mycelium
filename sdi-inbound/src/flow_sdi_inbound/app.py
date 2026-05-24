"""SDI inbound service.

Per docs/adr/0011 this is an always-on, mutual-TLS endpoint that SdI pushes
active-cycle notifications (RC/MC/NS/AT) to (not a worker poll). Mutual TLS is
enforced at the edge (reverse proxy / ``uvicorn --ssl-*``); this app parses the
notification, correlates it to the tenant by ``IdentificativoSdI`` and applies
the outcome via ``flow_core.services.sdi_inbound``.

The exact SOAP ``esito`` response shape is verified against the WSDL at
accreditation (F7c); until then a 200 acks receipt and an unparseable payload
gets a 400 (never a 500, so SdI's retry/log path stays clean).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from flow_core.services.sdi_inbound import ingest_notification


def create_app() -> FastAPI:
    app = FastAPI(title="Flow SDI inbound", version="0.1.0")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sdi/notification", tags=["sdi"])
    async def notification(request: Request) -> Response:
        raw = await request.body()
        try:
            await ingest_notification(raw)
        except ValueError:
            # Unrecognized payload: 400 so SdI / logs surface it, not a 500.
            return Response(status_code=400)
        return Response(status_code=200)

    return app
