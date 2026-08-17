"""Durability of the inbound payment connectors under at-least-once delivery
and N replicas (ADR-0051).

The sibling module ``test_payment_connectors_service`` covers what the runner
DOES with one event. This module covers what happens when the runner is
interrupted, duplicated or repeated -- the properties that decide whether the
subsystem may be switched on with more than one worker, given that a provider
guarantees at-least-once delivery and nothing else:

- CLAIMING: ``claim_due`` is a real lease (``FOR UPDATE SKIP LOCKED`` plus the
  status flip), so a second claimer -- concurrent or later -- sees nothing;
- THE LEASE: ``reclaim_expired`` returns an abandoned row to the pool only once
  ``payment_connector_lease_seconds`` has actually elapsed (both sides of the
  boundary are tested by writing ``last_attempt_at`` directly);
- RESUME, NOT RESTART: an event whose worker died mid-emission re-processes onto
  the SAME document. This is the property the whole ordering rule exists for, so
  it is tested against a dispatch that genuinely failed after the pre-dispatch
  commit, not only against a clean re-run;
- IDEMPOTENCY: reprocessing an event, and processing a second event that names
  the same money, leave the fiscal number and the object links untouched;
- BACKOFF AND PARKING: a deferrable event backs off on the documented curve, is
  invisible to ``claim_due`` until then, and becomes ``dead`` when the budget is
  spent instead of retrying forever; ``retry_event`` re-arms it;
- RLS: the five tables are org-scoped and fail closed with no tenant GUC;
- CLIENT RESOLUTION: the ``payment_customer_links`` identity map keeps one
  provider customer on one client tag, including when its fiscal data changes.

The default SdI channel is ``ManualExportChannel``, so a successful transmit
lands on ``state=transmitted`` with ``identificativo_sdi`` NULL and the dispatch
lease CLEARED -- that is the settled shape. A parked (retryable) dispatch is the
same state with the lease still set, which is what the failing-channel tests
below produce on purpose.

ONE TEST IN THIS MODULE FAILS ON PURPOSE.
``test_redelivered_refund_respects_credit_note_mode_draft`` documents a real
defect: ``payment_connectors._settle`` (line 993) decides whether to transmit by
reading ``connector.invoice_mode`` even when the document it was handed is a
credit note, so a redelivered refund files a TD04 that ``credit_note_mode='draft'``
asked to hold. It is left red rather than softened, because the assertion is the
bug report.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text, update

from mycelium_core.config import get_settings
from mycelium_core.db import ActorKind, admin_session, tenant_session
from mycelium_core.errors import ConflictError
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, Invoice, InvoiceState, PaymentStatus
from mycelium_core.models.membership import Role
from mycelium_core.models.payment_connector import (
    PaymentConnector,
    PaymentConnectorEvent,
    PaymentCustomerLink,
    PaymentObjectLink,
    PaymentWebhookDelivery,
)
from mycelium_core.sdi_channel import (
    IntermediaryIdentity,
    TransmitResult,
    set_channel_override,
)
from mycelium_core.services import invoice as inv_svc
from mycelium_core.services import payment_connectors as svc
from mycelium_core.services.auth import signup
from mycelium_core.services.payment_events import EmissionIntent, get_mapper

# --- fixtures-by-hand ------------------------------------------------------
# There are no shared fixtures in this repo: every module mints its own tenant
# per test. Copied from test_payment_connectors_service so the two modules can
# drift independently -- a helper shared between them would silently couple the
# behavioural suite to the durability suite.


async def _org_and_issuer() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="PCD",
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


def _invoice_paid(
    *,
    event_id: str = "evt_1",
    invoice_id: str = "in_1",
    charge_id: str = "ch_1",
    payment_intent_id: str = "pi_1",
    customer_id: str = "cus_1",
    customer_name: str = "Acme SpA",
    city: str = "Milano",
    amount: int = 10000,
    vat_amount: int = 2200,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "invoice.paid",
        "created": 1_755_000_000,
        "data": {
            "object": {
                "id": invoice_id,
                "object": "invoice",
                "currency": "eur",
                "customer": customer_id,
                "customer_name": customer_name,
                "customer_email": "amministrazione@acme.test",
                "customer_address": {
                    "line1": "Via Milano 9",
                    "postal_code": "20100",
                    "city": city,
                    "state": "MI",
                    "country": "IT",
                },
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


async def _ingest(org_id: uuid.UUID, connector_id: uuid.UUID, payload: dict[str, Any]) -> uuid.UUID:
    """Persist one event and return its id.

    The created/duplicate flag is covered in ``test_payment_connectors_service``;
    everything here starts from an event that is already on disk, which is the
    only state the runner ever sees.
    """
    async with tenant_session(str(org_id), str(connector_id), actor_kind="payment_connector") as s:
        await svc.ingest(
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
        ).scalar_one()
    return row


# --- direct row surgery ----------------------------------------------------


async def _force_event(
    org_id: uuid.UUID, user_id: uuid.UUID, event_id: uuid.UUID, **values: Any
) -> None:
    """Write straight onto an event row.

    The lease clock, the attempt budget and the ``processing`` state are
    normally written by a worker that is not running in this process. Setting
    them by hand is the only way to test the BOUNDARY of a time-based rule
    without sleeping for ten minutes, and a row left ``processing`` with a stale
    ``last_attempt_at`` is exactly what a pod that died mid-emission leaves
    behind.
    """
    async with tenant_session(str(org_id), str(user_id)) as s:
        await s.execute(
            update(PaymentConnectorEvent)
            .where(PaymentConnectorEvent.id == event_id)
            .values(**values)
        )


async def _age_dispatch_lease(org_id: uuid.UUID, user_id: uuid.UUID, invoice_id: uuid.UUID) -> None:
    """Push an invoice's SdI dispatch lease into the past.

    ADR-0046 refuses to re-transmit while the dispatch lease is fresh, and
    ADR-0051 sizes the CONNECTOR lease (600 s) strictly above the whole dispatch
    budget (120 s timeout + 300 s lease) precisely so that an event which
    becomes reclaimable always finds a dispatch that is provably no longer in
    flight. Ageing the invoice lease here reproduces that ordering; without it
    the test would be asserting a race the design forbids.
    """
    stale = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
        seconds=get_settings().sdi_dispatch_lease_seconds + 60
    )
    async with tenant_session(str(org_id), str(user_id)) as s:
        await s.execute(
            update(Invoice).where(Invoice.id == invoice_id).values(sdi_dispatch_started_at=stale)
        )


# --- readers ---------------------------------------------------------------


async def _event(
    org_id: uuid.UUID, user_id: uuid.UUID, event_id: uuid.UUID
) -> PaymentConnectorEvent:
    async with tenant_session(str(org_id), str(user_id)) as s:
        return (
            await s.execute(
                select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
            )
        ).scalar_one()


async def _invoices(org_id: uuid.UUID, user_id: uuid.UUID) -> list[Invoice]:
    async with tenant_session(str(org_id), str(user_id)) as s:
        return await inv_svc.list_invoices(s, org_id=org_id)


async def _links(
    org_id: uuid.UUID, user_id: uuid.UUID, connector_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str, uuid.UUID]]:
    """Every object-link row for a connector, as a stable sorted tuple list.

    The link's own id is part of the tuple on purpose: the idempotency claim is
    ``ON CONFLICT DO NOTHING``, so a re-run that silently re-inserted (or
    re-pointed) a link would keep the same (kind, object_id) pair and could only
    be caught by watching the row identity.
    """
    async with tenant_session(str(org_id), str(user_id)) as s:
        rows = (
            (
                await s.execute(
                    select(
                        PaymentObjectLink.id,
                        PaymentObjectLink.object_kind,
                        PaymentObjectLink.object_id,
                        PaymentObjectLink.invoice_id,
                    ).where(PaymentObjectLink.connector_id == connector_id)
                )
            )
            .tuples()
            .all()
        )
    return sorted(rows, key=lambda r: (r[1], r[2]))


async def _claim(org_id: uuid.UUID, user_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session(str(org_id), str(user_id), actor_kind="system") as s:
        return await svc.claim_due(s, org_id=org_id)


async def _reclaim(org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    async with tenant_session(str(org_id), str(user_id), actor_kind="system") as s:
        return await svc.reclaim_expired(s, org_id=org_id)


async def _emission_keys(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    connector_id: uuid.UUID,
    payload: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    """The object keys the MAPPER actually derives for this payload.

    Read from the mapper rather than hardcoded, so "a link exists for every id
    in the intent" keeps meaning what it says if the adapter starts claiming one
    more provider id.
    """
    async with tenant_session(str(org_id), str(user_id)) as s:
        connector = await svc.get_connector(s, org_id=org_id, connector_id=connector_id)
        intent = get_mapper(connector.provider).to_intent(
            payload, config=svc.mapper_config(connector)
        )
    assert isinstance(intent, EmissionIntent)
    return intent.object_keys


class _ExplodingChannel:
    """An SdI channel whose dispatch fails AMBIGUOUSLY.

    A plain ``RuntimeError`` is deliberately not one of the two shapes ADR-0046
    accepts as "provably nothing was filed", so the invoice is PARKED
    (transmitted, ident-less, dispatch lease kept) instead of being reverted to
    draft. That is the exact state a pod killed inside the dispatch leaves, and
    the only state in which the resume-vs-restart question has an answer worth
    testing.
    """

    name = "exploding"

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return None

    async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
        raise RuntimeError("sdi unreachable")


# --- claiming and the lease ------------------------------------------------


async def test_claim_due_leases_rows_against_every_other_claimer() -> None:
    """The lease has to hold twice over: against a worker racing us right now
    (``FOR UPDATE SKIP LOCKED``) and against one that arrives after we commit
    (the ``processing`` status). Either one failing means two pods compose two
    invoices for one payment."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    first = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_1"))
    second = await _ingest(
        org_id,
        connector_id,
        _invoice_paid(
            event_id="evt_2", invoice_id="in_2", charge_id="ch_2", payment_intent_id="pi_2"
        ),
    )

    async with tenant_session(str(org_id), str(user_id), actor_kind="system") as worker_a:
        claimed = await svc.claim_due(worker_a, org_id=org_id)
        # A second worker running CONCURRENTLY: worker_a's transaction is still
        # open, so the rows still read 'pending' in worker_b's snapshot and only
        # the row locks can stop it. SKIP LOCKED must make it walk past them
        # rather than block on them (a blocking claim would serialise the whole
        # fleet behind the slowest emission).
        async with tenant_session(str(org_id), str(user_id), actor_kind="system") as worker_b:
            # A short lock_timeout so the WRONG behaviour is a loud failure
            # instead of a hang: without SKIP LOCKED this claim would block on
            # worker_a's row locks, and worker_a cannot commit until this block
            # returns -- the test would deadlock rather than report anything.
            await worker_b.execute(text("SET LOCAL lock_timeout = '3s'"))
            concurrent = await svc.claim_due(worker_b, org_id=org_id)

    assert sorted(claimed) == sorted([first, second])
    assert concurrent == [], "a concurrent claimer must not see rows another worker holds"

    # And a worker arriving AFTER the claim committed is stopped by the status
    # change instead of by the lock.
    assert await _claim(org_id, user_id) == []

    for event_id in (first, second):
        row = await _event(org_id, user_id, event_id)
        assert row.status == "processing"
        assert row.last_attempt_at is not None, "the lease clock must start with the claim"


async def test_reclaim_expired_honours_both_sides_of_the_lease_boundary() -> None:
    """A lease that expires early re-runs an emission that is still in flight;
    one that never expires strands the event forever. Both sides are tested by
    writing ``last_attempt_at`` directly, because the real window is 10 minutes."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert await _claim(org_id, user_id) == [event_id]

    lease = get_settings().payment_connector_lease_seconds
    now = datetime.datetime.now(tz=datetime.UTC)

    # Inside the window: the worker holding it may still be mid-dispatch.
    await _force_event(
        org_id, user_id, event_id, last_attempt_at=now - datetime.timedelta(seconds=lease - 60)
    )
    assert await _reclaim(org_id, user_id) == 0
    row = await _event(org_id, user_id, event_id)
    assert row.status == "processing", "a live lease must not be broken"

    # Past the window: the holder provably died, and ADR-0051 sizes the lease
    # above the whole dispatch budget so nothing can still be in flight.
    await _force_event(
        org_id, user_id, event_id, last_attempt_at=now - datetime.timedelta(seconds=lease + 60)
    )
    assert await _reclaim(org_id, user_id) == 1
    row = await _event(org_id, user_id, event_id)
    assert row.status == "pending"
    assert row.last_error == "lease_expired"
    assert await _claim(org_id, user_id) == [event_id], "a reclaimed event must be claimable again"


async def test_abandoned_event_resumes_its_document_instead_of_emitting_a_second() -> None:
    """The whole point of the lease: a reclaimed event must land on the document
    it already produced, never on a second one with a second fiscal number."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert await _run(org_id, connector_id, event_id) == "done"

    before = await _invoices(org_id, user_id)
    assert len(before) == 1
    number = before[0].number
    assert number is not None

    # The pod died AFTER the work but BEFORE it could record the outcome: the
    # row is stuck 'processing' with a stale lease.
    lease = get_settings().payment_connector_lease_seconds
    await _force_event(
        org_id,
        user_id,
        event_id,
        status="processing",
        last_attempt_at=datetime.datetime.now(tz=datetime.UTC)
        - datetime.timedelta(seconds=lease + 60),
    )
    assert await _reclaim(org_id, user_id) == 1
    assert await _run(org_id, connector_id, event_id) == "done"

    after = await _invoices(org_id, user_id)
    assert len(after) == 1, "a reclaimed event must never mint a second document"
    assert after[0].id == before[0].id
    assert after[0].number == number, "the fiscal number must not move under a retry"
    assert (await _event(org_id, user_id, event_id)).invoice_id == before[0].id


# --- idempotency of the fiscal work ----------------------------------------


async def test_reprocessing_an_emission_event_changes_nothing() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert await _run(org_id, connector_id, event_id) == "done"

    before = await _invoices(org_id, user_id)
    links_before = await _links(org_id, user_id, connector_id)
    assert len(before) == 1

    await _force_event(org_id, user_id, event_id, status="pending")
    assert await _run(org_id, connector_id, event_id) == "done"

    after = await _invoices(org_id, user_id)
    assert [i.id for i in after] == [i.id for i in before]
    assert after[0].number == before[0].number
    assert after[0].total == before[0].total
    assert after[0].payment_status is PaymentStatus.paid
    assert [i for i in after if i.document_type is DocumentType.TD04] == [], (
        "an emission re-run must not produce a credit note"
    )
    assert await _links(org_id, user_id, connector_id) == links_before


async def test_reprocessing_a_refund_event_does_not_mint_a_second_credit_note() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    parent = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_parent"))
    assert await _run(org_id, connector_id, parent) == "done"

    refund = await _ingest(org_id, connector_id, _refund(event_id="evt_ref", amount=12200))
    assert await _run(org_id, connector_id, refund) == "done"

    notes_before = [
        i for i in await _invoices(org_id, user_id) if i.document_type is DocumentType.TD04
    ]
    assert len(notes_before) == 1
    assert notes_before[0].number is not None

    await _force_event(org_id, user_id, refund, status="pending")
    assert await _run(org_id, connector_id, refund) == "done"

    notes_after = [
        i for i in await _invoices(org_id, user_id) if i.document_type is DocumentType.TD04
    ]
    assert len(notes_after) == 1, "one reversal must never yield two credit notes"
    assert notes_after[0].id == notes_before[0].id
    assert notes_after[0].number == notes_before[0].number
    assert notes_after[0].total == notes_before[0].total


async def test_redelivered_refund_respects_credit_note_mode_draft() -> None:
    """FAILING -- reports a real defect in ``payment_connectors._settle``.

    ``credit_note_mode`` is an INDEPENDENT switch: the model documents
    "automating invoices while keeping storni manual is a legitimate and common
    posture", and ``draft`` means compose the TD04 but let a human file it.
    ``_process_credit_note`` honours that on the FIRST pass, but the
    already-linked branch delegates to ``_settle``, which decides whether to
    transmit by reading ``connector.invoice_mode`` -- the wrong switch for a
    credit note.

    So a connector configured ``invoice_mode='transmit'`` +
    ``credit_note_mode='draft'`` files the storno at SdI as soon as ANY second
    event touches it: a provider redelivery, the sibling ``credit_note.created``
    that shares the refund id, or a lease-expiry reclaim. Under at-least-once
    delivery that is not an edge case, it is the normal course of events -- and
    an SdI filing is irreversible, so the operator's review gate is bypassed for
    a document they explicitly asked to hold.

    The mirror image of the same line is a durability hole: with
    ``invoice_mode='draft'`` + ``credit_note_mode='transmit'``, ``_settle``
    returns early and NEVER resumes a credit note whose dispatch was left
    unsettled, so a reclaimed event cannot finish filing it.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(
        org_id, user_id, issuer_id, invoice_mode="transmit", credit_note_mode="draft"
    )
    parent = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_parent"))
    assert await _run(org_id, connector_id, parent) == "done"

    refund = await _ingest(org_id, connector_id, _refund(event_id="evt_ref", amount=12200))
    assert await _run(org_id, connector_id, refund) == "done"

    notes = [i for i in await _invoices(org_id, user_id) if i.document_type is DocumentType.TD04]
    assert len(notes) == 1
    assert notes[0].state is InvoiceState.draft, "first pass honours credit_note_mode='draft'"
    assert notes[0].number is None, "a held storno must not consume a fiscal number"

    # The redelivery a provider is entitled to send at any time.
    await _force_event(org_id, user_id, refund, status="pending")
    assert await _run(org_id, connector_id, refund) == "done"

    notes = [i for i in await _invoices(org_id, user_id) if i.document_type is DocumentType.TD04]
    assert len(notes) == 1
    assert notes[0].state is InvoiceState.draft, (
        "a redelivery must not file a storno the operator asked to hold"
    )
    assert notes[0].number is None


async def test_second_event_for_the_same_money_leaves_number_and_links_untouched() -> None:
    """``test_payment_connectors_service`` already proves no second invoice is
    filed. What matters for durability is the finer point: the FIRST document's
    identity and its idempotency claims must be bit-for-bit the same afterwards,
    or a later refund would resolve against a moved target."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    first = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_a"))
    assert await _run(org_id, connector_id, first) == "done"

    invoice_before = (await _invoices(org_id, user_id))[0]
    links_before = await _links(org_id, user_id, connector_id)
    assert len(links_before) == 3

    # A different provider event id naming the same invoice/charge/intent.
    second = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_b"))
    assert await _run(org_id, connector_id, second) == "done"

    invoices = await _invoices(org_id, user_id)
    assert len(invoices) == 1
    assert invoices[0].number == invoice_before.number
    assert invoices[0].issued_at == invoice_before.issued_at
    assert await _links(org_id, user_id, connector_id) == links_before, (
        "the object links are the idempotency ledger: a re-run must not rewrite them"
    )
    # Both events must point at the one document, so an operator reading either
    # one lands on the same invoice.
    assert (await _event(org_id, user_id, second)).invoice_id == invoice_before.id


async def test_object_links_are_committed_before_the_dispatch() -> None:
    """THE ordering rule of ADR-0051, tested against a dispatch that really
    fails after the pre-dispatch commit.

    A crash between "the document exists" and "the document was filed" must
    leave the link claims on disk, because they are the only thing that stops
    the retry from composing a second draft and burning a second fiscal number
    for one payment.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    payload = _invoice_paid()
    event_id = await _ingest(org_id, connector_id, payload)
    expected_keys = await _emission_keys(org_id, user_id, connector_id, payload)
    assert expected_keys, "the mapper must claim at least one provider id"

    set_channel_override(lambda: _ExplodingChannel())
    try:
        # The dispatch blows up; the fiscal identifiers and the claims were
        # already committed, so the event only defers.
        assert await _run(org_id, connector_id, event_id) == "pending"
    finally:
        set_channel_override(None)

    invoices = await _invoices(org_id, user_id)
    assert len(invoices) == 1
    parked = invoices[0]
    assert parked.number is not None, "the number was allocated before the dispatch"
    assert parked.state is InvoiceState.transmitted
    assert parked.identificativo_sdi is None
    assert parked.sdi_dispatch_started_at is not None, "an ambiguous failure keeps the lease"

    links = await _links(org_id, user_id, connector_id)
    assert {(kind, ident) for _id, kind, ident, _inv in links} == set(expected_keys)
    assert {inv for _id, _kind, _ident, inv in links} == {parked.id}

    # An immediate retry is refused rather than allowed to file twice: the SdI
    # dispatch lease is still fresh, so the invoice may be in flight.
    assert await _run(org_id, connector_id, event_id) == "pending"
    assert len(await _invoices(org_id, user_id)) == 1

    # Once that lease has expired the retry RESUMES the same document.
    await _age_dispatch_lease(org_id, user_id, parked.id)
    assert await _run(org_id, connector_id, event_id) == "done"

    settled = await _invoices(org_id, user_id)
    assert len(settled) == 1, "the resume must not compose a second draft"
    assert settled[0].id == parked.id
    assert settled[0].number == parked.number, "the fiscal number must survive the resume"
    assert settled[0].sdi_dispatch_started_at is None
    assert await _links(org_id, user_id, connector_id) == links


# --- backoff and parking ---------------------------------------------------


async def test_backoff_is_exponential_and_hides_the_event_from_the_queue() -> None:
    """A refund whose parent is not emitted yet is the canonical deferrable
    event. Each attempt must push ``next_attempt_at`` out on the documented
    curve AND actually keep ``claim_due`` from picking the row up again -- a
    backoff the claim query ignores is a hot loop against the fiscal engine."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_early", amount=100))

    settings = get_settings()
    base = datetime.timedelta(seconds=settings.payment_connector_backoff_base_seconds)

    # The stamp is ``now() + backoff`` taken INSIDE the call, so bracketing the
    # call between two clock reads pins it exactly: no sleeping, and no
    # tolerance constant that a loaded CI box can blow through.
    before = datetime.datetime.now(tz=datetime.UTC)
    assert await _run(org_id, connector_id, event_id) == "pending"
    after = datetime.datetime.now(tz=datetime.UTC)
    row = await _event(org_id, user_id, event_id)
    assert row.attempt_count == 1
    assert row.last_error == "parent_not_emitted"
    assert before + base <= row.next_attempt_at <= after + base

    assert await _claim(org_id, user_id) == [], "a backed-off event must be invisible to claim_due"

    before = datetime.datetime.now(tz=datetime.UTC)
    assert await _run(org_id, connector_id, event_id) == "pending"
    after = datetime.datetime.now(tz=datetime.UTC)
    row = await _event(org_id, user_id, event_id)
    assert row.attempt_count == 2
    # base * 2**(attempt-1): the second wait is twice the first, not a repeat.
    assert before + 2 * base <= row.next_attempt_at <= after + 2 * base

    # The curve is capped, so a long-lived failure cannot push an event years
    # into the future and effectively lose it.
    assert svc.backoff_seconds(1_000) == settings.payment_connector_backoff_cap_seconds


async def test_event_dies_once_the_attempt_budget_is_spent() -> None:
    """The alternative to parking is an event that retries forever against a
    condition that will never change, which is a permanent load source and an
    operator queue nobody can see."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _refund(event_id="evt_orphan", amount=100))
    # The shipped budget is 10 attempts; drive it from the row so the test does
    # not depend on ten round trips.
    await _force_event(org_id, user_id, event_id, max_attempts=2)

    assert await _run(org_id, connector_id, event_id) == "pending"
    assert (await _event(org_id, user_id, event_id)).attempt_count == 1

    assert await _run(org_id, connector_id, event_id) == "dead"
    row = await _event(org_id, user_id, event_id)
    assert row.attempt_count == 2
    assert row.status == "dead"
    assert row.last_error == "parent_not_emitted"
    assert row.processed_at is not None
    assert row.invoice_id is None
    assert await _claim(org_id, user_id) == [], "a dead event must leave the work queue"


async def test_retry_event_rearms_dead_and_parked_rows_only() -> None:
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    # A dead row: the orphan refund above, driven to exhaustion.
    dead = await _ingest(org_id, connector_id, _refund(event_id="evt_dead", amount=100))
    await _force_event(org_id, user_id, dead, max_attempts=1)
    assert await _run(org_id, connector_id, dead) == "dead"

    # A parked row: a counterpart with no billing data waits in its OWN state,
    # not in the operator queue -- nothing is broken, the customer simply has
    # not filled the form in. Retry must re-arm it just the same.
    parked = await _ingest(
        org_id,
        connector_id,
        _invoice_paid(
            event_id="evt_parked",
            invoice_id="in_p",
            charge_id="ch_p",
            payment_intent_id="pi_p",
            metadata={},
        ),
    )
    assert await _run(org_id, connector_id, parked) == "no_billing_data"

    async with tenant_session(str(org_id), str(user_id)) as s:
        for event_id in (dead, parked):
            row = await svc.retry_event(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                event_id=event_id,
            )
            assert row.status == "pending"
            assert row.attempt_count == 0, "the old attempts measured a condition that is gone"
            assert row.last_error is None
            assert row.error_detail is None

    assert sorted(await _claim(org_id, user_id)) == sorted([dead, parked])

    # An in-flight row is NOT re-armable: doing so would race the worker that
    # holds its lease, which is how one payment gets invoiced twice.
    with pytest.raises(ConflictError):
        async with tenant_session(str(org_id), str(user_id)) as s:
            await svc.retry_event(
                s,
                org_id=org_id,
                actor_id=user_id,
                connector_id=connector_id,
                event_id=dead,
            )


# --- RLS / tenant isolation ------------------------------------------------


async def _seed_tenant() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A whole tenant with a row in every payment table.

    Returns ``(org_id, user_id, connector_id, event_id)``. The emission is run
    for real so ``payment_object_links`` and ``payment_customer_links`` carry
    genuine rows rather than hand-inserted ones -- an isolation test over rows
    the service never wrote would prove nothing about the service.
    """
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _invoice_paid())
    assert await _run(org_id, connector_id, event_id) == "done"
    async with tenant_session(str(org_id), str(user_id)) as s:
        await svc.record_delivery(
            s,
            org_id=org_id,
            connector_id=connector_id,
            provider="stripe",
            outcome="accepted",
            http_status=200,
            raw_body=b"{}",
            signature_present=True,
            api_key_present=False,
            provider_event_id="evt_1",
            event_id=event_id,
        )
    return org_id, user_id, connector_id, event_id


async def test_payment_tables_are_isolated_between_orgs() -> None:
    org_a, user_a, connector_a, event_a = await _seed_tenant()
    org_b, user_b, connector_b, event_b = await _seed_tenant()

    async def _visible(org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, set[uuid.UUID]]:
        async with tenant_session(str(org_id), str(user_id)) as s:
            return {
                "connectors": set((await s.execute(select(PaymentConnector.id))).scalars().all()),
                "events": set((await s.execute(select(PaymentConnectorEvent.id))).scalars().all()),
                "links": set(
                    (await s.execute(select(PaymentObjectLink.connector_id))).scalars().all()
                ),
                "deliveries": set(
                    (await s.execute(select(PaymentWebhookDelivery.connector_id))).scalars().all()
                ),
                "customers": set(
                    (await s.execute(select(PaymentCustomerLink.connector_id))).scalars().all()
                ),
            }

    seen_a = await _visible(org_a, user_a)
    seen_b = await _visible(org_b, user_b)

    assert seen_a["connectors"] == {connector_a}
    assert seen_a["events"] == {event_a}
    assert seen_a["links"] == {connector_a}
    assert seen_a["deliveries"] == {connector_a}
    assert seen_a["customers"] == {connector_a}

    assert seen_b["connectors"] == {connector_b}
    assert seen_b["events"] == {event_b}
    assert seen_b["links"] == {connector_b}
    assert seen_b["deliveries"] == {connector_b}
    assert seen_b["customers"] == {connector_b}

    # And the service's own explicit org filter agrees with the policy: B's
    # connector id is simply not found from A.
    async with tenant_session(str(org_a), str(user_a)) as s:
        assert await svc.list_events(s, org_id=org_a, connector_id=connector_b) == []


#: Every table this subsystem owns. Counted by name rather than through the ORM
#: because a policy hole is a SQL-level fact: an ORM read could be narrowed by a
#: WHERE clause the policy never enforced, and would then pass while the table
#: was in fact readable.
_PAYMENT_TABLES = (
    "payment_connectors",
    "payment_connector_events",
    "payment_object_links",
    "payment_webhook_deliveries",
    "payment_customer_links",
)


async def _no_tenant_counts(actor_kind: ActorKind) -> dict[str, int]:
    async with admin_session(actor_kind=actor_kind) as s:
        return {
            table: (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            for table in _PAYMENT_TABLES
        }


async def test_payment_tables_fail_closed_without_a_tenant_guc() -> None:
    """No ``app.current_org`` means no rows. The connector table is
    ENABLE-but-not-FORCE RLS so the SECURITY DEFINER resolver can read it, and
    that asymmetry must not turn into a hole for an ordinary no-tenant session:
    the resolver is the ONLY door.

    Both no-tenant actor kinds are checked. ``system`` is the one the worker
    really uses to enumerate workspaces (migration 0029 opens exactly three
    enumeration tables to it, none of them these), and ``human_direct`` is the
    stricter case that must see nothing anywhere.
    """
    org_id, _user_id, connector_id, _event_id = await _seed_tenant()

    for actor_kind in ("system", "human_direct"):
        counts = await _no_tenant_counts(actor_kind)
        assert counts == dict.fromkeys(_PAYMENT_TABLES, 0), (
            f"a no-tenant {actor_kind} session read {counts}"
        )

    # The sanctioned door still works from the same no-tenant session, which is
    # what makes the unauthenticated ingress possible at all: fail-closed must
    # not mean the ingress cannot resolve its own tenant.
    async with admin_session() as s:
        resolved = await svc.resolve_for_ingress(s, connector_id=connector_id)
    assert resolved is not None
    assert resolved.org_id == org_id
    assert resolved.enabled is True


# --- client resolution -----------------------------------------------------


async def _client_tag_ids(org_id: uuid.UUID, user_id: uuid.UUID) -> set[uuid.UUID]:
    async with tenant_session(str(org_id), str(user_id)) as s:
        invoices = await inv_svc.list_invoices(s, org_id=org_id)
    return {i.client_tag_id for i in invoices}


async def _profiles_for_vat(org_id: uuid.UUID, user_id: uuid.UUID, *vats: str) -> list[uuid.UUID]:
    async with tenant_session(str(org_id), str(user_id)) as s:
        return list(
            (
                await s.execute(
                    select(ClientProfile.tag_id).where(ClientProfile.vat_number.in_(vats))
                )
            )
            .scalars()
            .all()
        )


async def _customer_links(
    org_id: uuid.UUID, user_id: uuid.UUID, connector_id: uuid.UUID
) -> list[tuple[str, uuid.UUID]]:
    async with tenant_session(str(org_id), str(user_id)) as s:
        return list(
            (
                await s.execute(
                    select(
                        PaymentCustomerLink.provider_customer_id,
                        PaymentCustomerLink.client_tag_id,
                    ).where(PaymentCustomerLink.connector_id == connector_id)
                )
            )
            .tuples()
            .all()
        )


async def test_two_events_for_one_customer_resolve_to_one_client() -> None:
    """The identity map exists because ``resolve_or_create_client`` dedupes with
    a SELECT-then-INSERT that no unique constraint backs: two payments from one
    customer must not end up as two client tags with two per-client sezionali."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    first = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_1"))
    assert await _run(org_id, connector_id, first) == "done"
    second = await _ingest(
        org_id,
        connector_id,
        _invoice_paid(
            event_id="evt_2",
            invoice_id="in_2",
            charge_id="ch_2",
            payment_intent_id="pi_2",
        ),
    )
    assert await _run(org_id, connector_id, second) == "done"

    invoices = await _invoices(org_id, user_id)
    assert len(invoices) == 2, "two distinct payments are two distinct documents"
    tags = await _client_tag_ids(org_id, user_id)
    assert len(tags) == 1, "both documents must name the same counterpart"

    links = await _customer_links(org_id, user_id, connector_id)
    assert links == [("cus_1", next(iter(tags)))]
    assert len(await _profiles_for_vat(org_id, user_id, "09876543210")) == 1, (
        "one VAT must not end up on two client cards"
    )


async def test_changed_fiscal_data_does_not_fork_the_client() -> None:
    """A provider customer whose card is edited (new legal name, new address,
    even a corrected VAT) is still the SAME counterpart. Keying on the provider
    customer id is what makes that true; keying on the fiscal identity alone
    would fork the client and split its invoice series."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)

    first = await _ingest(org_id, connector_id, _invoice_paid(event_id="evt_1"))
    assert await _run(org_id, connector_id, first) == "done"

    second = await _ingest(
        org_id,
        connector_id,
        _invoice_paid(
            event_id="evt_2",
            invoice_id="in_2",
            charge_id="ch_2",
            payment_intent_id="pi_2",
            customer_name="Acme Group SpA",
            city="Torino",
            metadata={"vat_number": "IT11122233344"},
        ),
    )
    assert await _run(org_id, connector_id, second) == "done"

    tags = await _client_tag_ids(org_id, user_id)
    assert len(tags) == 1, "an edited customer card must not create a second client"
    assert await _customer_links(org_id, user_id, connector_id) == [("cus_1", next(iter(tags)))]
    # The second, corrected VAT was never used to mint anything: the link short
    # circuits before ``resolve_or_create_client`` is consulted at all.
    assert await _profiles_for_vat(org_id, user_id, "11122233344") == []
    assert len(await _profiles_for_vat(org_id, user_id, "09876543210")) == 1


async def test_emitted_totals_survive_a_reclaim_and_a_resume() -> None:
    """A belt-and-braces amount check across the whole durability story: a
    payment that was claimed, abandoned, reclaimed and resumed must still bill
    exactly what the provider reported, once."""
    org_id, user_id, issuer_id = await _org_and_issuer()
    connector_id = await _connector(org_id, user_id, issuer_id)
    event_id = await _ingest(org_id, connector_id, _invoice_paid())

    assert await _claim(org_id, user_id) == [event_id]
    lease = get_settings().payment_connector_lease_seconds
    await _force_event(
        org_id,
        user_id,
        event_id,
        last_attempt_at=datetime.datetime.now(tz=datetime.UTC)
        - datetime.timedelta(seconds=lease + 60),
    )
    assert await _reclaim(org_id, user_id) == 1
    assert await _claim(org_id, user_id) == [event_id]
    assert await _run(org_id, connector_id, event_id) == "done"

    invoices = await _invoices(org_id, user_id)
    assert len(invoices) == 1
    assert invoices[0].taxable == Decimal("100.00")
    assert invoices[0].vat == Decimal("22.00")
    assert invoices[0].total == Decimal("122.00")
    assert invoices[0].document_type is DocumentType.TD01
