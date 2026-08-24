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
from mycelium_core.errors import ConflictError, DomainError, UnprocessableError
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
    # Overridable because it is one of the ids the connector CLAIMS: two events
    # that share it are two announcements of one payment, and resolve to the
    # same document. A test that wants two distinct payments has to vary it.
    payment_intent_id: str = "pi_1",
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
                "payment_intent": payment_intent_id,
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


async def test_an_ignored_event_always_says_why() -> None:
    """ "Ignored" with an empty reason is the one outcome an operator cannot act
    on: it reads as "something happened and nobody will say what". Payment
    reconciliation is the branch that can legitimately find nothing to do, so it
    is the branch that has to name it."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    # A payment for money we never invoiced: no document of ours matches it.
    _c, orphan = await _ingest(
        org_id,
        connector_id,
        {
            "id": "evt_orphan",
            "type": "charge.succeeded",
            "created": 1_755_000_400,
            "data": {
                "object": {
                    "id": "ch_orphan",
                    "object": "charge",
                    "currency": "eur",
                    "payment_intent": "pi_orphan",
                    "amount": 5000,
                }
            },
        },
    )
    assert orphan is not None
    assert await _run(org_id, connector_id, orphan) == "ignored"
    async with tenant_session(str(org_id), str(user_id)) as s:
        row = await s.get(PaymentConnectorEvent, orphan)
        assert row is not None
        assert row.last_error == "payment_without_invoice"

    # And with reconciliation switched off, the reason says THAT instead.
    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"payment_sync_enabled": False},
        )
    _c2, off = await _ingest(
        org_id,
        connector_id,
        {
            "id": "evt_off",
            "type": "charge.succeeded",
            "created": 1_755_000_500,
            "data": {
                "object": {
                    "id": "ch_off",
                    "object": "charge",
                    "currency": "eur",
                    "payment_intent": "pi_off",
                    "amount": 5000,
                }
            },
        },
    )
    assert off is not None
    assert await _run(org_id, connector_id, off) == "ignored"
    async with tenant_session(str(org_id), str(user_id)) as s:
        row = await s.get(PaymentConnectorEvent, off)
        assert row is not None
        assert row.last_error == "payment_sync_off"


def _invoice_paid_2026_api(event_id: str = "evt_new_api") -> dict[str, Any]:
    """A real ``invoice.paid`` as the 2026-07-29 API sends it.

    Reduced from an actual production delivery. The tax breakdown is the part
    that matters: ``taxes`` instead of ``tax_amounts``, ``tax_behavior``
    instead of ``inclusive``, and the rate identified only by id -- with the
    percentage nowhere in the payload.
    """
    return {
        "id": event_id,
        "type": "invoice.paid",
        "created": 1_786_967_554,
        "api_version": "2026-07-29.dahlia",
        "data": {
            "object": {
                "id": "in_new_api",
                "object": "invoice",
                "currency": "eur",
                "customer": "cus_new_api",
                "customer_name": "AIR CONSULTING GROUP SRL",
                "customer_email": "amministrazione@acme.test",
                "customer_address": {
                    "city": "Roma",
                    "country": "IT",
                    "line1": "Via Donatello, 67",
                    "postal_code": "00196",
                    "state": "RM",
                },
                "customer_tax_ids": [{"type": "eu_vat", "value": "IT11278231003"}],
                "metadata": {"codice_destinatario": "ABCDEFG"},
                "total": 2500,
                "subtotal_excluding_tax": 2049,
                "lines": {
                    "data": [
                        {
                            "id": "il_new_api",
                            "object": "line_item",
                            "amount": 2500,
                            "currency": "eur",
                            "description": "MrCall Business",
                            "quantity": 1,
                            "subtotal": 2049,
                            "taxes": [
                                {
                                    "amount": 451,
                                    "tax_behavior": "inclusive",
                                    "taxable_amount": 2049,
                                    "tax_rate_details": {"tax_rate": "txr_1N5pXm"},
                                    "type": "tax_rate_details",
                                }
                            ],
                        }
                    ]
                },
            }
        },
    }


async def test_the_2026_stripe_tax_shape_does_not_inflate_the_invoice() -> None:
    """A live account moved to the 2026-07-29 API and the money changed meaning.

    The tax moved from ``tax_amounts`` (expanded rate, boolean ``inclusive``) to
    ``taxes`` (``tax_behavior``, rate by id only). Reading just the old name is
    not a missing feature, it is a WRONG DOCUMENT: the line looks untaxed, its
    GROSS amount is taken for a net one, and the connector default is added on
    top -- 25.00 collected, 30.50 invoiced, filed with SdI.

    The rate is nowhere in the payload, so it is recovered from the amounts by
    finding the statutory rate that reproduces the reported tax: 2049 x 22% =
    450.78 -> 451. That is a verification, not a guess, and it avoids the
    22.0107 the raw quotient would give.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid_2026_api())
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        inv = (await inv_svc.list_invoices(s, org_id=org_id, view="active"))[0]
        assert inv.total == Decimal("25.00"), (
            "the document must be for the money that was actually collected"
        )
        assert inv.taxable == Decimal("20.49")
        assert inv.vat == Decimal("4.51")


def _customer_updated_real() -> dict[str, Any]:
    """A real ``customer.updated``, reduced from a production delivery.

    Two things in it are not hypothetical. ``address.state`` is ``"Lazio"`` --
    Stripe's state field is free text and Italian records carry the region as
    often as the sigla -- and the metadata bag holds the codice destinatario
    under several spellings at once, including a legacy vendor's.
    """
    return {
        "id": "evt_customer_real",
        "type": "customer.updated",
        "created": 1_786_991_436,
        "data": {
            "object": {
                "id": "cus_real",
                "object": "customer",
                "name": "AIR CONSULTING GROUP SRL",
                "email": "amministrazione@acme.test",
                "address": {
                    "city": "Roma",
                    "country": "IT",
                    "line1": "Via Donatello, 67",
                    "line2": "JHBM40P",
                    "postal_code": "00196",
                    "state": "Lazio",
                },
                "metadata": {
                    "companyName": "AIR CONSULTING GROUP SRL",
                    "billit_allowsend": "true",
                    "billit_identifier_sdicoddest": "JHBM40P",
                    "vatId": "IT11278231003",
                    "codice_destinatario": "JHBM40P",
                    "fiscal_code": "IT11278231003",
                },
            }
        },
    }


async def test_a_customer_event_becomes_a_client_even_with_a_region_in_state() -> None:
    """The event that silently failed in production.

    Stripe sent ``state: "Lazio"`` and the province column holds four
    characters, so the insert raised a driver-level truncation error. That is
    NOT a DomainError: it escaped the runner, failed the event, and retried a
    payload that could never succeed -- while the operator saw a customer they
    had just updated in Stripe simply not appear.

    So the mapper makes values fit before they reach the schema, and a
    ``state`` that is not a sigla is DROPPED rather than truncated: "Lazio"
    would have become "LAZI", and a wrong provincia on a fiscal document is
    worse than an absent one, which the standard permits.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    _c, event_id = await _ingest(org_id, connector_id, _customer_updated_real())
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        link = (
            await s.execute(
                select(PaymentCustomerLink).where(
                    PaymentCustomerLink.provider_customer_id == "cus_real"
                )
            )
        ).scalar_one()
        profile = (
            await s.execute(select(ClientProfile).where(ClientProfile.tag_id == link.client_tag_id))
        ).scalar_one()

    assert profile.legal_name == "AIR CONSULTING GROUP SRL"
    assert profile.sdi_code == "JHBM40P", "the codice destinatario lives in the metadata"
    assert profile.city == "Roma"
    assert profile.postal_code == "00196"
    assert profile.address == "Via Donatello, 67"
    assert profile.province is None, "'Lazio' is a region, not a sigla, so it is dropped"
    # ``address.line2`` also held the codice destinatario, because the customer
    # typed it into the only free field the checkout offered. It is NOT read as
    # one: a second address line is an address line.
    assert "JHBM40P" not in (profile.address or "")


async def _native_connector(
    org_id: uuid.UUID, user_id: uuid.UUID, issuer_id: uuid.UUID
) -> uuid.UUID:
    async with tenant_session(str(org_id), str(user_id)) as s:
        row, _secret, _key = await svc.create_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            issuer_profile_id=issuer_id,
            label=f"native-{uuid.uuid4().hex[:6]}",
            provider="mycelium",
            enabled=True,
        )
        return row.id


def _native_issue(event_id: str, **line_over: Any) -> dict[str, Any]:
    line: dict[str, Any] = {"description": "Consulenza", "unit_price": "100.00"}
    line.update(line_over)
    return {
        "id": event_id,
        "type": "invoice.issue",
        "created": 1_755_000_000,
        "data": {
            "reference": f"ORD-{event_id}",
            "customer": {
                "legal_name": "Acme SpA",
                "country_code": "IT",
                "vat_number": "IT09876543210",
                "address": "Via Milano",
                "civic_number": "9",
                "postal_code": "20100",
                "city": "Milano",
                "province": "MI",
                "sdi_code": "ABCDEFG",
            },
            "lines": [line],
        },
    }


async def test_no_provider_value_can_fail_an_event_with_a_driver_error() -> None:
    """The class of bug, not one instance of it.

    A value the schema cannot hold does not truncate on the way in: it raises a
    driver-level error (string truncation, numeric overflow) that is NOT a
    DomainError, so it escapes the event runner, fails the event, and retries a
    payload that can never succeed. The operator sees nothing happen and has
    nothing to read.

    One of these was live: Stripe sends ``address.state: "Lazio"`` and the
    province column holds four characters, so every customer event from that
    account failed silently. Probing the rest of the surface found five more,
    across BOTH providers -- which is the point: the model is generic and has to
    survive whatever either kind of sender puts in it.

    Strings that are merely long are trimmed or dropped and the document is
    still emitted; figures that do not fit are REFUSED (money silently shrunk
    would be a number the sender never wrote, in a fiscal document). Either way
    the outcome is one the runner classifies and the operator can read.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    stripe_id = await _connector(org_id, user_id, issuer_id)
    native_id = await _native_connector(org_id, user_id, issuer_id)

    long_text = "X" * 400
    stripe_cases: list[tuple[str, dict[str, Any], str]] = [
        # invoices.purpose is String(200); a Stripe description is free text.
        ("long_description", {"description": long_text}, "done"),
        # client country is String(2) and a hand-made customer carries a name.
        (
            "long_country",
            {
                "customer_address": {
                    "city": "Milano",
                    "country": "ITALIA",
                    "line1": "Via Milano 9",
                    "postal_code": "20100",
                    "state": "Lombardia",
                }
            },
            "done",
        ),
        ("long_name", {"customer_name": long_text}, "done"),
    ]
    for name, over, expected in stripe_cases:
        payload = _invoice_paid(event_id=f"evt_{name}", invoice_id=f"in_{name}")
        payload["data"]["object"].update(over)
        payload["data"]["object"]["payment_intent"] = f"pi_{name}"
        payload["data"]["object"]["charge"] = f"ch_{name}"
        _c, event_id = await _ingest(org_id, stripe_id, payload)
        assert event_id is not None
        assert await _run(org_id, stripe_id, event_id) == expected, name

    native_cases: list[tuple[str, dict[str, Any], str]] = [
        ("huge_price", {"unit_price": "999999999999999.00"}, "needs_attention"),
        ("huge_rate", {"vat_rate": "99999"}, "needs_attention"),
        ("huge_quantity", {"quantity": "99999999999999"}, "needs_attention"),
        ("long_description", {"description": long_text}, "done"),
    ]
    for name, over, expected in native_cases:
        payload = _native_issue(f"nat_{name}", **over)
        _c, event_id = await _ingest(org_id, native_id, payload)
        assert event_id is not None
        assert await _run(org_id, native_id, event_id) == expected, name

    # A country NAME on our own contract is the same trap from the other side.
    payload = _native_issue("nat_country")
    payload["data"]["customer"]["country_code"] = "ITALIA"
    _c, event_id = await _ingest(org_id, native_id, payload)
    assert event_id is not None
    assert await _run(org_id, native_id, event_id) == "done"


# --- credit notes ----------------------------------------------------------


def _refund(*, event_id: str, amount: int, refund_id: str = "re_1") -> dict[str, Any]:
    """A refund the way the DEFAULT configuration hears about it.

    ``refund.created``, not ``charge.refunded``: a connector honours exactly one
    of Stripe's two refund announcements (``refund_event``), because the pair
    does not reliably deduplicate and acting on both would file two TD04 for one
    refund. The legacy announcement has its own test below.
    """
    return {
        "id": event_id,
        "type": "refund.created",
        "created": 1_755_000_200,
        "data": {
            "object": {
                "id": refund_id,
                "object": "refund",
                "currency": "eur",
                "charge": "ch_1",
                "payment_intent": "pi_1",
                "amount": amount,
                "reason": "requested",
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


def _legacy_refund(*, event_id: str, amount: int) -> dict[str, Any]:
    """The older announcement, for an endpoint that predates ``refund.created``."""
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
                "refunds": {"data": [{"id": "re_1", "amount": amount, "reason": "requested"}]},
            }
        },
    }


async def test_a_connector_acts_on_one_refund_announcement_and_files_the_other() -> None:
    """The two announcements of one refund are configuration, not both-and.

    Checked end to end rather than only at the mapper, because what matters
    operationally is the LEDGER: the announcement that is not honoured has to
    land as a recorded ``ignored`` event, so an operator who subscribed the
    wrong one in Stripe can see why no credit note appeared. A silent drop would
    look exactly like a delivery that never arrived.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    await _emit_parent(org_id, connector_id)

    _c, ignored_id = await _ingest(org_id, connector_id, _legacy_refund(event_id="evt_x", amount=1))
    assert ignored_id is not None
    assert await _run(org_id, connector_id, ignored_id) == "ignored"

    async with tenant_session(str(org_id), str(user_id)) as s:
        row = await s.get(PaymentConnectorEvent, ignored_id)
        assert row is not None
        assert row.status == "ignored"
        assert row.last_error == "refund_event_not_selected", (
            "the reason has to survive into the ledger, or an operator who "
            "subscribed the other event has nothing to go on"
        )
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert not notes, "the unselected announcement must not reverse anything"


async def test_the_legacy_refund_announcement_works_when_selected() -> None:
    """An endpoint configured years ago only delivers ``charge.refunded``.
    Selecting it has to be enough: the guard narrows to ONE announcement, it
    does not deprecate the older one."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, refund_event="charge.refunded")
    await _emit_parent(org_id, connector_id)

    _c, event_id = await _ingest(
        org_id, connector_id, _legacy_refund(event_id="evt_l", amount=6100)
    )
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        notes = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id)
            if i.document_type is DocumentType.TD04
        ]
        assert len(notes) == 1
        assert notes[0].total == Decimal("61.00")

    # ... and the newer announcement for the SAME refund is now the ignored one,
    # which is the whole point: exactly one of the pair is ever honoured.
    _c2, other = await _ingest(org_id, connector_id, _refund(event_id="evt_l2", amount=6100))
    assert other is not None
    assert await _run(org_id, connector_id, other) == "ignored"


async def test_refund_is_not_reversed_twice() -> None:
    """A dashboard refund fires both refund.created and credit_note.created;
    they share the refund id, so whichever lands first claims it and the other
    settles onto the same document."""
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


async def test_a_shadow_document_says_why_it_was_not_sent() -> None:
    """The marker an operator reads during a parallel run.

    A shadow document is a draft, archived out of the active list -- which is
    also what an incomplete draft, a document waiting for review, and one
    rejected by SdI and being redone all look like. During a parallel run the
    only question that matters about an unsent document is "was it held back
    because we are shadowing, or for some other reason?", so the document
    carries the answer rather than requiring a join through the ingress ledger.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        shadow = (await inv_svc.list_invoices(s, org_id=org_id, view="archived"))[0]
        assert shadow.dry_run is True
        assert shadow.state is InvoiceState.draft
        assert shadow.number is None, "a shadow run spends no fiscal number"

    # ... and a document composed with the flag OFF never carries the marker,
    # or it would say "not sent because we were shadowing" about a real one.
    live_connector = await _connector(org_id, user_id, issuer_id, invoice_mode="transmit")
    _c2, live_event = await _ingest(
        org_id, live_connector, _invoice_paid(event_id="evt_live", invoice_id="in_live")
    )
    assert live_event is not None
    assert await _run(org_id, live_connector, live_event) == "done"
    async with tenant_session(str(org_id), str(user_id)) as s:
        real = [
            i
            for i in await inv_svc.list_invoices(s, org_id=org_id, view="active")
            if i.state is InvoiceState.transmitted
        ]
        assert len(real) == 1
        assert real[0].dry_run is False


async def test_promoting_a_shadow_makes_it_sendable_and_moves_its_claim() -> None:
    """The manual exit: this payment has to be invoiced by US after all.

    The claim moving with the document is the part that is not obvious and the
    part that matters. A promoted document whose claim stayed in the shadow
    universe would be invisible to a live lookup, so the next redelivery of the
    same payment would compose a SECOND invoice for money already invoiced --
    the exact duplication the whole ledger exists to prevent.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    _c, shadow_event = await _ingest(org_id, connector_id, _invoice_paid())
    assert shadow_event is not None
    assert await _run(org_id, connector_id, shadow_event) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        shadow = (await inv_svc.list_invoices(s, org_id=org_id, view="archived"))[0]
        promoted = await svc.promote_dry_run(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            invoice_id=shadow.id,
        )
        assert promoted.dry_run is False
        assert promoted.is_archived is False, "back where sendable drafts are read"
        assert promoted.state is InvoiceState.draft
        assert promoted.number is None, "the number is allocated at transmit, not here"

    # The claim moved: the connector goes live, the same payment is redelivered,
    # and it must resolve to the promoted document instead of composing another.
    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"invoice_mode": "transmit"},
        )
    _c2, live = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_again"))
    assert live is not None
    assert await _run(org_id, connector_id, live) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        # Both bands, explicitly: a promoted document leaves the archive, so
        # counting only one band could miss a second document rather than prove
        # there is none.
        active = await inv_svc.list_invoices(s, org_id=org_id, view="active")
        archived = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        everything = active + archived
        assert len(everything) == 1, "one payment, one document -- promoted then transmitted"
        assert everything[0].state is InvoiceState.transmitted
        assert everything[0].number is not None, "the number arrives with the real filing"


async def test_promoting_is_refused_when_a_real_document_already_covers_it() -> None:
    """The other order of events: the connector went live first, so a real
    document already exists for this payment. Promoting the leftover shadow
    would invoice it twice, so it is refused rather than filed."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    _c, shadow_event = await _ingest(org_id, connector_id, _invoice_paid())
    assert shadow_event is not None
    assert await _run(org_id, connector_id, shadow_event) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.update_connector(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            values={"invoice_mode": "transmit"},
        )
    _c2, live = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_real"))
    assert live is not None
    assert await _run(org_id, connector_id, live) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        shadow = (await inv_svc.list_invoices(s, org_id=org_id, view="archived"))[0]
        with pytest.raises(DomainError) as err:
            await svc.promote_dry_run(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                invoice_id=shadow.id,
            )
        assert err.value.code is MessageCode.PAYMENT_CONNECTOR_ALREADY_EMITTED

    async with tenant_session(str(org_id), str(user_id)) as s:
        still = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        assert still[0].dry_run is True, "the refusal changed nothing"


async def test_promoting_a_document_that_was_never_shadowed_is_refused() -> None:
    """Promotion exists to undo the ONE reason a document was held back. Applied
    to anything else it would be an unaudited way to un-archive a document."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="transmit")

    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        real = (await inv_svc.list_invoices(s, org_id=org_id, view="active"))[0]
        with pytest.raises(DomainError) as err:
            await svc.promote_dry_run(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                invoice_id=real.id,
            )
        assert err.value.code is MessageCode.PAYMENT_CONNECTOR_NOT_DRY_RUN


async def test_discard_leaves_a_promoted_document_alone() -> None:
    """Discard is "throw away what we were only comparing". A document an
    operator deliberately promoted is no longer that, and deleting it would
    destroy a document meant to be filed."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")

    _c, keep = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_keep"))
    _c2, drop = await _ingest(
        org_id,
        connector_id,
        _invoice_paid(
            event_id="evt_drop",
            invoice_id="in_drop",
            charge_id="ch_drop",
            payment_intent_id="pi_drop",
        ),
    )
    assert keep is not None and drop is not None
    assert await _run(org_id, connector_id, keep) == "done"
    assert await _run(org_id, connector_id, drop) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        shadows = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        assert len(shadows) == 2
        kept = next(i for i in shadows if i.id)  # promote one of them
        await svc.promote_dry_run(
            s, org_id=org_id, actor_id=user_id, connector_id=connector_id, invoice_id=kept.id
        )

    async with tenant_session(str(org_id), str(user_id)) as s:
        discarded = await svc.discard_dry_run(
            s, org_id=org_id, actor_id=user_id, connector_id=connector_id
        )
        assert discarded == 1, "only the document still marked as a shadow"

    async with tenant_session(str(org_id), str(user_id)) as s:
        active = await inv_svc.list_invoices(s, org_id=org_id, view="active")
        archived = await inv_svc.list_invoices(s, org_id=org_id, view="archived")
        left = active + archived
        assert [i.id for i in left] == [kept.id]
        assert left[0].dry_run is False


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


# --- what a connector says about payment ------------------------------------


async def _issuer_with_iban(org_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """The shape a person's own issuer profile really has: a default IBAN,
    because their hand-written invoices are paid by bonifico."""
    async with tenant_session(str(org_id), str(user_id)) as s:
        profile = await inv_svc.create_issuer_profile(
            s,
            org_id=org_id,
            actor_id=user_id,
            label=f"con-iban-{uuid.uuid4().hex[:6]}",
            legal_name="HahnBanach SRL",
            vat_number="01234567890",
            address="Via Roma",
            civic_number="1",
            postal_code="00100",
            city="Roma",
            province="RM",
            default_iban="IT60X0542811101000000123456",
        )
        return profile.id


async def test_a_connector_document_does_not_inherit_the_issuer_bonifico_iban() -> None:
    """The route that made this a live defect rather than a theoretical one:
    nothing in the connector's own configuration mentions payment, yet
    ``create_draft`` used to copy the issuer's default IBAN onto every draft.
    An IBAN alone opens <DatiPagamento>, and ModalitaPagamento then resolves to
    the module default MP05, so a card charge went out described as a bank
    transfer, with a bonifico IBAN attached, to the customer's accountant."""
    org_id, user_id, _ = await _org_and_issuer()
    issuer_id = await _issuer_with_iban(org_id, user_id)
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="draft")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_iban"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        inv = (await inv_svc.list_invoices(s, org_id=org_id))[0]
        assert inv.payment_iban is None
        xml = await inv_svc.get_xml_preview(s, org_id=org_id, invoice_id=inv.id)
    assert "<DatiPagamento>" not in xml
    assert "MP05" not in xml
    assert "IT60X0542811101000000123456" not in xml


async def test_a_connector_that_states_a_method_states_the_terms_with_it() -> None:
    """The opt-in half. An operator who says "this connector takes cards" gets
    a complete payment block, not a method with the terms left to fall through
    (nor terms with the method left to fall through, which is refused outright
    at configuration time)."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(
        org_id, user_id, issuer_id, invoice_mode="draft", default_payment_method_code="MP08"
    )
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_mp08"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        inv = (await inv_svc.list_invoices(s, org_id=org_id))[0]
        xml = await inv_svc.get_xml_preview(s, org_id=org_id, invoice_id=inv.id)
    assert "<ModalitaPagamento>MP08</ModalitaPagamento>" in xml
    assert "<CondizioniPagamento>TP02</CondizioniPagamento>" in xml


async def test_a_connector_cannot_state_payment_terms_without_a_method() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    with pytest.raises(UnprocessableError) as err:
        await _connector(org_id, user_id, issuer_id, default_payment_conditions_code="TP02")
    assert err.value.code is MessageCode.PAYMENT_CONNECTOR_PAYMENT_PAIR_INVALID


async def test_a_connector_cannot_carry_a_payment_code_outside_the_sdi_tables() -> None:
    """These were not validated on the connector row at all: the router bounds
    them to four characters and the service did a blind setattr, so an unknown
    code was accepted and only failed later, while composing a document from a
    webhook, where a domain error is a parked event rather than a 422."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    with pytest.raises(DomainError):
        await _connector(org_id, user_id, issuer_id, default_payment_method_code="MP99")


# --- re-shooting the shadow document ----------------------------------------


async def test_reshooting_replaces_the_shadow_xml_with_todays_build() -> None:
    """A shadow blob is the ONLY XML this subsystem stores, so it is the only
    one a serializer fix does not reach on its own: a live draft holds none and
    is rebuilt on read. These blobs still carry whatever the builder produced
    the day they were captured."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_shadow"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        ev = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert ev.dry_run_xml is not None
        # Stand in for "the builder changed since": a stale blob.
        ev.dry_run_xml = "<stale/>"
        await s.flush()

        row = await svc.reshoot_dry_run_xml(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            event_id=event_id,
        )
        assert row.dry_run_xml is not None
        assert row.dry_run_xml != "<stale/>"
        assert "<FatturaElettronica" in row.dry_run_xml
        # It is still a would-be document: re-shooting allocates nothing.
        assert "<ProgressivoInvio>ANTEPRIMA</ProgressivoInvio>" in row.dry_run_xml


async def test_reshooting_is_refused_once_the_document_has_been_promoted() -> None:
    """Promotion clears ``dry_run`` on the invoice: the document has left the
    shadow universe and is a real draft an operator may send. Re-shooting a
    comparison artefact for a document nobody is comparing is meaningless, and
    the row stops offering it."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="dry_run")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_promo"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        ev = (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()
        assert ev.invoice_id is not None
        await svc.promote_dry_run(
            s,
            org_id=org_id,
            actor_id=user_id,
            connector_id=connector_id,
            invoice_id=ev.invoice_id,
        )
        with pytest.raises(ConflictError):
            await svc.reshoot_dry_run_xml(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                event_id=event_id,
            )


# --- recompose --------------------------------------------------------------


async def _one_event(org_id, user_id, connector_id, event_id):
    async with tenant_session(str(org_id), str(user_id)) as s:
        return (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()


async def test_recompose_discards_the_document_and_rearms_the_event() -> None:
    """The verb Retry is not: retry finds the object claim and settles what
    exists. This is for a fix to the MAPPER, where the stale part is the
    persisted row rather than the serialization."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="draft")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_rc"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        before = (await inv_svc.list_invoices(s, org_id=org_id))[0]
        assert before.number is None
        row = await svc.recompose_event(
            s, org_id=org_id, actor_id=user_id, connector_id=connector_id, event_id=event_id
        )
        assert row.status == "pending"
        assert row.attempt_count == 0
        assert row.invoice_id is None
        # The document is gone, and so is its claim: the event is free to
        # compose again and re-claim the same object keys.
        assert await inv_svc.list_invoices(s, org_id=org_id) == []
        claims = (
            (
                await s.execute(
                    select(PaymentObjectLink).where(PaymentObjectLink.invoice_id == before.id)
                )
            )
            .scalars()
            .all()
        )
        assert claims == []

    # And re-running it composes a fresh document rather than resolving to the
    # deleted one.
    assert await _run(org_id, connector_id, event_id) == "done"
    async with tenant_session(str(org_id), str(user_id)) as s:
        again = await inv_svc.list_invoices(s, org_id=org_id)
        assert len(again) == 1, "exactly one document, not zero and not two"
        assert again[0].id != before.id


async def test_recompose_refuses_a_draft_that_already_spent_a_fiscal_number() -> None:
    """A definite-not-filed failure returns an invoice to draft while KEEPING
    its number and NomeFile, so the retry sails under the same file name and
    collides with SdI's own dedupe instead of double-filing. Deleting such a
    draft destroys that property and leaves a hole in the counter."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id, invoice_mode="draft")
    _c, event_id = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_burnt"))
    assert event_id is not None
    assert await _run(org_id, connector_id, event_id) == "done"

    async with tenant_session(str(org_id), str(user_id)) as s:
        doc = (await inv_svc.list_invoices(s, org_id=org_id))[0]
        doc.number = 7
        doc.nome_file = "IT01234567890_00007.xml"
        await s.flush()
        with pytest.raises(ConflictError):
            await svc.recompose_event(
                s, org_id=org_id, actor_id=user_id, connector_id=connector_id, event_id=event_id
            )
        # Nothing was touched by the refusal.
        assert len(await inv_svc.list_invoices(s, org_id=org_id)) == 1
