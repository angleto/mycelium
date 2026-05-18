"""SdI channel abstraction (docs/adr/0011, FR-9).

A single shared accredited channel transmits invoices whose tenant
identity is in the FatturaPA payload, never the TLS identity (ADR-0011:
Flow is an intermediary under per-Org mandate). Same Protocol +
injectable factory seam as the other providers.

- ``ManualExportChannel`` (F7a, default): the XML is downloadable and
  legally already issued, but it did NOT pass through SdI, so AdE free
  conservation does NOT cover it (ADR-0010): conservation =
  out_of_coverage.
- ``SdICoopChannel`` (F7b test / F7c production): SdI assigns an
  ``IdentificativoSdI``; conservation becomes AdE-pending then covered
  once receipts arrive. F7c (service agreement + accreditation +
  always-on inbound mutual-TLS SOAP endpoint) is the heavy external
  step (ADR-0011); the test seam below models the deterministic
  correlation, not the transport.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from flow_core.models.invoice import ConservationStatus


@dataclass(frozen=True)
class TransmitResult:
    identificativo_sdi: str | None
    conservation: ConservationStatus
    channel: str


class ManualExportChannel:
    name = "manual_export"

    def transmit(self, *, xml: str, invoice_id: str) -> TransmitResult:
        # Legally issued but outside AdE free conservation (ADR-0010).
        return TransmitResult(
            identificativo_sdi=None,
            conservation=ConservationStatus.out_of_coverage,
            channel=self.name,
        )


class SdICoopChannel:
    """Production transport requires F7c accreditation (ADR-0011); not
    exercised in CI. The deterministic correlation it enables (an
    IdentificativoSdI, then push receipts) is what the service and
    tests exercise via the injectable seam."""

    name = "sdicoop"

    def transmit(self, *, xml: str, invoice_id: str) -> TransmitResult:  # pragma: no cover
        ident = "SDI" + hashlib.sha256(invoice_id.encode()).hexdigest()[:12].upper()
        return TransmitResult(
            identificativo_sdi=ident,
            conservation=ConservationStatus.ade_pending,
            channel=self.name,
        )


SdiChannel = ManualExportChannel | SdICoopChannel

_FactoryFn = Callable[[], "SdiChannel"]
_override: _FactoryFn | None = None


def set_channel_override(fn: _FactoryFn | None) -> None:
    """Test seam: inject a deterministic channel (e.g. a fake SdICoop).
    Production leaves this None (manual export until F7c)."""
    global _override
    _override = fn


def get_channel() -> SdiChannel:
    if _override is not None:
        return _override()
    return ManualExportChannel()
