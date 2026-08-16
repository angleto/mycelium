"""Payment-connector service tests (ADR-0051).

Covers the paths that decide whether the subsystem is safe to switch on:

- ingest is idempotent under provider redelivery (UNIQUE + ON CONFLICT);
- an emission event composes and transmits ONE invoice, and a second event
  naming the same money resolves to it instead of filing a second document;
- a counterpart without fiscal data is QUARANTINED, not silently emitted or
  retried forever;
- a refund produces a TD04 against the right parent, full and partial;
- the credit-note automation switch is honoured;
- ``charge.succeeded`` reconciles payment state without minting anything.

The default SdI channel is ``ManualExportChannel``, so a successful transmit
lands on ``state=transmitted`` with ``identificativo_sdi`` NULL and the dispatch
lease cleared. That is the settled shape here, not an unsettled one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, InvoiceState, PaymentStatus
from mycelium_core.models.membership import Role
from mycelium_core.models.payment_connector import (
    PaymentConnectorEvent,
    PaymentCustomerLink,
    PaymentObjectLink,
)
from mycelium_core.models.tag import TagKind
from mycelium_core.services import invoice as inv_svc
from mycelium_core.services import payment_connectors as svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _org_and_issuer() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PC",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        profile = await inv_svc.create_issuer_profile(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            label="Principale",
            legal_name="HahnBanach SRL",
            vat_number="01234567890",
            address="Via Roma",
            civic_number="1",
            postal_code="00100",
            city="Roma",
            province="RM",
            is_default=True,
        )
        issuer_id = profile.id
    return r.org_id, r.user_id, issuer_id


async def _connector(
    org_id: uuid.UUID, user_id: uuid.UUID, issuer_id: uuid.UUID, **fields: Any
) -> uuid.UUID:
    """A connector configured the way a real Stripe integration has to be."""
    async with tenant_session(str(org_id), str(user_id)) as s:
        row, _secret, _key = await svc.create_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            issuer_profile_id=issuer_id,
            label=f"stripe-{uuid.uuid4().hex[:6]}",
            # A vendor adapter cannot be handed a secret we invented: the
            # provider issues it, so the service refuses to mint one.
            signing_secret=f"whsec_test_{uuid.uuid4().hex}",
            enabled=True,
            **fields,
        )
        return row.id


async def _run(org_id: uuid.UUID, connector_id: uuid.UUID, event_id: uuid.UUID) -> str:
    """Process one event the way the worker does: connector as the actor, role
    pinned so ``require_role`` authorizes a principal with no membership."""
    async with tenant_session(
        str(org_id),
        str(connector_id),
        actor_kind="payment_connector",
        actor_subject_id=str(connector_id),
    ) as s:
        await s.execute(
            text("SELECT set_config('app.current_role', :r, true)"), {"r": Role.member.value}
        )
        return await svc.process_event(s, org_id=org_id, event_id=event_id)


def _customer() -> dict[str, Any]:
    return {
        "name": "Acme SpA",
        "email": "amministrazione@acme.test",
        "address": {
            "line1": "Via Milano 9",
            "postal_code": "20100",
            "city": "Milano",
            "state": "MI",
            "country": "IT",
        },
    }


def _invoice_paid(
    *,
    event_id: str = "evt_1",
    invoice_id: str = "in_1",
    charge_id: str = "ch_1",
    amount: int = 10000,
    vat_amount: int = 2200,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    cust = _customer()
    return {
        "id": event_id,
        "type": "invoice.paid",
        "created": 1_755_000_000,
        "data": {
            "object": {
                "id": invoice_id,
                "object": "invoice",
                "currency": "eur",
                "customer": "cus_1",
                "customer_name": cust["name"],
                "customer_email": cust["email"],
                "customer_address": cust["address"],
                "charge": charge_id,
                "payment_intent": "pi_1",
                "description": "Abbonamento marzo",
                # A real codice destinatario: 0000000 cannot be used to send, so a
                # counterpart is only addressable with a real code or a PEC.
                "metadata": (
                    {"vat_number": "IT09876543210", "sdi_code": "ABCDEFG"}
                    if metadata is None
                    else metadata
                ),
                "total": amount + vat_amount,
                "lines": {
                    "data": [
                        {
                            "description": "Piano Pro",
                            "quantity": 1,
                            "amount": amount,
                            "tax_amounts": [
                                {
                                    "amount": vat_amount,
                                    "inclusive": False,
                                    "tax_rate": {"percentage": 22.0},
                                }
                            ],
                        }
                    ]
                },
            }
        },
    }


async def _ingest(
    org_id: uuid.UUID, connector_id: uuid.UUID, payload: dict[str, Any]
) -> tuple[bool, uuid.UUID | None]:
    async with tenant_session(str(org_id), str(connector_id), actor_kind="payment_connector") as s:
        created = await svc.ingest(
            s,
            org_id=org_id,
            connector_id=connector_id,
            provider_event_id=str(payload["id"]),
            event_type=str(payload["type"]),
            payload=payload,
            occurred_at=None,
        )
        row = (
            await s.execute(
                select(PaymentConnectorEvent.id).where(
                    PaymentConnectorEvent.connector_id == connector_id,
                    PaymentConnectorEvent.provider_event_id == str(payload["id"]),
                )
            )
        ).scalar_one_or_none()
    return created, row


# --- ingest ----------------------------------------------------------------


async def test_ingest_is_idempotent_under_redelivery() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    payload = _invoice_paid()

    first, event_id = await _ingest(org_id, connector_id, payload)
    second, again = await _ingest(org_id, connector_id, payload)

    assert first is True
    assert second is False, "a redelivery must not create a second event row"
    assert event_id == again


# --- emission --------------------------------------------------------------


async def test_invoice_paid_emits_one_transmitted_invoice() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    _created, event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert event_id is not None

    status = await _run(org_id, connector_id, event_id)
    assert status == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.invoice_id is not None
        inv = await inv_svc.get_invoice(s, org_id=org_id, invoice_id=event.invoice_id)
        assert inv.document_type is DocumentType.TD01
        assert inv.state is InvoiceState.transmitted
        assert inv.number is not None, "a transmitted invoice must carry a fiscal number"
        assert inv.payment_status is PaymentStatus.paid
        # 100.00 net + 22% -> 122.00 total, from Stripe's minor units.
        assert inv.taxable == Decimal("100.00")
        assert inv.vat == Decimal("22.00")
        assert inv.total == Decimal("122.00")
        # Every provider id that names this money is claimed.
        links = (
            (
                await s.execute(
                    select(PaymentObjectLink.object_kind, PaymentObjectLink.object_id).where(
                        PaymentObjectLink.connector_id == connector_id
                    )
                )
            )
            .tuples()
            .all()
        )
        assert set(links) == {("invoice", "in_1"), ("charge", "ch_1"), ("payment_intent", "pi_1")}


async def test_second_event_for_the_same_money_does_not_double_invoice() -> None:
    """The core no-double-filing guarantee: a different event id naming an
    already-invoiced charge resolves to the existing document."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c1, first_event = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_a"))
    assert first_event is not None
    await _run(org_id, connector_id, first_event)

    # Same invoice/charge, brand-new provider event id (Stripe's own retry with
    # a regenerated event, or a replayed webhook from the dashboard).
    _c2, second_event = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_b"))
    assert second_event is not None
    status = await _run(org_id, connector_id, second_event)
    assert status == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1, "one payment must never yield two fiscal documents"


async def test_missing_fiscal_data_parks_as_no_billing_data_not_quarantine() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    # No VAT metadata and no tax id: not invoiceable as a FatturaPA.
    payload = _invoice_paid(event_id="evt_bad", metadata={})
    _created, event_id = await _ingest(org_id, connector_id, payload)
    assert event_id is not None

    status = await _run(org_id, connector_id, event_id)
    # NOT `needs_attention`: nothing is broken and there is nothing for an
    # operator to decide -- the customer simply never entered their billing
    # data. Its own state keeps the operator queue readable and lets the event
    # re-arm itself when a customer event finally carries the data.
    assert status == "no_billing_data"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.last_error == "client_billing_data_missing"
        assert event.invoice_id is None
        assert await inv_svc.list_invoices(s, org_id=org_id) == []


async def test_draft_mode_composes_without_transmitting() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="draft")
    _created, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_draft"))
    assert event_id is not None

    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1
        assert invoices[0].state is InvoiceState.draft
        assert invoices[0].number is None, "a draft must not consume a fiscal number"


# --- payment reconciliation ------------------------------------------------


async def test_charge_succeeded_marks_paid_without_emitting() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    # Emit as a DRAFT so the payment event has something unpaid to reconcile.
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="draft")
    _c, emission = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_e"))
    assert emission is not None
    await _run(org_id, connector_id, emission)

    async with tenant_session(str(org_id), str(user_id)) as s:
        inv = (await inv_svc.list_invoices(s, org_id=org_id))[0]
        await inv_svc.transmit(s, org_id=org_id, actor_id=user_id, invoice_id=inv.id)

    charge_event = {
        "id": "evt_charge",
        "type": "charge.succeeded",
        "created": 1_755_000_100,
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_1", "object": "charge"}},
    }
    _c2, event_id = await _ingest(org_id, connector_id, charge_event)
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1, "a payment event must not mint a document"
        assert invoices[0].payment_status is PaymentStatus.paid


async def test_payment_event_for_unknown_money_is_ignored() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    charge_event = {
        "id": "evt_unknown",
        "type": "charge.succeeded",
        "created": 1_755_000_100,
        "data": {"object": {"id": "ch_nope", "object": "charge"}},
    }
    _c, event_id = await _ingest(org_id, connector_id, charge_event)
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "ignored"


# --- credit notes ----------------------------------------------------------


def _refund(*, event_id: str, amount: int, refund_id: str = "re_1") -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "charge.refunded",
        "created": 1_755_000_200,
        "data": {
            "object": {
                "id": "ch_1",
                "object": "charge",
                "currency": "eur",
                "payment_intent": "pi_1",
                "amount_refunded": amount,
                "refunds": {"data": [{"id": refund_id, "amount": amount, "reason": "requested"}]},
            }
        },
    }


async def _emit_parent(org_id: uuid.UUID, connector_id: uuid.UUID) -> None:
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_parent"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"


async def test_full_refund_emits_a_matching_credit_note() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    await _emit_parent(org_id, connector_id)

    _c, event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_ref", amount=12200))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert len(notes) == 1
        assert notes[0].total == Decimal("122.00"), "a full refund reverses the whole document"
        assert notes[0].parent_invoice_id is not None


async def test_partial_refund_scales_the_credit_note() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    await _emit_parent(org_id, connector_id)

    # Half the gross: 61.00 of 122.00.
    _c, event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_part", amount=6100))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert len(notes) == 1
        assert notes[0].taxable == Decimal("50.00")
        assert notes[0].total == Decimal("61.00")


async def test_refund_is_not_reversed_twice() -> None:
    """A dashboard refund fires both charge.refunded and credit_note.created;
    they share the refund id, so whichever lands first claims it."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    await _emit_parent(org_id, connector_id)

    _c1, first = await _ingest(org_id, connector_id, _refund(event_id="evt_r1", amount=12200))
    assert first is not None
    await _run(org_id, connector_id, first)

    credit_note_event = {
        "id": "evt_cn",
        "type": "credit_note.created",
        "created": 1_755_000_300,
        "data": {
            "object": {
                "id": "cn_1",
                "object": "credit_note",
                "currency": "eur",
                "invoice": "in_1",
                "total": 12200,
                "refund": "re_1",
                "reason": "duplicate",
                "lines": {"data": []},
            }
        },
    }
    _c2, second = await _ingest(org_id, connector_id, credit_note_event)
    assert second is not None
    assert await _run(org_id, connector_id, second) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert len(notes) == 1, "one reversal must never yield two credit notes"


async def test_credit_note_mode_off_quarantines_for_manual_handling() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, credit_note_mode="off")
    await _emit_parent(org_id, connector_id)

    _c, event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_man", amount=12200))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "needs_attention"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.last_error == "credit_note_manual"
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert notes == []


async def test_refund_before_its_invoice_retries_instead_of_failing() -> None:
    """Provider ordering is not guaranteed, so an orphan refund is deferred."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c, event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_early", amount=100))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "pending"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.last_error == "parent_not_emitted"
        assert event.attempt_count == 1


# --- the customer record is the single source of fiscal truth ---------------


def _customer_event(
    *,
    event_id: str = "evt_cus",
    customer_id: str = "cus_1",
    metadata: dict[str, str] | None = None,
    address: bool = True,
) -> dict[str, Any]:
    """A Stripe ``customer.updated``, shaped as Stripe really sends it.

    This is the ONLY event that carries the counterpart's fiscal identity: an
    invoice event names its customer by id and a Stripe webhook payload cannot
    be expanded, so without this the data never reaches the invoice.
    """
    return {
        "id": event_id,
        "type": "customer.updated",
        "created": 1_755_000_000,
        "data": {
            "object": {
                "id": customer_id,
                "object": "customer",
                "name": "Acme SpA",
                "email": "amministrazione@acme.test",
                "address": (
                    {
                        "line1": "Via Milano 9",
                        "postal_code": "20100",
                        "city": "Milano",
                        "state": "MI",
                        "country": "IT",
                    }
                    if address
                    else None
                ),
                "metadata": metadata
                if metadata is not None
                else {"vatId": "IT09876543210", "codice_destinatario": "ABCDEFG"},
            }
        },
    }


async def test_customer_event_registers_the_client_and_its_fiscal_data() -> None:
    """The fiscal identity lands in client_profile, the org's real anagrafica,
    not in a connector-owned copy that would be a second truth to reconcile."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c, event_id = await _ingest(org_id, connector_id, _customer_event())
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        link = (
            await s.execute(
                select(PaymentCustomerLink).where(PaymentCustomerLink.connector_id == connector_id)
            )
        ).scalar_one()
        assert link.provider_customer_id == "cus_1"
        profile = (
            await s.execute(select(ClientProfile).where(ClientProfile.tag_id == link.client_tag_id))
        ).scalar_one()
        assert profile.vat_number == "09876543210", "the VIES prefix is split off"
        assert profile.country_code == "IT"
        assert profile.sdi_code == "ABCDEFG"
        assert profile.city == "Milano"


async def test_a_customer_event_never_overwrites_a_curated_value() -> None:
    """An operator's correction outlives whatever the provider says later.

    This is what makes it safe to run the enrichment unattended: the connector
    can only ever fill a hole.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    _c, first = await _ingest(org_id, connector_id, _customer_event())
    assert first is not None
    await _run(org_id, connector_id, first)

    async with tenant_session(str(org_id), str(user_id)) as s:
        link = (
            await s.execute(
                select(PaymentCustomerLink).where(PaymentCustomerLink.connector_id == connector_id)
            )
        ).scalar_one()
        tag_id = link.client_tag_id
        profile = (
            await s.execute(select(ClientProfile).where(ClientProfile.tag_id == tag_id))
        ).scalar_one()
        profile.address = "Via Corretta A Mano 1"  # an operator fixes the address
        await s.flush()

    # The provider re-states the OLD address on a later event.
    _c2, second = await _ingest(org_id, connector_id, _customer_event(event_id="evt_cus2"))
    assert second is not None
    await _run(org_id, connector_id, second)

    async with tenant_session(str(org_id), str(user_id)) as s:
        profile = (
            await s.execute(select(ClientProfile).where(ClientProfile.tag_id == tag_id))
        ).scalar_one()
        assert profile.address == "Via Corretta A Mano 1", "a curated value always wins"


async def test_a_payment_waits_for_billing_data_then_emits_by_itself() -> None:
    """THE scenario a real Stripe integration lives on.

    A customer pays before completing their billing details, so the invoice
    event carries nothing usable. It parks in ``no_billing_data`` -- not in the
    operator queue -- and the moment a customer event arrives with the data the
    payment re-arms ITSELF and the invoice is emitted. Without this the payment
    would sit parked until a human noticed, which for a monthly subscription
    means noticing every month.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    # 1. The payment arrives first, with no fiscal identity anywhere.
    _c, payment = await _ingest(
        org_id, connector_id, _invoice_paid(event_id="evt_early", metadata={})
    )
    assert payment is not None
    assert await _run(org_id, connector_id, payment) == "no_billing_data"

    async with tenant_session(str(org_id), str(user_id)) as s:
        row = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == payment)
            )
        ).scalar_one()
        assert row.last_error == "client_billing_data_missing"
        assert row.provider_customer_id == "cus_1", "parked against the customer it waits on"
        assert await inv_svc.list_invoices(s, org_id=org_id) == []

    # 2. The customer finally fills their billing details in.
    _c2, customer = await _ingest(org_id, connector_id, _customer_event())
    assert customer is not None
    assert await _run(org_id, connector_id, customer) == "done"

    # 3. The payment re-armed itself; no operator touched anything.
    async with tenant_session(str(org_id), str(user_id)) as s:
        row = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == payment)
            )
        ).scalar_one()
        assert row.status == "pending", "the waiting room empties on its own"
        assert row.attempt_count == 0

    assert await _run(org_id, connector_id, payment) == "done"
    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1
        assert invoices[0].state is InvoiceState.transmitted
        assert invoices[0].total == Decimal("122.00")


async def test_a_customer_event_without_fiscal_data_rearms_nothing() -> None:
    """A customer event about something else (a new card, a renamed company)
    must not churn the waiting room into pointless attempts."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    _c, payment = await _ingest(
        org_id, connector_id, _invoice_paid(event_id="evt_early2", metadata={})
    )
    assert payment is not None
    assert await _run(org_id, connector_id, payment) == "no_billing_data"

    _c2, customer = await _ingest(
        org_id, connector_id, _customer_event(metadata={"note": "nessun dato fiscale"})
    )
    assert customer is not None
    await _run(org_id, connector_id, customer)

    async with tenant_session(str(org_id), str(user_id)) as s:
        row = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == payment)
            )
        ).scalar_one()
        assert row.status == "no_billing_data", "still waiting, not retried"


async def test_a_foreign_counterpart_is_addressed_by_the_standard_not_by_a_default() -> None:
    """A counterpart outside Italy has no Italian recipient code by
    construction, and FatturaPA prescribes seven X for exactly that case.

    That is a RULE, not a fallback: it is the only way such an invoice can be
    filed at all, and it must not be confused with ``0000000``, which cannot be
    used to send and therefore never makes a document emittable.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    payload = _invoice_paid(event_id="evt_fr", metadata={"vat_number": "FR12345678901"})
    obj = payload["data"]["object"]  # type: ignore[index]
    obj["customer_address"] = {  # type: ignore[index]
        "line1": "12 Rue de la Paix",
        "postal_code": "75002",
        "city": "Paris",
        "country": "FR",
    }
    _c, event_id = await _ingest(org_id, connector_id, payload)
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1
        profile = (
            await s.execute(
                select(ClientProfile).where(ClientProfile.tag_id == invoices[0].client_tag_id)
            )
        ).scalar_one()
        assert profile.sdi_code == svc.FOREIGN_SDI_CODE == "XXXXXXX"


async def test_an_italian_counterpart_without_a_recipient_code_is_not_emitted() -> None:
    """The rule the operator set: 0000000 cannot be used to send, so an Italian
    counterpart that supplied neither a codice destinatario nor a PEC waits."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    payload = _invoice_paid(event_id="evt_no_dest", metadata={"vat_number": "IT09876543210"})
    _c, event_id = await _ingest(org_id, connector_id, payload)
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "no_billing_data"

    async with tenant_session(str(org_id), str(user_id)) as s:
        row = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert "sdi_code|pec" in (row.error_detail or "")
        assert await inv_svc.list_invoices(s, org_id=org_id) == []


# --- shadow mode -----------------------------------------------------------


async def test_dry_run_builds_a_valid_xml_without_spending_a_number() -> None:
    """The whole point: everything a real emission does, minus the send.

    A `draft` connector would compose and stop, leaving nothing to inspect --
    the XML is only built at transmit. `dry_run` builds and validates it, so a
    parallel run against an incumbent provider has an artefact to diff.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_dry"))
    assert event_id is not None

    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.dry_run is True
        assert event.dry_run_xml is not None
        xml = event.dry_run_xml
        # A real FatturaPA, with the real counterpart and the real amounts.
        assert "<CodiceDestinatario>ABCDEFG</CodiceDestinatario>" in xml
        assert "<ImportoTotaleDocumento>122.00</ImportoTotaleDocumento>" in xml
        # ...and unmistakably not a filed one.
        assert "<ProgressivoInvio>ANTEPRIMA</ProgressivoInvio>" in xml

        inv = await inv_svc.get_invoice(s, org_id=org_id, invoice_id=event.invoice_id)
        assert inv.state is InvoiceState.draft, "a shadow document is never transmitted"
        assert inv.number is None, "a shadow run must not consume a fiscal number"
        assert inv.payment_status is PaymentStatus.unpaid
        assert inv.is_archived is True, "kept out of the list an operator transmits from"


async def test_dry_run_does_not_block_the_real_emission_once_the_flag_comes_off() -> None:
    """The reversibility guarantee, and the reason shadow claims are namespaced.

    If a shadow run claimed the provider ids the way a real one does, switching
    to `transmit` would find the shadow draft, resume it, and file a document
    composed from data the shadow period existed to distrust.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    _c, shadow = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_shadow"))
    assert shadow is not None
    assert await _run(org_id, connector_id, shadow) == "done"

    # The operator is satisfied and switches the connector live.
    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"invoice_mode": "transmit"},
        )

    # The same payment arrives again (a redelivery, or a replay from the
    # dashboard). It must produce a REAL invoice, not resume the shadow one.
    _c2, live = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_live"))
    assert live is not None
    assert await _run(org_id, connector_id, live) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        transmitted = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id, view="active")
            if i.state is InvoiceState.transmitted
        ]
        assert len(transmitted) == 1, "exactly one real document"
        assert transmitted[0].number is not None
        shadowed = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        assert len(shadowed) == 1, "the shadow document survives as evidence"
        assert shadowed[0].id != transmitted[0].id
        assert shadowed[0].number is None


async def test_dry_run_is_idempotent_under_redelivery() -> None:
    """A redelivery during the shadow period must not pile up shadow copies."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    for event_id in ("evt_d1", "evt_d2"):
        _c, eid = await _ingest(org_id, connector_id, _invoice_paid(event_id=event_id))
        assert eid is not None
        assert await _run(org_id, connector_id, eid) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        assert len(await inv_svc.list_invoices(s, org_id=org_id, view="archived")) == 1


async def test_dry_run_surfaces_an_invalid_document_instead_of_retrying_it() -> None:
    """A shadow run exists to find bad data. An invalid province is caught by
    the XSD/validation pass, and parking it names the field -- retrying would
    bury the finding under attempts for a condition that cannot self-resolve."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    payload = _invoice_paid(event_id="evt_badprov")
    obj = payload["data"]["object"]  # type: ignore[index]
    obj["customer_address"] = {  # type: ignore[index]
        "line1": "Via Milano 9",
        "postal_code": "20100",
        "city": "Milano",
        "state": "ZZ",  # not an Italian province
        "country": "IT",
    }
    _c, event_id = await _ingest(org_id, connector_id, payload)
    assert event_id is not None

    assert await _run(org_id, connector_id, event_id) == "needs_attention"
    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.last_error == "dry_run_invalid_document"
        assert event.dry_run is True


async def test_dry_run_refuses_to_pretend_about_credit_notes() -> None:
    """A TD04 corrects an EMITTED document, and in shadow mode nothing is ever
    emitted. Saying so beats validating a fiction."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(
        org_id, user_id, issuer_id, invoice_mode="dry_run", credit_note_mode="dry_run"
    )
    _c, emission = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_dr_par"))
    assert emission is not None
    await _run(org_id, connector_id, emission)

    _c2, refund = await _ingest(org_id, connector_id, _refund(event_id="evt_dr_ref", amount=12200))
    assert refund is not None
    assert await _run(org_id, connector_id, refund) == "needs_attention"

    async with tenant_session(str(org_id), str(user_id)) as s:
        event = (
            await s.execute(select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == refund))
        ).scalar_one()
        assert event.last_error == "dry_run_credit_note_unsupported"


async def test_a_sender_cannot_reach_a_shadow_document_through_its_reference() -> None:
    """The shadow/live separation must not be forgeable by the sender.

    On the native contract the provider object id IS the sender's own
    ``reference``. An earlier cut separated shadow claims by prefixing that id
    with a reserved string, which meant a sender presenting the reserved form
    could make a LIVE run resolve to a shadow document -- and file, at SdI, a
    draft composed during the period whose whole purpose was to distrust it.
    The discriminator is a column the sender cannot express.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    async with tenant_session(str(org_id), str(user_id)) as s:
        row, secret, _key = await svc.create_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            issuer_profile_id=issuer_id,
            label=f"native-{uuid.uuid4().hex[:6]}",
            provider="mycelium",
            enabled=True,
            invoice_mode="dry_run",
        )
        connector_id = row.id
    assert secret

    def native(reference: str, event_id: str) -> dict[str, Any]:
        return {
            "id": event_id,
            "type": "invoice.issue",
            "created": 1_755_000_000,
            "data": {
                "reference": reference,
                "currency": "EUR",
                "paid": True,
                "customer": {
                    "legal_name": "Acme SpA",
                    "country_code": "IT",
                    "vat_number": "09876543210",
                    "address": "Via Milano 9",
                    "postal_code": "20100",
                    "city": "Milano",
                    "province": "MI",
                    "country": "IT",
                    "sdi_code": "ABCDEFG",
                },
                "lines": [{"description": "Canone", "quantity": "1", "unit_price": "100.00"}],
            },
        }

    _c, shadow = await _ingest(org_id, connector_id, native("ORDER-1", "evt_s"))
    assert shadow is not None
    assert await _run(org_id, connector_id, shadow) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"invoice_mode": "transmit"},
        )

    # The sender now presents the reserved form. Under the old prefix scheme
    # this resolved to the shadow draft and transmitted it.
    _c2, forged = await _ingest(org_id, connector_id, native("dryrun:ORDER-1", "evt_f"))
    assert forged is not None
    assert await _run(org_id, connector_id, forged) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        archived = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        assert len(archived) == 1, "the shadow document is untouched"
        assert archived[0].state is InvoiceState.draft
        assert archived[0].number is None, "the shadow document was never filed"
        active = await inv_svc.list_invoices(s, org_id=org_id, view="active")
        assert len(active) == 1, "the forged reference emitted its OWN document"
        assert active[0].id != archived[0].id


async def test_a_shadow_run_can_actually_be_thrown_away() -> None:
    """The documented cleanup has to work.

    A shadow claim holds a RESTRICT foreign key on its draft -- the same FK that
    stops a live claim from ever dangling -- so deleting the draft directly
    fails with a raw ForeignKeyViolationError. ``discard_dry_run`` drops the
    claims first, which is the only order that works.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_discard"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        shadow = (await inv_svc.list_invoices(s, org_id=org_id, view="archived"))[0]
        # The direct route is genuinely blocked; that is WHY the operation exists.
        with pytest.raises(Exception):  # noqa: B017 (a driver-level FK violation)
            async with s.begin_nested():
                await inv_svc.delete_draft(s, org_id=org_id, actor_id=user_id, invoice_id=shadow.id)

    async with tenant_session(str(org_id), str(user_id)) as s:
        discarded = await svc.discard_dry_run(
            s, org_id=org_id, actor_id=user_id, connector_id=connector_id
        )
        assert discarded == 1

    async with tenant_session(str(org_id), str(user_id)) as s:
        assert await inv_svc.list_invoices(s, org_id=org_id, view="archived") == []
        links = (
            (
                await s.execute(
                    select(PaymentObjectLink).where(PaymentObjectLink.connector_id == connector_id)
                )
            )
            .scalars()
            .all()
        )
        assert links == [], "the shadow claims went with the documents"
        # The evidence survives: the XML is on the event, which now points at
        # no invoice (the FK is SET NULL) but still carries what was generated.
        event = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert event.dry_run_xml is not None


async def test_discarding_a_shadow_run_never_touches_a_live_document() -> None:
    """A connector that shadowed and then went live must keep its real
    invoices when the shadow run is cleared."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")
    _c, shadow = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_ds"))
    assert shadow is not None
    await _run(org_id, connector_id, shadow)

    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"invoice_mode": "transmit"},
        )
    _c2, live = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_dl"))
    assert live is not None
    assert await _run(org_id, connector_id, live) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        assert (
            await svc.discard_dry_run(s, org_id=org_id, actor_id=user_id, connector_id=connector_id)
            == 1
        )

    async with tenant_session(str(org_id), str(user_id)) as s:
        active = await inv_svc.list_invoices(s, org_id=org_id, view="active")
        assert len(active) == 1, "the real invoice is untouched"
        assert active[0].state is InvoiceState.transmitted
        live_links = (
            (
                await s.execute(
                    select(PaymentObjectLink).where(
                        PaymentObjectLink.connector_id == connector_id,
                        PaymentObjectLink.dry_run.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert live_links, "the live claims survive, or a redelivery would re-emit"


# --- the customer supplied their billing data late -------------------------


async def test_billing_data_entered_in_mycelium_unblocks_the_payment() -> None:
    """The scenario this exists for, end to end.

    A customer pays before completing their billing details, so the payment
    parks. The data then arrives OUTSIDE the provider -- by email, from an
    accountant -- and is entered in mycelium. Fixing the anagrafica alone can
    never unblock the payment: nothing ties that client to the provider's
    customer id, so a retry re-derives the counterpart from the frozen payload
    and finds it just as empty. The association is the missing edge, and it
    re-arms the waiting payments itself.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c, payment = await _ingest(
        org_id, connector_id, _invoice_paid(event_id="evt_late", metadata={})
    )
    assert payment is not None
    assert await _run(org_id, connector_id, payment) == "no_billing_data"

    # A retry changes nothing while the association does not exist: the payload
    # is frozen and still carries no fiscal identity.
    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.retry_event(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            event_id=payment,
        )
    assert await _run(org_id, connector_id, payment) == "no_billing_data"

    # The operator creates the client in mycelium from what the customer sent.
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.resolve_or_create_client(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="Acme SpA",
            profile=ClientInput(
                legal_name="Acme SpA",
                country_code="IT",
                vat_number="09876543210",
                address="Via Milano 9",
                postal_code="20100",
                city="Milano",
                province="MI",
                country="IT",
                sdi_code="ABCDEFG",
            ),
        )
        client_tag_id = tag.id

    # ...and points the provider customer at it.
    async with tenant_session(str(org_id), str(user_id)) as s:
        rearmed = await svc.assign_customer_client(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            provider_customer_id="cus_1",
            client_tag_id=client_tag_id,
        )
        assert rearmed == 1, "the association woke the payment waiting on it"

    async with tenant_session(str(org_id), str(user_id)) as s:
        row = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == payment)
            )
        ).scalar_one()
        assert row.status == "pending"
        assert row.attempt_count == 0

    assert await _run(org_id, connector_id, payment) == "done"
    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(invoices) == 1
        assert invoices[0].state is InvoiceState.transmitted
        assert invoices[0].client_tag_id == client_tag_id


async def test_assigning_a_client_that_is_still_incomplete_is_refused() -> None:
    """The refusal lands where the operator is looking.

    Accepting the association and letting the retry fail afterwards would move
    the error to a screen they are not on, for a reason they cannot see.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.resolve_or_create_client(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="Senza Recapito Srl",
            profile=ClientInput(
                legal_name="Senza Recapito Srl",
                country_code="IT",
                vat_number="09876543210",
                address="Via Milano 9",
                postal_code="20100",
                city="Milano",
                province="MI",
                country="IT",
                # no sdi_code and no pec: not deliverable
            ),
        )
        incomplete = tag.id

    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(UnprocessableError) as exc:
            await svc.assign_customer_client(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                provider_customer_id="cus_x",
                client_tag_id=incomplete,
            )
    # A DomainError, deliberately, and not the runner's internal
    # MissingBillingDataError: only a DomainError is in the exception->status
    # map, so only a DomainError reaches the operator as a 422 naming the
    # fields. Raising the internal signal here surfaced as an opaque 500.
    assert isinstance(exc.value, DomainError)
    assert exc.value.code is MessageCode.PAYMENT_CONNECTOR_CLIENT_INCOMPLETE
    assert "sdi_code|pec" in str(exc.value)


async def test_a_non_client_tag_cannot_be_assigned_as_a_counterpart() -> None:
    """Only a client tag names a counterpart. A project or a generic tag has no
    ClientProfile behind it and would produce an undeliverable document."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    async with tenant_session(str(org_id), str(user_id)) as s:
        generic = await taxonomy.create_tag(
            s,
            org_id=org_id,
            actor_id=user_id,
            kind=TagKind.generic,
            name=f"tag-{uuid.uuid4().hex[:6]}",
        )

    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(DomainError):
            await svc.assign_customer_client(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                provider_customer_id="cus_y",
                client_tag_id=generic.id,
            )
