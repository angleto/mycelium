"""SdI payment-method whitelists (CondizioniPagamento / ModalitaPagamento)
and resolver.

The two FatturaPA fields are constrained to closed enumerations by the
official XSD (see Specifiche Tecniche FatturaPA 1.2): an out-of-table
value is rejected by SdI (scarto). The maps below are the authoritative
source for both validation and the UI dropdown so the two never drift.

The resolver is the only entry point used by the XML build: it returns
the effective (CondizioniPagamento, ModalitaPagamento, GiorniTerminiPagamento)
triple, applying precedence ``invoice > client > issuer > system default``.
NULL/empty at one level falls through to the next; ``system default`` is
TP02 (pagamento completo) + MP05 (bonifico), historically hardcoded in
``invoice_format._build_xml`` and kept here for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import Invoice, IssuerProfile

# CondizioniPagamento (FatturaPA 1.2: TP01..TP03).
CONDIZIONI_PAGAMENTO: dict[str, str] = {
    "TP01": "pagamento a rate",
    "TP02": "pagamento completo",
    "TP03": "anticipo",
}

# ModalitaPagamento (FatturaPA 1.2: MP01..MP23). The label is the SdI
# short description; we keep both code and label so the UI does not need
# its own translation table.
MODALITA_PAGAMENTO: dict[str, str] = {
    "MP01": "contanti",
    "MP02": "assegno",
    "MP03": "assegno circolare",
    "MP04": "contanti presso Tesoreria",
    "MP05": "bonifico",
    "MP06": "vaglia cambiario",
    "MP07": "bollettino bancario",
    "MP08": "carta di pagamento",
    "MP09": "RID",
    "MP10": "RID utenze",
    "MP11": "RID veloce",
    "MP12": "RIBA",
    "MP13": "MAV",
    "MP14": "quietanza erario stato",
    "MP15": "giroconto su conti di contabilita' speciale",
    "MP16": "domiciliazione bancaria",
    "MP17": "domiciliazione postale",
    "MP18": "bollettino di c/c postale",
    "MP19": "SEPA Direct Debit",
    "MP20": "SEPA Direct Debit CORE",
    "MP21": "SEPA Direct Debit B2B",
    "MP22": "trattenuta su somme gia' riscosse",
    "MP23": "PagoPA",
}

DEFAULT_CONDIZIONI = "TP02"
DEFAULT_MODALITA = "MP05"


def validate_condizioni(code: str | None) -> str | None:
    """Reject an unknown CondizioniPagamento code with a clean domain
    error before it can reach the XML build. NULL passes through (means
    "inherit from the next level")."""
    if code is None or code == "":
        return None
    if code not in CONDIZIONI_PAGAMENTO:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"payment_conditions_code '{code}'")
    return code


def validate_modalita(code: str | None) -> str | None:
    if code is None or code == "":
        return None
    if code not in MODALITA_PAGAMENTO:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"payment_method_code '{code}'")
    return code


def validate_terms_days(days: int | None) -> int | None:
    """GiorniTerminiPagamento is XSD ``xs:integer`` with no upper bound,
    but a negative or excessive value is almost certainly a typo. Allow
    [0, 365] (one year covers every realistic net-term scenario)."""
    if days is None:
        return None
    if days < 0 or days > 365:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"payment_terms_days '{days}'")
    return days


@dataclass(frozen=True)
class ResolvedPayment:
    """The values that will actually land in the XML.

    ``terms_days`` is None when no level set it; ``DataScadenzaPagamento``
    is still computed elsewhere (it may come from an explicit
    ``payment_due_date`` even when ``terms_days`` is unset)."""

    condizioni: str
    modalita: str
    terms_days: int | None


def resolve_payment(
    inv: Invoice,
    client: ClientProfile | None,
    issuer: IssuerProfile | None,
) -> ResolvedPayment:
    """Apply invoice > client > issuer > system-default precedence,
    independently per field. An empty string is treated like NULL (the
    UI may send ``""`` to mean "clear this override")."""

    def _pick(*values: str | None, default: str) -> str:
        for v in values:
            if v:
                return v
        return default

    condizioni = _pick(
        inv.payment_conditions_code,
        client.default_payment_conditions_code if client is not None else None,
        issuer.default_payment_conditions_code if issuer is not None else None,
        default=DEFAULT_CONDIZIONI,
    )
    modalita = _pick(
        inv.payment_method_code,
        client.default_payment_method_code if client is not None else None,
        issuer.default_payment_method_code if issuer is not None else None,
        default=DEFAULT_MODALITA,
    )
    terms_days: int | None = (
        inv.payment_terms_days
        if inv.payment_terms_days is not None
        else (
            client.default_payment_terms_days
            if client is not None and client.default_payment_terms_days is not None
            else (issuer.default_payment_terms_days if issuer is not None else None)
        )
    )
    return ResolvedPayment(condizioni=condizioni, modalita=modalita, terms_days=terms_days)


__all__ = [
    "CONDIZIONI_PAGAMENTO",
    "DEFAULT_CONDIZIONI",
    "DEFAULT_MODALITA",
    "MODALITA_PAGAMENTO",
    "ResolvedPayment",
    "resolve_payment",
    "validate_condizioni",
    "validate_modalita",
    "validate_terms_days",
]
