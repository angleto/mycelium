"""Official SdI notification XSD validation (FR-9 hardening, ADR-0011).

SdI pushes outcome notifications (RC/MC/NS/AT in the v1 active cycle) to the
trasmittente's endpoint as XML. This validator checks the payload against the
official ``MessaggiTypes_v1.1`` schema (vendored under ``fatturapa_xsd/``)
*before* the namespace-agnostic XPath parser in ``sdi_inbound`` extracts
fields: a structurally invalid payload is a protocol bug (ours or theirs) and
must be surfaced, not silently tolerated by lax XPath.

The schema declares ``ds:Signature`` as required on every notification root.
Real SdI notifications are XAdES-signed; signature *verification* is a
separate concern (CAdES / XAdES, deferred for v1 per ADR-0011). To validate
the *business* payload (IdentificativoSdI, NomeFile, timestamps, outcome
codes, etc.) without conflating it with the cryptographic check, the schema
is loaded in memory and its ``ds:Signature`` references are rewritten to
``minOccurs=0``. The vendored XSD file on disk is *not* modified.
"""

from __future__ import annotations

import functools
import pathlib

import lxml.etree as ET

_XSD_FILE = pathlib.Path(__file__).parent / "fatturapa_xsd" / "MessaggiTypes_v1.1.xsd"

NS_MESSAGGI = "http://www.fatturapa.gov.it/sdi/messaggi/v1.0"

# v1 active-cycle notifications (ADR-0011). NE/DT/EC/SE/MT are post-v1.
V1_NOTIFICATION_ROOTS: frozenset[str] = frozenset(
    {
        "RicevutaConsegna",
        "NotificaScarto",
        "NotificaMancataConsegna",
        "AttestazioneTrasmissioneFattura",
    }
)


_XSD_NS = "http://www.w3.org/2001/XMLSchema"


@functools.lru_cache(maxsize=1)
def _schema() -> ET.XMLSchema:
    """Compile the official MessaggiTypes schema once, with ``ds:Signature``
    relaxed to ``minOccurs=0`` in memory. The vendored XSD file is not
    modified; xmldsig-core import resolves to its sibling vendored file."""
    tree = ET.parse(str(_XSD_FILE))
    for el in tree.iter(f"{{{_XSD_NS}}}element"):
        if el.get("ref") == "ds:Signature":
            el.set("minOccurs", "0")
    return ET.XMLSchema(tree)


def validate_sdi_notification(xml: bytes | str) -> list[str]:
    """Validate an SdI notification XML against the official schema.

    Returns a list of human-readable ``line: message`` errors, empty when
    valid. Pre-strips ``ds:Signature`` so a fixture / unsigned-test payload
    is judged on its business structure alone. Never raises for an invalid
    payload; only a malformed string returns a single not-well-formed error.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        doc = ET.fromstring(xml)
    except ET.XMLSyntaxError as exc:
        return [f"XML not well-formed: {exc}"]
    qname = ET.QName(doc)
    if qname.localname not in V1_NOTIFICATION_ROOTS:
        return [
            f"line {doc.sourceline}: root '{qname.localname}' is not a supported "
            f"v1 notification (expected one of {sorted(V1_NOTIFICATION_ROOTS)})"
        ]
    if qname.namespace != NS_MESSAGGI:
        return [
            f"line {doc.sourceline}: namespace "
            f"'{qname.namespace}' is not the official SdI messaggi namespace "
            f"'{NS_MESSAGGI}'"
        ]
    schema = _schema()
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]
