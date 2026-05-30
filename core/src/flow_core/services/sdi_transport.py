"""SdICoop transmission transport (docs/adr/0011, FR-9 / F7b).

The accredited-channel web service that sends a FatturaPA file to SdI and
returns the ``IdentificativoSdI``. SOAP 1.1 over mutual TLS (the channel's
client certificate). The envelope build + response parse are pure and
unit-tested; the live POST is config-gated and never exercised in CI (it
needs accreditation + real certificates).

The service contract was VERIFIED against the live AdE test WSDL on
2026-05-30 (``SdIRiceviFile_v1.0.wsdl`` at testservizi.fatturapa.it,
target/types namespace ``.../sdi/ws/trasmissione/v1.0[/types]``, binding
document/literal, no WS-Security policy: only mutual TLS is required). The
canonical SOAP endpoint advertised by the WSDL is
``https://<host>/SdI2AccoglienzaWeb/SdIRiceviFile_service`` (the historical
``/ricevi_file`` alias resolves to the same Axis2 service). Mutual TLS
(client cert) is always required; the host is environment-specific
(``FLOW_SDI_ENDPOINT_URL``: testservizi for accreditation, servizi for
prod). The request/response shape is the SdICoop ``RiceviFile`` contract
(NomeFile + base64 File -> IdentificativoSdI [+ optional Errore]); the parser
is namespace-agnostic so a future namespace revision does not silently break
correlation.
"""

from __future__ import annotations

import base64
import re
import ssl
import string

import httpx
import lxml.etree as ET

# SdICoop "Trasmissione" (SdIRiceviFile) service. The request/response
# elements (fileSdIAccoglienza / rispostaSdIRiceviFile) live in the *types*
# namespace below; the operation's SOAPAction is a fixed URI. Both were read
# off the live WSDL at testservizi.fatturapa.it on 2026-05-30
# (SdIRiceviFile_v1.0.wsdl + TrasmissioneTypes_v1.0.xsd), so they are exact,
# not guessed. Note the deliberate host mismatch AdE ships: the schema
# namespace uses ``fatturapa.gov.it`` while the SOAPAction uses
# ``fatturapa.it`` -- both verbatim from the WSDL. Kept in one place so a
# future revision is a one-line change.
_RICEVI_NS = "http://www.fatturapa.gov.it/sdi/ws/trasmissione/v1.0/types"
_RICEVI_SOAP_ACTION = "http://www.fatturapa.it/SdIRiceviFile/RiceviFile"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SEND_TIMEOUT_S = 30.0


def _mtls_ssl_context(
    *, client_cert: str, client_key: str, ca_bundle: str | None
) -> ssl.SSLContext:
    """Build the SSL context for the SdI mutual-TLS POST: trust the curated
    CA bundle (or the system store when none is given) and load the client
    certificate chain. Replaces httpx's deprecated ``cert=`` kwarg (httpx
    0.28+); the context is a Python stdlib type so unit tests stay portable
    and respx is unaffected (it intercepts before TLS).

    With an explicit ``ca_bundle`` we enable PARTIAL_CHAIN. AdE distributes
    the SdI trust anchors as the certs to import, and the production server
    ``servizi.fatturapa.it`` chains to ``Sectigo Public Server Authentication
    Root R46`` in its USERTrust-cross-signed (i.e. *not* self-signed) form.
    Without PARTIAL_CHAIN OpenSSL refuses to treat R46 as a terminal anchor
    and keeps walking up looking for USERTrust, so the handshake fails even
    though R46+R36 are in the bundle. PARTIAL_CHAIN lets validation stop at
    any anchor present in the bundle, so the AdE-supplied Sectigo R46+R36
    pair verifies the prod leaf on its own. The test endpoint's AdE-internal
    CA (a self-signed root) is unaffected by the flag."""
    if ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    else:
        ctx = ssl.create_default_context()
    ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    return ctx


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


def _decode_soap_response(content: bytes, content_type: str | None) -> bytes:
    """Return the SOAP-envelope XML out of a RiceviFile/RiceviNotifica reply.

    SdI's Axis2 stack answers with an MTOM/XOP ``multipart/related`` body --
    the SOAP envelope is the root ``application/xop+xml`` part, framed by a
    MIME boundary, NOT bare XML -- so feeding the raw bytes to an XML parser
    fails on the leading boundary. We pull the first XML part out of the
    multipart when a boundary is advertised, then slice from the first ``<``
    to the last ``>`` to drop any MIME preamble/epilogue. A plain
    ``text/xml`` response (or a bare envelope) passes through unchanged."""
    ct = content_type or ""
    boundary: bytes | None = None
    m = re.search(r'boundary="?([^";\r\n]+)"?', ct)
    if m:
        boundary = ("--" + m.group(1)).encode()
    elif content.lstrip().startswith(b"--"):
        # No usable Content-Type, but the body self-describes as a MIME
        # multipart: take the boundary delimiter from its first line.
        boundary = content.lstrip().split(b"\r\n", 1)[0].rstrip()
    if boundary:
        for part in content.split(boundary):
            _, sep, part_body = part.partition(b"\r\n\r\n")
            if sep and b"<" in part_body:
                content = part_body
                break
    start = content.find(b"<")
    end = content.rfind(b">")
    if start != -1 and end != -1 and start <= end:
        return content[start : end + 1]
    return content


def parse_ricevifile_response(body: bytes) -> str:
    """Extract ``IdentificativoSdI`` from a RiceviFile SOAP response.

    Namespace-agnostic (matches by local element name) so a namespace
    revision in the WSDL does not break correlation. The response type
    (``rispostaSdIRiceviFile_Type``) always carries an ``IdentificativoSdI``
    plus, on a transport-level rejection, an optional ``Errore`` (EI01 file
    vuoto / EI02 servizio non disponibile / EI03 utente non abilitato). A
    present, non-empty ``Errore`` means the file was NOT accepted, so we
    raise rather than hand back an ``IdentificativoSdI`` that would be
    mistaken for a successful submission. Raises ValueError when no
    ``IdentificativoSdI`` is present at all (a SOAP fault / unexpected
    shape), including a short body excerpt for diagnosis."""
    body = _decode_soap_response(body, None)
    root = ET.fromstring(body)
    identificativo: str | None = None
    errore: str | None = None
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = ET.QName(el).localname
        if local == "IdentificativoSdI" and (el.text or "").strip():
            identificativo = el.text.strip()
        elif local == "Errore" and (el.text or "").strip():
            errore = el.text.strip()
    if errore:
        raise ValueError(f"RiceviFile rejected the file: Errore={errore}")
    if identificativo:
        return identificativo
    excerpt = body[:400].decode("utf-8", "replace")
    raise ValueError(f"RiceviFile response has no IdentificativoSdI: {excerpt}")


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
    ctx = _mtls_ssl_context(client_cert=client_cert, client_key=client_key, ca_bundle=ca_bundle)
    async with httpx.AsyncClient(verify=ctx, timeout=_SEND_TIMEOUT_S) as client:
        resp = await client.post(
            endpoint_url,
            content=envelope,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{_RICEVI_SOAP_ACTION}"',
            },
        )
    resp.raise_for_status()
    return parse_ricevifile_response(
        _decode_soap_response(resp.content, resp.headers.get("content-type"))
    )


# --- EsitoCommittente outbound (SdIRiceviNotifica) -----------------------------
# SDICoop ``RiceviNotifica`` is the SdI-side ingress for buyer-generated
# NotificaEsitoCommittente. The wire shape mirrors RiceviFile: a SOAP body
# with NomeFile + base64-encoded XML, and a response that carries either an
# acknowledgement (no specific identifier; SdI just confirms receipt) or a
# fault with an error code. The namespace/operation/SOAPAction below
# *must* be verified against the AdE-handed WSDL at accreditation: the
# Trasmissione and Ricezione services share a family of namespaces but the
# RiceviNotifica operation has historically been advertised under
# ``ricezione/v1.0``. One-line change here when the canonical name lands.
_NOTIFICA_NS = "http://www.fatturapa.it/sdi/ws/ricezione/v1.0/types"


def build_notificaesito_envelope(*, filename: str, signed_xml: bytes) -> bytes:
    """Build the SOAP 1.1 ``NotificaEsito`` request that ships a signed
    NotificaEsitoCommittente to SdI. Filename follows the AdE convention
    ``IT{idfiscale}_{progressivo}_EC_{progressivoEsito}.xml`` (caller-built;
    we pass through here)."""
    env = ET.Element(
        f"{{{_SOAP_NS}}}Envelope",
        nsmap={"soapenv": _SOAP_NS, "tns": _NOTIFICA_NS},
    )
    ET.SubElement(env, f"{{{_SOAP_NS}}}Header")
    body = ET.SubElement(env, f"{{{_SOAP_NS}}}Body")
    op = ET.SubElement(body, f"{{{_NOTIFICA_NS}}}notificaEsito")
    ET.SubElement(op, "NomeFile").text = filename
    ET.SubElement(op, "File").text = base64.b64encode(signed_xml).decode("ascii")
    return bytes(ET.tostring(env, xml_declaration=True, encoding="UTF-8"))


def parse_notificaesito_response(body: bytes) -> str:
    """Read the ``EsitoRicezione`` (or analogous ack element) from the
    SdIRiceviNotifica response. SdI typically returns a short code like
    ``ER01`` on success; on a content rejection a SOAP fault carries the
    error detail. Namespace-agnostic match by local name keeps a WSDL
    namespace revision from silently breaking the integration."""
    body = _decode_soap_response(body, None)
    root = ET.fromstring(body)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = ET.QName(el).localname
        if local in {"EsitoRicezione", "Esito", "Ack"}:
            text = (el.text or "").strip()
            if text:
                return text
    raise ValueError("RiceviNotifica response has no EsitoRicezione/Esito/Ack")


def esito_filename(id_paese: str, id_codice: str, progressivo: str, esito_seq: str) -> str:
    """AdE convention for the EC file name: the FatturaPA file name with an
    ``_EC_<progressivo-esito>`` suffix. Both progressivi are caller-managed
    so this stays a pure formatter."""
    return f"{id_paese}{id_codice}_{progressivo}_EC_{esito_seq}.xml"


async def send_esito_via_sdicoop(
    *,
    signed_xml: bytes,
    filename: str,
    endpoint_url: str,
    client_cert: str,
    client_key: str,
    ca_bundle: str | None = None,
) -> str:
    """POST a signed NotificaEsitoCommittente to the SdIRiceviNotifica
    endpoint over mutual TLS and return the ack code. Same error contract
    as ``send_via_sdicoop`` (network/TLS = ``httpx.HTTPError``, malformed
    response = ``ValueError``). Config-gated; never exercised in CI."""
    envelope = build_notificaesito_envelope(filename=filename, signed_xml=signed_xml)
    ctx = _mtls_ssl_context(client_cert=client_cert, client_key=client_key, ca_bundle=ca_bundle)
    async with httpx.AsyncClient(verify=ctx, timeout=_SEND_TIMEOUT_S) as client:
        resp = await client.post(
            endpoint_url,
            content=envelope,
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"NotificaEsito"'},
        )
    resp.raise_for_status()
    return parse_notificaesito_response(
        _decode_soap_response(resp.content, resp.headers.get("content-type"))
    )
