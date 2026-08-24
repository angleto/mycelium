"""SdI channel abstraction (docs/adr/0011, FR-9).

A single shared accredited channel transmits invoices whose tenant identity
is in the FatturaPA payload, never the TLS identity (ADR-0011: Mycelium is an
intermediary under a per-issuer ``SdiMandate``). The channel is selected by
``MYCELIUM_SDI_CHANNEL``:

- ``ManualExportChannel`` (default): the XML is downloadable and legally
  already issued, but it did NOT pass through SdI, so AdE free conservation
  does NOT cover it (ADR-0010): conservation = out_of_coverage. The cedente is
  its own trasmittente.
- ``SdICoopChannel`` (``sdicoop``): SdI assigns an ``IdentificativoSdI``;
  conservation becomes AdE-pending then covered once receipts arrive. Mycelium
  transmits for the tenant, so its ``IntermediaryIdentity`` is stamped into
  ``IdTrasmittente`` and a per-issuer ``SdiMandate`` is required (enforced in
  ``invoice.transmit``). Nothing of Mycelium's appears in the document body:
  the mandate covers transmission, not issuance (ADR-0053).
  The live mutual-TLS SOAP send is in ``services.sdi_transport`` (F7b) and is
  never exercised in CI (the test-suite injects a fake).

``transmit`` is async: the real send is network I/O and must not block the
event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mycelium_core.config import get_settings
from mycelium_core.models.invoice import ConservationStatus


@dataclass(frozen=True)
class IntermediaryIdentity:
    """Mycelium's accredited-channel identity: the soggetto trasmittente.

    It reaches the wire in exactly two places, both about transmission and
    neither about the document's content -- FatturaPA 1.1.1 ``IdTrasmittente``
    and the NomeFile / ProgressivoInvio sequence (ADR-0011, ADR-0053). There is
    deliberately no ``legal_name``: the only element that ever needed one was
    the emitter block, which is not emitted."""

    country_code: str
    vat_number: str


@dataclass(frozen=True)
class TransmitResult:
    identificativo_sdi: str | None
    conservation: ConservationStatus
    channel: str


class SdiChannel(Protocol):
    """The transmission seam. ``intermediary`` is None for self-submission
    (manual export) and the channel identity when Mycelium transmits as
    intermediary (which then requires a per-issuer ``SdiMandate``).
    ``filename`` is the SdI file name (``IT{id}_{progressivo}.xml``)."""

    name: str

    @property
    def intermediary(self) -> IntermediaryIdentity | None: ...

    async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult: ...


class ManualExportChannel:
    name = "manual_export"

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return None

    async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
        # Legally issued but outside AdE free conservation (ADR-0010).
        return TransmitResult(
            identificativo_sdi=None,
            conservation=ConservationStatus.out_of_coverage,
            channel=self.name,
        )


class SdICoopChannel:
    """Production transport (mutual-TLS SOAP RiceviFile) requires F7c
    accreditation (ADR-0011). Bound to the configured intermediary identity
    + transport settings; the live send lives in
    ``services.sdi_transport.send_via_sdicoop`` and is never exercised in CI
    (no accreditation / certificates)."""

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

    async def transmit(
        self, *, xml: str, invoice_id: str, filename: str
    ) -> TransmitResult:  # pragma: no cover
        from mycelium_core.services.sdi_transport import send_via_sdicoop

        identificativo = await send_via_sdicoop(
            xml=xml,
            filename=filename,
            endpoint_url=self._endpoint_url,
            client_cert=self._client_cert,
            client_key=self._client_key,
            ca_bundle=self._ca_bundle,
        )
        return TransmitResult(
            identificativo_sdi=identificativo,
            conservation=ConservationStatus.ade_pending,
            channel=self.name,
        )


_FactoryFn = Callable[[], "SdiChannel"]
_override: _FactoryFn | None = None


def set_channel_override(fn: _FactoryFn | None) -> None:
    """Test seam: inject a deterministic channel (e.g. a fake SdICoop).
    Production leaves this None and selects via MYCELIUM_SDI_CHANNEL."""
    global _override
    _override = fn


def get_channel(
    endpoint_override: str | None = None, id_codice_override: str | None = None
) -> SdiChannel:
    """The configured transmission channel.

    Both overrides carry a value the caller resolved from ``system_settings``,
    which this function cannot read: it is sync and holds no session, while the
    settings live in the database precisely so an operator can change them
    without a redeploy. ``endpoint_override`` is the test<->production switch;
    ``id_codice_override`` is the accredited channel's fiscal code. When either
    is None the env value stands, which is what keeps a deployment that has not
    been touched working exactly as before."""
    if _override is not None:
        return _override()
    s = get_settings()
    if s.sdicoop_active:
        return SdICoopChannel(
            intermediary=IntermediaryIdentity(
                country_code=s.sdi_intermediary_id_paese,
                vat_number=id_codice_override or s.sdi_intermediary_id_codice,
            ),
            endpoint_url=endpoint_override or s.sdi_endpoint_url,
            client_cert=s.sdi_client_cert,
            client_key=s.sdi_client_key,
            ca_bundle=s.sdi_ca_bundle or None,
        )
    return ManualExportChannel()
