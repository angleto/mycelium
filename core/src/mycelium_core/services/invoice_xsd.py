"""Official FatturaPA XSD validation (FR-9 hardening, ADR-0011).

``invoice_format`` builds a well-formed document with the right arithmetic,
but SdI *scarta* (rejects) anything that does not validate against the
official schema. Validating the built XML here catches an invalid document
at transmit/preview time, before it ever reaches SdI.

The official schema (current ``Schema_VFPA12_V1.2.3``, valid from
2025-04-01) and the W3C ``xmldsig-core-schema`` it imports are vendored
under ``fatturapa_xsd/``; the import ``schemaLocation`` is rewritten to the
local copy so validation is hermetic (no network at runtime). The schema's
``elementFormDefault`` is *unqualified*: only the root ``FatturaElettronica``
is namespace-qualified, every child is unqualified, which is exactly what
``invoice_format._build_xml`` emits.
"""

from __future__ import annotations

import functools
import pathlib

import lxml.etree as ET

_XSD_FILE = pathlib.Path(__file__).parent / "fatturapa_xsd" / "Schema_VFPA12_V1.2.3.xsd"


@functools.lru_cache(maxsize=1)
def _schema() -> ET.XMLSchema:
    """Compile the official schema once (it imports the vendored local
    xmldsig schema). Cached for the process lifetime."""
    return ET.XMLSchema(ET.parse(str(_XSD_FILE)))


def validate_fatturapa(xml: str) -> list[str]:
    """Validate a FatturaPA XML string against the official schema.

    Returns a list of human-readable ``line: message`` error strings, empty
    when the document is valid. Never raises for an invalid document (the
    caller decides whether to block); only a genuinely un-parseable string
    is reported as a single not-well-formed error."""
    try:
        doc = ET.fromstring(xml.encode("utf-8"))
    except ET.XMLSyntaxError as exc:
        return [f"XML not well-formed: {exc}"]
    schema = _schema()
    if schema.validate(doc):
        return []
    return [f"line {e.line}: {e.message}" for e in schema.error_log]
