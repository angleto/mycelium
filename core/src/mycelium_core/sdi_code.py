"""Codice destinatario normalization and validation for FatturaPA
``CodiceDestinatario``.

The third sibling of :mod:`mycelium_core.vat` and :mod:`mycelium_core.tax_code`,
for the same reason: a recipient code that cannot be valid must be refused
where it is written. Before this module nothing touched one. A code typed in
lower case on a Stripe customer reached the client record verbatim and failed
the XSD on the built document, and a code typed with eight characters was
CLAMPED to seven on the way in -- which produces a well-formed code that
names a different recipient, the one outcome worse than a refusal. Both were
measured in production on 2026-10-16.

WHAT IS CHECKED is the schema facet ``[A-Z0-9]{6,7}``, after removing
whitespace and upper-casing: six characters for a public administration's
codice univoco ufficio, seven for everybody else (``0000000`` and the foreign
``XXXXXXX`` included). Upper-casing is not a guess: the facet admits no lower
case, and the codes SdI issues are upper-case alphanumerics.

WHAT IT DOES NOT COVER. Whether the code exists, and whether it belongs to
this counterpart; only SdI knows, and a wrong but well-formed code is
delivered to whoever owns it.
"""

from __future__ import annotations

import re

#: ``CodiceDestinatarioType`` in the FatturaPA 1.2.3 schema.
_XSD = re.compile(r"[A-Z0-9]{6,7}")


def normalize_sdi_code(code: str | None) -> str | None:
    """Strip every whitespace character and upper-case; empty becomes None."""
    if code is None:
        return None
    cleaned = "".join(code.split()).upper()
    return cleaned or None


def is_valid_sdi_code(code: str | None) -> bool:
    """Validate a NORMALIZED code (see :func:`normalize_sdi_code`). Empty is
    accepted: whether a recipient is addressable is decided separately, by the
    "codice destinatario or PEC" gate."""
    if not code:
        return True
    return _XSD.fullmatch(code) is not None
