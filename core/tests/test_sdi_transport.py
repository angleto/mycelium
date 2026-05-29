"""SdICoop transport (F7b): pure envelope build + response parse + file
naming. No network (the live send is config-gated and never exercised in CI).

The mutual-TLS context builder is also covered here: it stays a pure stdlib
``ssl.SSLContext`` factory, so we can assert its verification flags without a
handshake.
"""

from __future__ import annotations

import base64
import datetime
import ssl

import lxml.etree as ET
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from flow_core.services.sdi_transport import (
    _mtls_ssl_context,
    build_notificaesito_envelope,
    build_ricevifile_envelope,
    esito_filename,
    fatturapa_filename,
    parse_notificaesito_response,
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


def test_esito_filename_appends_progressivo_esito() -> None:
    assert esito_filename("IT", "01234567890", "00001", "001") == "IT01234567890_00001_EC_001.xml"


def test_notificaesito_envelope_carries_filename_and_signed_payload() -> None:
    signed = b"<m:NotificaEsitoCommittente>...</m:NotificaEsitoCommittente>"
    env = build_notificaesito_envelope(filename="IT01234567890_00001_EC_001.xml", signed_xml=signed)
    texts = {e.text for e in ET.fromstring(env).iter() if e.text}
    assert "IT01234567890_00001_EC_001.xml" in texts
    assert base64.b64encode(signed).decode() in texts


def test_parse_notificaesito_response_extracts_ack() -> None:
    # SdI ack of buyer-EC reception: a short alphanumeric code; pick a
    # plausible shape and confirm namespace-agnostic extraction.
    resp = b'<ns:r xmlns:ns="urn:x"><EsitoRicezione>ER01</EsitoRicezione></ns:r>'
    assert parse_notificaesito_response(resp) == "ER01"


def test_parse_notificaesito_response_falls_back_to_local_names() -> None:
    # If AdE revises the WSDL and the ack element is renamed, the parser
    # should still match common alternatives by local name (Esito, Ack).
    assert parse_notificaesito_response(b"<r><Ack>OK</Ack></r>") == "OK"


def test_parse_notificaesito_response_raises_on_unknown_shape() -> None:
    with pytest.raises(ValueError):
        parse_notificaesito_response(b"<r><Whatever>1</Whatever></r>")


def _write_ephemeral_client_material(tmp_path: object) -> tuple[str, str, str]:
    """Write a throwaway self-signed cert+key pair plus a CA file so
    ``_mtls_ssl_context`` can be built without network or real AdE material.
    Returns (client_cert_path, client_key_path, ca_bundle_path)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "flow-sdi-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_file = tmp_path / "client.crt"  # type: ignore[attr-defined]
    key_file = tmp_path / "client.key"  # type: ignore[attr-defined]
    ca_file = tmp_path / "ca.pem"  # type: ignore[attr-defined]
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    ca_file.write_bytes(cert_pem)  # any valid CA PEM is fine for the flag test
    return str(cert_file), str(key_file), str(ca_file)


def test_mtls_context_enables_partial_chain_with_curated_bundle(tmp_path: object) -> None:
    # PARTIAL_CHAIN is what lets the AdE-supplied Sectigo R46+R36 (a
    # cross-signed, non-self-signed root) verify the prod servizi.fatturapa.it
    # leaf on its own. Without it the handshake to production would fail.
    cert, key, ca = _write_ephemeral_client_material(tmp_path)
    ctx = _mtls_ssl_context(client_cert=cert, client_key=key, ca_bundle=ca)
    assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_mtls_context_without_bundle_keeps_default_flags(tmp_path: object) -> None:
    # The system-store path (no curated bundle) keeps stock validation: we do
    # not relax anchor handling for the public PKI.
    cert, key, _ = _write_ephemeral_client_material(tmp_path)
    ctx = _mtls_ssl_context(client_cert=cert, client_key=key, ca_bundle=None)
    assert not (ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
