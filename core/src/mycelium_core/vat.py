"""VAT identifier (P.IVA) normalization for FatturaPA ``IdFiscaleIVA``.

FatturaPA splits the VAT id into ``IdPaese`` (country) + ``IdCodice`` (the bare
number, NO country prefix). Users naturally type the VIES form
``IT01112223334``; stored/emitted verbatim that puts ``IT01112223334`` in
``IdCodice`` -- the XSD's ``String28`` accepts it, but SdI scarta it. So at save
we accept both the prefixed and bare forms and normalize: the leading 2-letter
country prefix goes to ``IdPaese``, the bare digits to ``IdCodice``.
"""

from __future__ import annotations


def normalize_vat(code: str | None, country: str | None) -> tuple[str | None, str | None]:
    """Split a possibly VIES-prefixed VAT id into ``(country, bare_code)``.

    Strips spaces and dots; a leading 2-letter alphabetic prefix (followed by
    the number) becomes the country (overriding ``country``) and is removed
    from the code. Empty/None ``code`` is returned unchanged."""
    if not code:
        return country, code
    cleaned = "".join(code.split()).replace(".", "")
    if len(cleaned) > 2 and cleaned[:2].isalpha():
        return cleaned[:2].upper(), cleaned[2:]
    return country, cleaned


def is_valid_vat_code(code: str | None, country: str | None) -> bool:
    """Validate the *bare* ``IdCodice`` (after :func:`normalize_vat`). For IT it
    must be exactly 11 digits; foreign formats are not validated here (foreign
    B2B is post-v1). Empty is accepted -- presence is enforced separately by the
    invoice validation, not here."""
    if not code:
        return True
    if (country or "IT").upper() == "IT":
        return len(code) == 11 and code.isdigit()
    return True
