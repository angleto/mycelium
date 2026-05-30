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

import base64
import logging

import lxml.etree as ET
from fastapi import FastAPI, Request, Response

from flow_core.services.sdi_inbound import ingest_notification
from flow_core.services.sdi_passive import (
    ingest_passive_invoice,
    ingest_receiver_notification,
    is_passive_delivery,
    is_receiver_notification,
)
from flow_core.services.sdi_transport import _decode_soap_response

_log = logging.getLogger("flow_sdi_inbound")


def create_app() -> FastAPI:
    app = FastAPI(title="Flow SDI inbound", version="0.1.0")

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/sdi/notification", tags=["sdi"])
    async def notification(request: Request) -> Response:
        raw = await request.body()
        ct = request.headers.get("content-type")
        # SdI delivers notifications as a SOAP call that its Axis2 stack wraps
        # in an MTOM/XOP multipart body (same shape as the RiceviFile
        # responses); the bare parsers below do ET.fromstring and choke on the
        # leading MIME boundary -> 400 -> SdI marks delivery failed. Strip to
        # the SOAP envelope first with the shared decoder. The base64 line is
        # temporary instrumentation to lock SdI's exact wire format from a real
        # delivery (TODO: drop once the format is confirmed).
        _log.warning(
            "SDI-INBOUND ct=%s len=%d b64=%s", ct, len(raw), base64.b64encode(raw).decode()
        )
        payload = _decode_soap_response(raw, ct)
        try:
            if is_passive_delivery(payload):
                await ingest_passive_invoice(payload)
            elif is_receiver_notification(payload):
                # MT / SE: notifications on an invoice WE received.
                await ingest_receiver_notification(payload)
            else:
                # Active-cycle: RC/MC/NS/AT/NE/DT (DT transmitter-side).
                await ingest_notification(payload)
        except (ValueError, ET.XMLSyntaxError) as exc:
            # Unrecognized / not-well-formed payload: 400 so SdI / logs
            # surface it, not a 500 (ADR-0011: never 500 on the SdI push
            # path or its retry/log behaviour gets noisy). lxml raises
            # XMLSyntaxError on malformed XML; the service-layer parsers
            # raise ValueError on a structurally unknown notification or
            # FatturaElettronica.
            _log.warning("SDI-INBOUND parse failed: %s", exc)
            return Response(status_code=400)
        return Response(status_code=200)

    return app
