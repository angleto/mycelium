"""SdI channel abstraction (docs/adr/0011, FR-9).

A single shared accredited channel transmits invoices whose tenant identity
is in the FatturaPA payload, never the TLS identity (ADR-0011: Flow is an
intermediary under a per-issuer ``SdiMandate``). The channel is selected by
``FLOW_SDI_CHANNEL``:

- ``ManualExportChannel`` (default): the XML is downloadable and legally
  already issued, but it did NOT pass through SdI, so AdE free conservation
  does NOT cover it (ADR-0010): conservation = out_of_coverage. No
  intermediary block in the payload (the cedente is its own trasmittente).
- ``SdICoopChannel`` (``sdicoop``): SdI assigns an ``IdentificativoSdI``;
  conservation becomes AdE-pending then covered once receipts arrive. Flow
  transmits as intermediary, so its ``IntermediaryIdentity`` is stamped into
  ``IdTrasmittente`` + ``TerzoIntermediarioOSoggettoEmittente`` and a
  per-issuer ``SdiMandate`` is required (enforced in ``invoice.transmit``).
  The real mutual-TLS SOAP transport lands in
  ``flow_core.services.sdi_transport`` (F7b); the test-suite injects a fake
  via ``set_channel_override``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from flow_core.config import get_settings
from flow_core.models.invoice import ConservationStatus


@dataclass(frozen=True)
class IntermediaryIdentity:
    """Flow's accredited-channel identity, stamped into the FatturaPA
    payload when Flow transmits on a tenant's behalf (ADR-0011)."""

    id_paese: str
    id_codice: str
    denominazione: str


@dataclass(frozen=True)
class TransmitResult:
    identificativo_sdi: str | None
    conservation: ConservationStatus
    channel: str


class SdiChannel(Protocol):
    """The transmission seam. ``intermediary`` is None for self-submission
    (manual export) and the channel identity when Flow transmits as
    intermediary (which then requires a per-issuer ``SdiMandate``)."""

    name: str

    @property
    def intermediary(self) -> IntermediaryIdentity | None: ...

    def transmit(self, *, xml: str, invoice_id: str) -> TransmitResult: ...


class ManualExportChannel:
    name = "manual_export"

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return None

    def transmit(self, *, xml: str, invoice_id: str) -> TransmitResult:
        # Legally issued but outside AdE free conservation (ADR-0010).
        return TransmitResult(
            identificativo_sdi=None,
            conservation=ConservationStatus.out_of_coverage,
            channel=self.name,
        )


class SdICoopChannel:
    """Production transport (mutual-TLS SOAP RiceviFile) requires F7c
    accreditation (ADR-0011). Bound to the configured intermediary identity
    + transport settings; the live send lands in
    ``services.sdi_transport`` (F7b) and is never exercised in CI (the
    test-suite injects a fake)."""

    name = "sdicoop"

    def __init__(
        self,
        *,
        intermediary: IntermediaryIdentity,
        endpoint_url: str,
        client_cert: str,
        client_key: str,
        ca_bundle: str | None = None,
    ) -> None:
        self._intermediary = intermediary
        self._endpoint_url = endpoint_url
        self._client_cert = client_cert
        self._client_key = client_key
        self._ca_bundle = ca_bundle

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return self._intermediary

    def transmit(self, *, xml: str, invoice_id: str) -> TransmitResult:  # pragma: no cover
        raise NotImplementedError(
            "SdICoop mutual-TLS SOAP transport lands in F7b "
            "(flow_core.services.sdi_transport); configure once accredited"
        )


_FactoryFn = Callable[[], "SdiChannel"]
_override: _FactoryFn | None = None


def set_channel_override(fn: _FactoryFn | None) -> None:
    """Test seam: inject a deterministic channel (e.g. a fake SdICoop).
    Production leaves this None and selects via FLOW_SDI_CHANNEL."""
    global _override
    _override = fn


def get_channel() -> SdiChannel:
    if _override is not None:
        return _override()
    s = get_settings()
    if s.sdicoop_active:
        return SdICoopChannel(
            intermediary=IntermediaryIdentity(
                id_paese=s.sdi_intermediary_id_paese,
                id_codice=s.sdi_intermediary_id_codice,
                denominazione=s.sdi_intermediary_denominazione,
            ),
            endpoint_url=s.sdi_endpoint_url,
            client_cert=s.sdi_client_cert,
            client_key=s.sdi_client_key,
            ca_bundle=s.sdi_ca_bundle or None,
        )
    return ManualExportChannel()
