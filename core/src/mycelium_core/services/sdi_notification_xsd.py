"""Official SdI notification XSD validation (FR-9 hardening, ADR-0011).

SdI exchanges several notification types between trasmittente and ricevente
(see ``MessaggiTypes_v1.1`` schema, vendored under ``fatturapa_xsd/``); this
module is the schema gate that runs before any parser extracts fields, so a
structurally invalid payload is a protocol bug (ours or theirs) and gets
surfaced, not silently tolerated by lax XPath.

Routing of which type is allowed on which endpoint and how it is applied to
domain state is a separate concern (see ``sdi_inbound`` for the active cycle
and ``sdi_passive`` for the receiver cycle). This validator answers a single
question: *is this XML a well-formed SdI notification of any known kind?*

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
# B2B/B2C ("privati") notification namespace. SdI delivers FPR12-cycle
# notifications here (verified live 2026-05-30), NOT in the PA NS_MESSAGGI; we
# do not vendor this schema, so its payloads pass on the structural root check.
NS_MESSAGGI_PRIVATI = "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fattura/messaggi/v1.0"

# Notification roots we recognise. The PA/B2G subset is declared by the
# vendored MessaggiTypes_v1.1; the B2B/B2C "privati" subset (Ricevuta*) lives
# in a separate ivaservizi namespace we do not vendor. Semantic routing
# (active vs receiver) is in ``sdi_inbound`` / ``sdi_passive``; here we
# enumerate every known kind across both schemas.
ACTIVE_CYCLE_ROOTS: frozenset[str] = frozenset(
    {
        # Trasmittente receives these after pushing a FatturaElettronica to SdI.
        # PA / B2G roots (MessaggiTypes_v1.1):
        "RicevutaConsegna",
        "NotificaScarto",
        "NotificaMancataConsegna",
        "AttestazioneTrasmissioneFattura",
        "NotificaEsito",
        # B2B/B2C "privati" roots (ivaservizi messaggi namespace), verified
        # live 2026-05-30: a scarto is a RicevutaScarto, an undeliverable is a
        # RicevutaImpossibilitaRecapito (RicevutaConsegna is shared above).
        "RicevutaScarto",
        "RicevutaImpossibilitaRecapito",
    }
)
RECEIVER_CYCLE_ROOTS: frozenset[str] = frozenset(
    {
        # Ricevente receives these as the addressee of a passive FatturaElettronica.
        "MetadatiInvioFile",
        "NotificaEsitoCommittente",
        "ScartoEsitoCommittente",
    }
)
# DT (NotificaDecorrenzaTermini) is dual-direction: SdI sends it both to the
# trasmittente (deemed acceptance after the 15-day window) and to the
# ricevente (same event, opposite point of view). Listed in both subsets.
DUAL_CYCLE_ROOTS: frozenset[str] = frozenset({"NotificaDecorrenzaTermini"})

ALL_NOTIFICATION_ROOTS: frozenset[str] = (
    ACTIVE_CYCLE_ROOTS | RECEIVER_CYCLE_ROOTS | DUAL_CYCLE_ROOTS
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
    valid. The check is signature-agnostic (the schema's ``ds:Signature`` is
    relaxed in memory); root must be one of ``ALL_NOTIFICATION_ROOTS`` and
    in the official messaggi namespace. Never raises for an invalid payload;
    only a malformed string returns a single not-well-formed error.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        doc = ET.fromstring(xml)
    except ET.XMLSyntaxError as exc:
        return [f"XML not well-formed: {exc}"]
    qname = ET.QName(doc)
    if qname.localname not in ALL_NOTIFICATION_ROOTS:
        return [
            f"line {doc.sourceline}: root '{qname.localname}' is not a known "
            f"SdI notification (expected one of {sorted(ALL_NOTIFICATION_ROOTS)})"
        ]
    if qname.namespace not in (NS_MESSAGGI, NS_MESSAGGI_PRIVATI):
        return [
            f"line {doc.sourceline}: namespace '{qname.namespace}' is neither the "
            f"PA messaggi namespace '{NS_MESSAGGI}' nor the B2B/B2C privati "
            f"namespace '{NS_MESSAGGI_PRIVATI}'"
        ]
    if qname.namespace == NS_MESSAGGI_PRIVATI:
        # The vendored MessaggiTypes_v1.1 schema is the PA one; we do not vendor
        # the privati schema, so a recognised privati root + namespace is
        # accepted on structure alone (the field extractor is namespace-agnostic
        # and the inbound app still ACKs receipt). Strict XSD runs for PA only.
        return []
    schema = _schema()
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]
