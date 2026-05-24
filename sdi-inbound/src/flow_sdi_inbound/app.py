"""SDI inbound service.

Per docs/adr/0011 this is an always-on, mutual-TLS endpoint SdI pushes to.
Mutual TLS is enforced at the edge (Traefik TLSOption.clientAuth); this app
sees plain HTTP behind it. Two payload shapes hit the same path:

- **Active cycle** (notifications on invoices WE transmitted): RC, MC, NS,
  AT. Routed to ``flow_core.services.sdi_inbound.ingest_notification``.
- **Passive cycle** (FatturaElettronica WE receive as cessionario): routed
  to ``flow_core.services.sdi_passive.ingest_passive_invoice``. The
  RiceviFatture wrapper carries IdentificativoSdI + NomeFile + base64
  File; the inner FatturaElettronica.CodiceDestinatario resolves the
  recipient IssuerProfile -> org.

A single endpoint instead of two because the AdE portal lets us declare
the same URL for both Trasmissione and Ricezione, and a router predicate
based on the root element keeps the routing local + observable.

The exact SOAP ``esito`` response shape is verified against the WSDL at
accreditation (F7c); until then a 200 acks any successful ingestion and an
unparseable payload gets a 400 (never a 500, so SdI's retry/log path stays
clean).
"""

from __future__ import annotations

import lxml.etree as ET
from fastapi import FastAPI, Request, Response

from flow_core.services.sdi_inbound import ingest_notification
from flow_core.services.sdi_passive import ingest_passive_invoice, is_passive_delivery


def create_app() -> FastAPI:
    app = FastAPI(title="Flow SDI inbound", version="0.1.0")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sdi/notification", tags=["sdi"])
    async def notification(request: Request) -> Response:
        raw = await request.body()
        try:
            if is_passive_delivery(raw):
                await ingest_passive_invoice(raw)
            else:
                await ingest_notification(raw)
        except (ValueError, ET.XMLSyntaxError):
            # Unrecognized / not-well-formed payload: 400 so SdI / logs
            # surface it, not a 500 (ADR-0011: never 500 on the SdI push
            # path or its retry/log behaviour gets noisy). lxml raises
            # XMLSyntaxError on malformed XML; the service-layer parsers
            # raise ValueError on a structurally unknown notification or
            # FatturaElettronica.
            return Response(status_code=400)
        return Response(status_code=200)

    return app
