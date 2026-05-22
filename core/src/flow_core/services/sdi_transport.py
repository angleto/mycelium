"""SdICoop transmission transport (docs/adr/0011, FR-9 / F7b).

The accredited-channel web service that sends a FatturaPA file to SdI and
returns the ``IdentificativoSdI``. SOAP 1.1 over mutual TLS (the channel's
client certificate). The envelope build + response parse are pure and
unit-tested; the live POST is config-gated and never exercised in CI (it
needs accreditation + real certificates).

VERIFY against the AdE test environment before going live: the exact
service ``targetNamespace`` / operation name and whether the SOAP body must
be WS-Security signed for your accreditation profile. Mutual TLS (client
cert) is always required; the host/path is environment-specific
(``FLOW_SDI_ENDPOINT_URL``). The request/response *shape* below follows the
long-standing SdICoop ``RiceviFile`` contract (NomeFile + base64 File ->
IdentificativoSdI); the parser is namespace-agnostic so a namespace revision
does not silently break correlation.
"""

from __future__ import annotations

import base64
import string

import httpx
import lxml.etree as ET

# SdICoop "Trasmissione" (RiceviFile) service namespace. Confirm against the
# WSDL handed over at accreditation; kept in one place so a revision is a
# one-line change.
_RICEVI_NS = "http://www.fatturapa.it/sdi/ws/ricezione/v1.0/types"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SEND_TIMEOUT_S = 30.0


def fatturapa_filename(id_paese: str, id_codice: str, progressivo: str) -> str:
    """SdI transmission file name: ``IT{idfiscale}_{progressivo}.xml`` for an
    unsigned B2B/B2C file (a signed one would be ``.xml.p7m``)."""
    return f"{id_paese}{id_codice}_{progressivo}.xml"


_B36 = string.digits + string.ascii_uppercase


def transmission_progressivo(seq: int, width: int = 5) -> str:
    """Format a per-intermediary sequence as a zero-padded base36 string,
    reused for the file name + ProgressivoInvio. Width 5 supports 36**5 (~60M)
    transmissions per accredited channel before it would need to widen."""
    if seq < 0:
        raise ValueError("sequence must be non-negative")
    digits = ""
    n = seq
    while n:
        n, r = divmod(n, 36)
        digits = _B36[r] + digits
    return (digits or "0").rjust(width, "0")


def build_ricevifile_envelope(*, filename: str, xml: str) -> bytes:
    """Build the SOAP 1.1 ``RiceviFile`` request: the FatturaPA file name +
    its bytes (base64). Returned as UTF-8 bytes ready to POST."""
    env = ET.Element(f"{{{_SOAP_NS}}}Envelope", nsmap={"soapenv": _SOAP_NS, "tns": _RICEVI_NS})
    ET.SubElement(env, f"{{{_SOAP_NS}}}Header")
    body = ET.SubElement(env, f"{{{_SOAP_NS}}}Body")
    ricevi = ET.SubElement(body, f"{{{_RICEVI_NS}}}fileSdIAccoglienza")
    ET.SubElement(ricevi, "NomeFile").text = filename
    ET.SubElement(ricevi, "File").text = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return bytes(ET.tostring(env, xml_declaration=True, encoding="UTF-8"))


def parse_ricevifile_response(body: bytes) -> str:
    """Extract ``IdentificativoSdI`` from a RiceviFile SOAP response.
    Namespace-agnostic (matches by local element name) so a namespace
    revision in the WSDL does not break correlation. Raises ValueError if
    the element is absent (an unexpected/fault response)."""
    root = ET.fromstring(body)
    for el in root.iter():
        if isinstance(el.tag, str) and ET.QName(el).localname == "IdentificativoSdI":
            text = (el.text or "").strip()
            if text:
                return text
    raise ValueError("RiceviFile response has no IdentificativoSdI")


async def send_via_sdicoop(
    *,
    xml: str,
    filename: str,
    endpoint_url: str,
    client_cert: str,
    client_key: str,
    ca_bundle: str | None = None,
) -> str:
    """POST the FatturaPA file to the accredited SdICoop endpoint over mutual
    TLS and return the assigned ``IdentificativoSdI``. Network/TLS errors
    surface as ``httpx.HTTPError``; a malformed/fault response as
    ``ValueError``. Config-gated; never exercised in CI."""
    envelope = build_ricevifile_envelope(filename=filename, xml=xml)
    async with httpx.AsyncClient(
        cert=(client_cert, client_key),
        verify=ca_bundle or True,
        timeout=_SEND_TIMEOUT_S,
    ) as client:
        resp = await client.post(
            endpoint_url,
            content=envelope,
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"RiceviFile"'},
        )
    resp.raise_for_status()
    return parse_ricevifile_response(resp.content)
