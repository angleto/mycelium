"""Two-phase durable transmit (ADR-0046, task b6a0df8f) -- the T17 family.

The fiscal invariant under test: once a byte MAY have reached SdI, the DB
always knows the file's identity (numero, ProgressivoInvio, NomeFile, frozen
XML), a retry re-sends the SAME bytes under the SAME name (colliding with
SdI's NomeFile dedupe instead of double-filing), and a lost sync ACK is
reconciled from the inbound notification via the NomeFile fallback.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import ConservationStatus, InvoiceState, SdiStatus
from mycelium_core.sdi_channel import IntermediaryIdentity, TransmitResult
from mycelium_core.services import invoice as inv
from mycelium_core.services import sdi_mandate
from mycelium_core.services.auth import signup
from mycelium_core.services.sdi_inbound import ParsedNotification
from mycelium_core.services.system_settings import set_sdi_environment
from mycelium_core.services.taxonomy import ClientInput, create_client


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="T17")
    return r.org_id, r.user_id


async def _setup(s, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    profile = await inv.create_issuer_profile(
        s,
        org_id=org,
        actor_id=user,
        label="Principale",
        legal_name="Acme Srl",
        vat_number="01234567890",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
        is_default=True,
    )
    await sdi_mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=profile.id)
    client = await create_client(
        s,
        org_id=org,
        actor_id=user,
        name="Client SpA",
        profile=ClientInput(
            legal_name="Client SpA",
            country_code="IT",
            vat_number="09876543210",
            sdi_code="ABCDEFG",
            address="Via Milano 2",
            postal_code="20100",
            city="Milano",
            province="MI",
        ),
    )
    return client.id


async def _draft(s, org: uuid.UUID, user: uuid.UUID, client_id: uuid.UUID) -> uuid.UUID:
    d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
    await inv.add_line(
        s,
        org_id=org,
        actor_id=user,
        invoice_id=d.id,
        description="svc",
        unit_price=Decimal(100),
    )
    return d.id


class FlakyCoop:
    """A fake SdICoop channel with a scripted failure sequence: each call
    pops the next entry from ``script`` (an exception to raise AFTER
    recording the send, or None for success). Records every (filename, xml)
    it was asked to send -- the lost-ACK scenarios are exactly 'the channel
    delivered, the response was lost'."""

    name = "sdicoop"

    def __init__(self, script: list[Exception | None]) -> None:
        self.script = list(script)
        self.sent: list[tuple[str, str]] = []

    @property
    def intermediary(self) -> IntermediaryIdentity | None:
        return IntermediaryIdentity(country_code="IT", fiscal_code="11122233344")

    async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
        self.sent.append((filename, xml))
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        return TransmitResult(
            identificativo_sdi=f"SDI{uuid.uuid4().hex[:10].upper()}",
            conservation=ConservationStatus.ade_pending,
            channel=self.name,
        )


async def _expire_lease(org: uuid.UUID, user: uuid.UUID, invoice_id: uuid.UUID) -> None:
    """Age the dispatch lease past expiry (the tests must not sleep)."""
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text(
                "UPDATE invoices SET sdi_dispatch_started_at = now() - interval '1 hour' "
                "WHERE id = :iid"
            ),
            {"iid": str(invoice_id)},
        )


async def _fresh(org: uuid.UUID, user: uuid.UUID, invoice_id: uuid.UUID):
    async with tenant_session(str(org), str(user)) as s:
        return await inv.get_invoice(s, org_id=org, invoice_id=invoice_id)


async def test_t17a_ambiguous_failure_parks_durable_identity_and_retry_resends_same_file() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack"), None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)

    # Attempt 1: the file leaves, the ACK is lost -> unconfirmed conflict.
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_UNCONFIRMED

    # The identity of the possibly-filed document is durable (fresh session).
    parked = await _fresh(org, user, iid)
    assert parked.state is InvoiceState.transmitted
    assert parked.identificativo_sdi is None
    assert parked.progressivo_invio is not None
    assert parked.nome_file is not None
    assert parked.xml is not None
    assert parked.sdi_dispatch_started_at is not None
    assert parked.number is not None

    # Retry after lease expiry: SAME file name, byte-identical XML, success.
    await _expire_lease(org, user, iid)
    async with tenant_session(str(org), str(user)) as s:
        done = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        assert done.identificativo_sdi is not None
        assert done.sdi_dispatch_started_at is None
    assert len(ch.sent) == 2
    assert ch.sent[0][0] == ch.sent[1][0] == parked.nome_file
    assert ch.sent[0][1] == ch.sent[1][1]  # byte-identical resend


async def test_t17b_definite_failure_reverts_to_draft_keeping_identity_for_reuse() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ConnectError("refused"), None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)

    # Connection never established: provably nothing at SdI -> back to draft,
    # surfacing the transport error as-is.
    with pytest.raises(httpx.ConnectError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)

    reverted = await _fresh(org, user, iid)
    assert reverted.state is InvoiceState.draft
    assert reverted.xml is None
    assert reverted.sdi_dispatch_started_at is None
    assert reverted.nome_file is not None  # identity kept for verbatim reuse
    kept_name, kept_number = reverted.nome_file, reverted.number
    assert kept_number is not None

    # The next transmit reuses the burned identity verbatim.
    async with tenant_session(str(org), str(user)) as s:
        done = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        assert done.nome_file == kept_name
        assert done.number == kept_number
        assert done.identificativo_sdi is not None
    assert [f for f, _ in ch.sent] == [kept_name, kept_name]


async def test_t17b2_definite_failure_on_retry_leg_stays_parked() -> None:
    # Attempt 1 ambiguous (may have filed) -> attempt 2 ConnectError: the
    # definite verdict covers only attempt 2, so the invoice must NOT revert
    # to an editable draft (the frozen XML is the only copy of what attempt 1
    # may have filed).
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack"), httpx.ConnectError("refused")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    await _expire_lease(org, user, iid)
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_UNCONFIRMED
    parked = await _fresh(org, user, iid)
    assert parked.state is InvoiceState.transmitted
    assert parked.xml is not None


async def test_t17c_fresh_lease_blocks_concurrent_retry() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    # The lease is fresh: a second transmit is refused as in-progress.
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_IN_PROGRESS
    assert len(ch.sent) == 1  # nothing re-dispatched


async def test_t17c2_stale_identity_map_read_does_not_bypass_the_lease() -> None:
    # The reviewer scenario: the invoice is pre-loaded (unlocked) into the
    # SAME session that then transmits -- the FOR UPDATE re-read must see the
    # committed parked state, not the cached draft.
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            stale = await inv.get_invoice(s, org_id=org, invoice_id=iid)  # caches the row
            assert stale is not None
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_IN_PROGRESS
    assert len(ch.sent) == 1


async def test_t17d_counters_stay_durable_no_identity_collision() -> None:
    # After A's ambiguous failure, B (same trasmittente) must draw a FRESH
    # progressivo/nome_file -- the failed dispatch may have filed A's name.
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack"), None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid_a = await _draft(s, org, user, client_id)
        iid_b = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid_a, channel=ch)
    async with tenant_session(str(org), str(user)) as s:
        b = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid_b, channel=ch)
    a = await _fresh(org, user, iid_a)
    assert a.nome_file is not None and b.nome_file is not None
    assert a.nome_file != b.nome_file
    assert a.number != b.number


async def test_t17e_lost_ack_reconciled_from_inbound_rc_by_filename() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    parked = await _fresh(org, user, iid)
    assert parked.identificativo_sdi is None and parked.nome_file is not None

    # The original filing's RC arrives with an ident we never saw: the
    # NomeFile fallback correlates it and the invoice adopts the ident.
    parsed = ParsedNotification(
        outcome="RC",
        identificativo_sdi="424242001",
        message_id="M0001",
        file_name=parked.nome_file,
        esito=None,
        raw_xml=b"<RicevutaConsegna/>",
    )
    async with tenant_session(str(org), str(user)) as s:
        out = await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed)
        assert out.identificativo_sdi == "424242001"
        assert out.state is InvoiceState.delivered
        assert out.sdi_status is SdiStatus.RC


async def test_t17f_duplicate_echo_ns_00002_does_not_reject_but_00404_does() -> None:
    org, user = await _org()
    ch = FlakyCoop([None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
        sent = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        ident = sent.identificativo_sdi
        nome = sent.nome_file
    assert ident is not None and nome is not None

    echo_xml = (
        b"<NotificaScarto><ListaErrori><Errore><Codice>00002</Codice>"
        b"<Descrizione>Nome file duplicato</Descrizione></Errore></ListaErrori>"
        b"</NotificaScarto>"
    )
    # NS 00002 under a DIFFERENT ident (the resend's echo): archived, no
    # state transition.
    parsed = ParsedNotification(
        outcome="NS",
        identificativo_sdi="424242777",
        message_id="M0002",
        file_name=nome,
        esito=None,
        raw_xml=echo_xml,
    )
    async with tenant_session(str(org), str(user)) as s:
        await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed)
    after = await _fresh(org, user, iid)
    assert after.state is InvoiceState.transmitted  # unchanged
    assert after.identificativo_sdi == ident  # no adoption from an echo

    # A genuine 00404 (fattura duplicata) on the OWN filing must reject.
    scarto_xml = (
        b"<NotificaScarto><ListaErrori><Errore><Codice>00404</Codice>"
        b"<Descrizione>Fattura duplicata</Descrizione></Errore></ListaErrori>"
        b"</NotificaScarto>"
    )
    parsed2 = ParsedNotification(
        outcome="NS",
        identificativo_sdi=ident,
        message_id="M0003",
        file_name=nome,
        esito=None,
        raw_xml=scarto_xml,
    )
    async with tenant_session(str(org), str(user)) as s:
        await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed2)
    rejected = await _fresh(org, user, iid)
    assert rejected.state is InvoiceState.rejected


async def test_t17h_env_flip_between_attempts_refuses_the_retry() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack"), None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    await _expire_lease(org, user, iid)
    async with admin_session() as s:
        await set_sdi_environment(s, "production")
    try:
        with pytest.raises(ConflictError) as exc:
            async with tenant_session(str(org), str(user)) as s:
                await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        assert exc.value.code is MessageCode.INVOICE_TRANSMIT_ENV_CHANGED
        assert len(ch.sent) == 1  # nothing re-dispatched into the new env
    finally:
        async with admin_session() as s:
            await set_sdi_environment(s, "test")


async def test_t17i_manual_export_success_is_settled_not_retryable() -> None:
    # ManualExportChannel succeeds with a NULL identificativo_sdi: the lease
    # is cleared, so the invoice is settled, NOT retryable.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
        sent = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid)
        assert sent.identificativo_sdi is None  # manual export has no SdI id
        assert sent.sdi_dispatch_started_at is None
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid)
    assert exc.value.code is MessageCode.INVOICE_NOT_DRAFT


async def test_t17j_credit_note_refused_while_parent_unsettled() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.create_credit_note(s, org_id=org, actor_id=user, parent_invoice_id=iid)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_IN_PROGRESS


async def test_t17k_series_locked_once_number_allocated() -> None:
    # A definite-fail revert keeps number + nome_file on a draft: moving the
    # draft to another sezionale would re-use the number in another sequence.
    org, user = await _org()
    ch = FlakyCoop([httpx.ConnectError("refused")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(httpx.ConnectError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.update_draft(
                s, org_id=org, actor_id=user, invoice_id=iid, values={"series": "ZZ"}
            )


async def test_t17e2_reconcile_clears_the_dispatch_lease() -> None:
    # A notification that settles the invoice must also end the unsettled-
    # dispatch marker, or the SPA/API keep advertising a retry forever.
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    parked = await _fresh(org, user, iid)
    assert parked.sdi_dispatch_started_at is not None
    parsed = ParsedNotification(
        outcome="RC",
        identificativo_sdi="424242002",
        message_id="M0004",
        file_name=parked.nome_file,
        esito=None,
        raw_xml=b"<RicevutaConsegna/>",
    )
    async with tenant_session(str(org), str(user)) as s:
        await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed)
    settled = await _fresh(org, user, iid)
    assert settled.state is InvoiceState.delivered
    assert settled.sdi_dispatch_started_at is None


async def test_t17f2_same_ident_00002_without_resend_is_a_genuine_scarto() -> None:
    # A pure-00002 NS on the RECORDED ident with NO resend on file (e.g. a
    # name burned by a pre-ADR-0046 rollback) is a real rejection: it must
    # reject, or the 5-day correction window is silently missed.
    org, user = await _org()
    ch = FlakyCoop([None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
        sent = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        ident, nome = sent.identificativo_sdi, sent.nome_file
    assert ident is not None
    echo_xml = (
        b"<NotificaScarto><ListaErrori><Errore><Codice>00002</Codice>"
        b"<Descrizione>Nome file duplicato</Descrizione></Errore></ListaErrori>"
        b"</NotificaScarto>"
    )
    parsed = ParsedNotification(
        outcome="NS",
        identificativo_sdi=ident,
        message_id="M0005",
        file_name=nome,
        esito=None,
        raw_xml=echo_xml,
    )
    async with tenant_session(str(org), str(user)) as s:
        await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed)
    after = await _fresh(org, user, iid)
    assert after.state is InvoiceState.rejected  # NOT swallowed


async def test_t17f3_same_ident_00002_after_a_real_resend_is_swallowed() -> None:
    # Ambiguous attempt 1 (original may have filed), retry succeeds sync
    # (ident recorded = the RESEND's): the resend's own NS 00002 echo must
    # not reject the invoice -- sdi_resent_at is the persisted proof.
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack"), None])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    await _expire_lease(org, user, iid)
    async with tenant_session(str(org), str(user)) as s:
        done = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
        ident, nome = done.identificativo_sdi, done.nome_file
    resent = await _fresh(org, user, iid)
    assert resent.sdi_resent_at is not None
    echo_xml = (
        b"<NotificaScarto><ListaErrori><Errore><Codice>00002</Codice>"
        b"<Descrizione>Nome file duplicato</Descrizione></Errore></ListaErrori>"
        b"</NotificaScarto>"
    )
    parsed = ParsedNotification(
        outcome="NS",
        identificativo_sdi=ident,
        message_id="M0006",
        file_name=nome,
        esito=None,
        raw_xml=echo_xml,
    )
    async with tenant_session(str(org), str(user)) as s:
        await inv.ingest_active_notification(s, org_id=org, actor_id=user, parsed=parsed)
    after = await _fresh(org, user, iid)
    assert after.state is InvoiceState.transmitted  # echo archived, no scarto


async def test_t17l_trash_refused_while_dispatch_unsettled() -> None:
    org, user = await _org()
    ch = FlakyCoop([httpx.ReadTimeout("lost ack")])
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        iid = await _draft(s, org, user, client_id)
    with pytest.raises(ConflictError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=iid, channel=ch)
    with pytest.raises(ConflictError) as exc:
        async with tenant_session(str(org), str(user)) as s:
            await inv.soft_delete_invoice(s, org_id=org, actor_id=user, invoice_id=iid)
    assert exc.value.code is MessageCode.INVOICE_TRANSMIT_IN_PROGRESS
