"""Per-client invoice date format (courtesy PDF only).

A closed set of pattern tokens -> ``strftime`` formats. The client's
``invoice_date_format`` chooses how the issue date and due date are
printed on the courtesy A4 PDF (an Italian customer expects
``28-06-2026``, a US one ``2026-06-28``). NULL/unknown falls back to ISO
(``YYYY-MM-DD``), the historical behaviour, so existing clients are
unchanged. The FatturaPA XML is never affected: SdI dates have their own
fixed format in ``invoice_format``.

The set is closed (not a free ``strftime`` string) so a stored value can
never inject an arbitrary format directive; ``validate_date_format``
rejects anything outside it at the service boundary.
"""

from __future__ import annotations

import datetime as dt

from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode

# Pattern token (what the user picks / what we store) -> strftime format.
INVOICE_DATE_FORMATS: dict[str, str] = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD.MM.YYYY": "%d.%m.%Y",
}

DEFAULT_DATE_FORMAT = "YYYY-MM-DD"


def validate_date_format(fmt: str | None) -> str | None:
    """Normalize + validate a stored/incoming date-format token. Empty
    string is treated as NULL (the UI's "default" option). Raises
    ``DomainError`` for any token outside the closed set."""
    if fmt is None:
        return None
    fmt = fmt.strip()
    if not fmt:
        return None
    if fmt not in INVOICE_DATE_FORMATS:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"invoice_date_format '{fmt}'")
    return fmt


def format_date(d: dt.date, fmt: str | None) -> str:
    """Format ``d`` with the client's token, ISO for NULL/unknown."""
    return d.strftime(INVOICE_DATE_FORMATS.get(fmt or DEFAULT_DATE_FORMAT, "%Y-%m-%d"))
