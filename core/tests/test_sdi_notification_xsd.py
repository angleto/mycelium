"""Official MessaggiTypes_v1.1 XSD validation for SdI active-cycle notifications
(RC/MC/NS/AT) -- the schema gate that ``sdi_inbound.parse_notification`` runs
on every payload before XPath extraction.

The schema-on-disk requires ``ds:Signature``; the validator relaxes that in
memory (signature verification is a separate, post-v1 concern). What these
tests cover is the *business* shape of the payload, plus the namespace and
root-element guards.
"""

from __future__ import annotations

import pytest

from flow_core.services.sdi_notification_xsd import (
    NS_MESSAGGI,
    V1_NOTIFICATION_ROOTS,
    validate_sdi_notification,
)


def _rc(ident: str = "123456789012") -> str:
    return (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>IT01234567890_00001.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<DataOraConsegna>2026-05-25T10:01:00</DataOraConsegna>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>MID00001</MessageId>"
        f"</m:RicevutaConsegna>"
    )


def _ns(ident: str = "111") -> str:
    return (
        f'<m:NotificaScarto xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<ListaErrori><Errore><Codice>00001</Codice>"
        f"<Descrizione>boom</Descrizione></Errore></ListaErrori>"
        f"<MessageId>M</MessageId>"
        f"</m:NotificaScarto>"
    )


def _mc(ident: str = "222") -> str:
    return (
        f'<m:NotificaMancataConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<MessageId>M</MessageId>"
        f"</m:NotificaMancataConsegna>"
    )


def _at(ident: str = "333") -> str:
    return (
        f'<m:AttestazioneTrasmissioneFattura xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>{ident}</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"<HashFileOriginale>abc</HashFileOriginale>"
        f"</m:AttestazioneTrasmissioneFattura>"
    )


@pytest.mark.parametrize("payload", [_rc(), _ns(), _mc(), _at()])
def test_valid_v1_notifications_pass(payload: str) -> None:
    assert validate_sdi_notification(payload) == []


def test_v1_root_set_matches_active_cycle_only() -> None:
    # Guard against accidental inclusion of v2 elements (NE/DT/EC/SE/MT) that
    # the inbound pipeline does not yet handle.
    assert V1_NOTIFICATION_ROOTS == {
        "RicevutaConsegna",
        "NotificaScarto",
        "NotificaMancataConsegna",
        "AttestazioneTrasmissioneFattura",
    }


def test_wrong_namespace_is_rejected() -> None:
    bad = (
        '<m:RicevutaConsegna xmlns:m="urn:x">'
        "<IdentificativoSdI>1</IdentificativoSdI></m:RicevutaConsegna>"
    )
    errors = validate_sdi_notification(bad)
    assert errors and "not the official SdI messaggi namespace" in errors[0]


def test_missing_namespace_is_rejected() -> None:
    bare = "<RicevutaConsegna><IdentificativoSdI>1</IdentificativoSdI></RicevutaConsegna>"
    errors = validate_sdi_notification(bare)
    assert errors and "not the official SdI messaggi namespace" in errors[0]


def test_unknown_root_is_rejected_with_namespace() -> None:
    foo = f'<m:Foo xmlns:m="{NS_MESSAGGI}"/>'
    errors = validate_sdi_notification(foo)
    assert errors and "not a supported v1 notification" in errors[0]


def test_v2_root_is_rejected_even_if_in_schema() -> None:
    # MetadatiInvioFile is declared in the schema but is part of the receiver
    # (passive) cycle, which is post-v1 -- it must not slip through.
    mt = (
        f'<m:MetadatiInvioFile xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>1</IdentificativoSdI><NomeFile>x</NomeFile>"
        f"<CodiceDestinatario>ABCDEFG</CodiceDestinatario><Formato>FPR12</Formato>"
        f"<TentativiInvio>1</TentativiInvio><MessageId>M</MessageId>"
        f"</m:MetadatiInvioFile>"
    )
    errors = validate_sdi_notification(mt)
    assert errors and "not a supported v1 notification" in errors[0]


def test_missing_required_field_is_rejected() -> None:
    # RC without DataOraConsegna must fail XSD validation.
    incomplete = (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" versione="1.0">'
        f"<IdentificativoSdI>1</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"</m:RicevutaConsegna>"
    )
    errors = validate_sdi_notification(incomplete)
    assert errors and any("DataOraConsegna" in e for e in errors)


def test_malformed_xml_is_rejected_cleanly() -> None:
    errors = validate_sdi_notification(b"not xml")
    assert errors == [e for e in errors if "not well-formed" in e.lower()]
    assert errors


def test_real_payload_with_signature_passes() -> None:
    # The wire payload from SdI is XAdES-signed: a Signature placeholder must
    # not break validation (it is allowed by the relaxed schema, not stripped).
    with_sig = (
        f'<m:RicevutaConsegna xmlns:m="{NS_MESSAGGI}" '
        f'xmlns:ds="http://www.w3.org/2000/09/xmldsig#" versione="1.0">'
        f"<IdentificativoSdI>1</IdentificativoSdI>"
        f"<NomeFile>x.xml</NomeFile>"
        f"<DataOraRicezione>2026-05-25T10:00:00</DataOraRicezione>"
        f"<DataOraConsegna>2026-05-25T10:01:00</DataOraConsegna>"
        f"<Destinatario><Codice>ABCDEFG</Codice><Descrizione>A</Descrizione></Destinatario>"
        f"<MessageId>M</MessageId>"
        f"<ds:Signature>"
        f"<ds:SignedInfo>"
        f'<ds:CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>'
        f'<ds:SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"/>'
        f'<ds:Reference URI=""><ds:DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>'
        f"<ds:DigestValue>YWJjZGVmZ2g=</ds:DigestValue></ds:Reference>"
        f"</ds:SignedInfo>"
        f"<ds:SignatureValue>YWJjZGVmZ2g=</ds:SignatureValue>"
        f"</ds:Signature>"
        f"</m:RicevutaConsegna>"
    )
    assert validate_sdi_notification(with_sig) == []
