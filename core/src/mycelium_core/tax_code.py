"""Codice fiscale normalization and validation for FatturaPA ``CodiceFiscale``.

The sibling of :mod:`mycelium_core.vat`, and it exists for the same reason:
a fiscal identifier that cannot be valid must be refused where it is
WRITTEN, not discovered by the XSD when a document is built from it days
later. Before this module nothing checked a codice fiscale at all, and a
``0`` typed into a Stripe customer's metadata to mean "none" travelled into
the client record, satisfied the "VAT number or tax code" gate because it is
not empty, and surfaced only as an XSD pattern failure on the invoice.

WHAT IS CHECKED depends on the counterpart's country. For an Italian one
the field holds a real codice fiscale, in one of two forms:

- 16 characters, for a natural person: the structure (six letters, the
  encoded birth date and place, with omocodia substitutions allowed in the
  digit positions) and the check character;
- 11 digits, for a legal person or a provisional one for a natural person:
  the same check digit as a partita IVA.

For a FOREIGN counterpart the same element may carry the foreign identifier:
AdE FAQ 63 (19 July 2019) tells a foreign final consumer's invoice to repeat
the ``IdCodice`` value in ``CodiceFiscale``. Only the XSD's own facet,
``[A-Z0-9]{11,16}``, binds that value, so it is all that is checked. The
country is the counterpart's ADDRESS country, falling back to the VAT
country, and an unknown one counts as Italian, the case the check exists for.

WHAT IT DOES NOT COVER. A well-formed code with a correct check character
can still not exist, or belong to somebody else; only the Anagrafe
Tributaria knows, and SdI asks it. This catches the typing errors and the
placeholders, not identity.
"""

from __future__ import annotations

import re

_PERSON = re.compile(
    r"[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]"
)
_NUMERIC = re.compile(r"[0-9]{11}")
#: ``CodiceFiscaleType`` in the FatturaPA 1.2.3 schema.
_XSD = re.compile(r"[A-Z0-9]{11,16}")

# The value of a character in an ODD position (1-based) of a 16-character
# code, from the Ministerial Decree of 23 December 1976. Digits share the
# values of the letters in the same rank (0 with A, 1 with B, ...).
_ODD = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        map(int, "1 0 5 7 9 13 15 17 19 21 2 4 18 20 11 3 6 8 12 14 16 10 22 25 24 23".split()),
        strict=True,
    )
)


def tax_code_country(address_country: str | None, vat_country: str | None) -> str | None:
    """The country a counterpart's codice fiscale is judged by: the address
    country, else the VAT country. One rule for every caller, so the record
    and the document cannot disagree about whether a code is acceptable."""
    return address_country or vat_country


def normalize_tax_code(code: str | None) -> str | None:
    """Strip every whitespace character and upper-case; empty becomes None."""
    if code is None:
        return None
    cleaned = "".join(code.split()).upper()
    return cleaned or None


def _person_check_char(body: str) -> str:
    total = 0
    for index, ch in enumerate(body):
        rank = ord(ch) - ord("0") if ch.isdigit() else ord(ch) - ord("A")
        if index % 2 == 0:  # 1-based odd position
            total += _ODD[chr(ord("A") + rank)]
        else:
            total += rank
    return chr(ord("A") + total % 26)


def _numeric_check_digit(body: str) -> int:
    total = 0
    for index, ch in enumerate(body):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10


def is_valid_tax_code(code: str | None, country: str | None = None) -> bool:
    """Validate a NORMALIZED code (see :func:`normalize_tax_code`) for a
    counterpart in ``country`` (ISO-2; None means Italian).

    Empty is accepted: presence is enforced separately, by the invoice
    validation and the connector's completeness gate, exactly as for
    :func:`mycelium_core.vat.is_valid_vat_code`.
    """
    if not code:
        return True
    if (country or "IT").upper() != "IT":
        return _XSD.fullmatch(code) is not None
    if _PERSON.fullmatch(code):
        return _person_check_char(code[:15]) == code[15]
    if _NUMERIC.fullmatch(code):
        return _numeric_check_digit(code[:10]) == int(code[10])
    return False
