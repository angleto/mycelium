"""SdICoop transport (F7b): pure envelope build + response parse + file
naming. No network (the live send is config-gated and never exercised in CI).
"""

from __future__ import annotations

import base64

import lxml.etree as ET
import pytest

from flow_core.services.sdi_transport import (
    build_ricevifile_envelope,
    fatturapa_filename,
    parse_ricevifile_response,
    transmission_progressivo,
)


def test_fatturapa_filename() -> None:
    assert fatturapa_filename("IT", "01234567890", "00001") == "IT01234567890_00001.xml"


def test_transmission_progressivo_is_padded_base36() -> None:
    assert transmission_progressivo(1) == "00001"
    assert transmission_progressivo(36) == "00010"
    assert transmission_progressivo(2) != transmission_progressivo(3)


def test_envelope_carries_filename_and_base64_xml() -> None:
    xml = "<FatturaElettronica>x</FatturaElettronica>"
    env = build_ricevifile_envelope(filename="IT01234567890_00001.xml", xml=xml)
    texts = {e.text for e in ET.fromstring(env).iter() if e.text}
    assert "IT01234567890_00001.xml" in texts
    assert base64.b64encode(xml.encode()).decode() in texts


def test_parse_response_extracts_identificativo() -> None:
    resp = b'<ns:r xmlns:ns="urn:x"><IdentificativoSdI>SDI12345</IdentificativoSdI></ns:r>'
    assert parse_ricevifile_response(resp) == "SDI12345"


def test_parse_response_missing_identificativo_raises() -> None:
    with pytest.raises(ValueError):
        parse_ricevifile_response(b"<r><Other>1</Other></r>")
