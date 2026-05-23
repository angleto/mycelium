"""F7 electronic invoicing (DB-backed), ADR-0009/0010/0011 + FR-9.

The legally load-bearing invariants: concurrency-safe progressive
numbering (no gaps/dups, allocated only at transmit), immutability
after emission, deterministic FatturaPA XML + arithmetic, TD04 credit
note linkage, AdE-conservation coverage, SdI receipt correlation,
cross-org isolation.
"""

from __future__ import annotations

import asyncio
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from decimal import Decimal

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import ConflictError, DomainError, NotFoundError
from flow_core.models.invoice import ConservationStatus, InvoiceState, SdiStatus
from flow_core.sdi_channel import IntermediaryIdentity, TransmitResult, set_channel_override
from flow_core.services import invoice as inv
from flow_core.services.auth import signup
from flow_core.services.taxonomy import ClientInput, create_client


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="INV")
    return r.org_id, r.user_id


async def _setup(s, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    await inv.create_issuer_profile(
        s,
        org_id=org,
        actor_id=user,
        label="Principale",
        denominazione="Acme Srl",
        piva="01234567890",
        indirizzo="Via Roma 1",
        cap="00100",
        comune="Roma",
        is_default=True,
    )
    client = await create_client(
        s,
        org_id=org,
        actor_id=user,
        name="Client SpA",
        profile=ClientInput(
            ragione_sociale="Client SpA",
            id_paese="IT",
            id_codice="09876543210",
            codice_destinatario="ABCDEFG",
            indirizzo="Via Milano 2",
            cap="20100",
            comune="Milano",
            provincia="MI",
        ),
    )
    return client.id


async def test_numbering_is_concurrency_safe_and_allocated_at_transmit() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        ids: list[uuid.UUID] = []
        for _ in range(5):
            d = await inv.create_draft(
                s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026
            )
            await inv.add_line(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                description="svc",
                unit_price=Decimal(100),
            )
            assert d.number is None  # not allocated until transmit
            ids.append(d.id)

    async def _tx(iid: uuid.UUID) -> int:
        async with tenant_session(str(org), str(user)) as s2:
            inv2 = await inv.transmit(s2, org_id=org, actor_id=user, invoice_id=iid)
            assert inv2.number is not None
            return inv2.number

    numbers = await asyncio.gather(*[_tx(i) for i in ids])
    assert sorted(numbers) == [1, 2, 3, 4, 5]  # no gaps, no duplicates


async def test_numbering_counter_is_keyed_per_issuer() -> None:
    # The progressive number belongs to the cedente (DPR 633/72 art.21): two
    # issuer profiles draw INDEPENDENT sequences (both start at 1), where a
    # single org-wide counter would have interleaved them. Exercised at the
    # allocation seam directly so it neither depends on nor (by emitting two
    # invoices that share a number) collides with the per-issuer invoice-number
    # unique constraint.
    org, user = await _org()
    issuer_a, issuer_b = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(str(org), str(user)) as s:
        a1 = await inv._allocate_number(
            s, org_id=org, issuer_profile_id=issuer_a, series="A", year=2026
        )
        b1 = await inv._allocate_number(
            s, org_id=org, issuer_profile_id=issuer_b, series="A", year=2026
        )
        a2 = await inv._allocate_number(
            s, org_id=org, issuer_profile_id=issuer_a, series="A", year=2026
        )
        # a different series for the same issuer is its own sezionale
        a_sb = await inv._allocate_number(
            s, org_id=org, issuer_profile_id=issuer_a, series="B", year=2026
        )
    assert (a1, b1, a2, a_sb) == (1, 1, 2, 1)


async def test_series_defaults_to_per_client_sezionale() -> None:
    # A draft created without an explicit series defaults to the client's own
    # sezionale (derived from the name, unique per org): each client gets an
    # independent sequence, and the code is stable across that client's drafts.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)  # "Client SpA"
        other = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="Other",
            profile=ClientInput(
                ragione_sociale="Beta Group", id_paese="IT", id_codice="09876543210"
            ),
        )
        d1 = await inv.create_draft(
            s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026
        )
        d2 = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=other.id, year=2026)
        d1b = await inv.create_draft(
            s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026
        )
    assert d1.series == "CLIENTSP"  # derived from "Client SpA", no longer "A"
    assert d2.series == "BETAGROU"  # distinct client -> distinct sezionale
    assert d1b.series == "CLIENTSP"  # stable: same client reuses its series


async def test_manual_export_filename_progressivo_is_max_5_alnum() -> None:
    # SdI file-name progressivo is max 5 alphanumeric chars (Specifiche SDI):
    # the self-submission path must draw the per-trasmittente sequence, never
    # a 9-char f"{year}{number}". The default ManualExportChannel discards the
    # filename, so a recording channel (intermediary=None -> manual path)
    # captures what would be sent.
    captured: dict[str, str] = {}

    class RecordingManual:
        name = "manual_export"

        @property
        def intermediary(self) -> IntermediaryIdentity | None:
            return None

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
            captured["filename"] = filename
            return TransmitResult(
                identificativo_sdi=None,
                conservation=ConservationStatus.out_of_coverage,
                channel=self.name,
            )

    set_channel_override(RecordingManual)
    try:
        org, user = await _org()
        async with tenant_session(str(org), str(user)) as s:
            client_id = await _setup(s, org, user)
            d = await inv.create_draft(
                s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026
            )
            await inv.add_line(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                description="svc",
                unit_price=Decimal(100),
            )
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
    finally:
        set_channel_override(None)

    name = captured["filename"]
    assert name.endswith(".xml")
    progressivo = name.removesuffix(".xml").split("_")[-1]
    assert 1 <= len(progressivo) <= 5
    assert progressivo.isalnum()


async def test_immutable_after_emission_and_xml_is_valid() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="consulting",
            unit_price=Decimal("100.00"),
            quantity=Decimal(2),
            vat_rate=Decimal(22),
        )
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="setup",
            unit_price=Decimal("50.00"),
            quantity=Decimal(1),
            vat_rate=Decimal(10),
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        # Arithmetic: 200 @22 + 50 @10 -> taxable 250, vat 49, total 299.
        assert tx.taxable == Decimal("250.00")
        assert tx.vat == Decimal("49.00")
        assert tx.total == Decimal("299.00")
        assert tx.state is InvoiceState.transmitted
        # Manual export => outside AdE free conservation (ADR-0010).
        assert tx.conservation_status is ConservationStatus.out_of_coverage
        # Immutable after emission (ADR-0009).
        with pytest.raises(ConflictError):
            await inv.add_line(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                description="late",
                unit_price=Decimal(1),
            )
        with pytest.raises(ConflictError):
            await inv.delete_draft(s, org_id=org, actor_id=user, invoice_id=d.id)
        root = ET.fromstring(tx.xml or "")  # noqa: S314 (our own generated XML)
        assert root.tag.endswith("FatturaElettronica")
        assert root.attrib["versione"] == "FPR12"
        assert root.findtext(".//FormatoTrasmissione") == "FPR12"
        assert root.findtext(".//CedentePrestatore//Denominazione") == "Acme Srl"
        assert root.findtext(".//ImportoTotaleDocumento") == "299.00"
        assert len(root.findall(".//DettaglioLinee")) == 2
        assert len(root.findall(".//DatiRiepilogo")) == 2


async def test_td04_credit_note_links_parent() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        parent = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        note = await inv.create_credit_note(
            s, org_id=org, actor_id=user, parent_invoice_id=parent.id
        )
        assert note.document_type.value == "TD04"
        assert note.parent_invoice_id == parent.id
        lines = await inv.list_lines(s, org_id=org, invoice_id=note.id)
        assert len(lines) == 1
        ntx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=note.id)
        assert ntx.number == 2  # shares the parent's (issuer, series, year) sequence
        assert "TD04" in (ntx.xml or "")
        # DatiFattureCollegate links the parent by its FISCAL number + date,
        # never the internal UUID (the bug this guards against).
        assert "<DatiFattureCollegate>" in (ntx.xml or "")
        assert f"<IdDocumento>{parent.series}-{parent.number}</IdDocumento>" in (ntx.xml or "")
        assert str(parent.id) not in (ntx.xml or "")


@pytest.fixture
def _sdicoop() -> Iterator[None]:
    class FakeCoop:
        name = "sdicoop"

        @property
        def intermediary(self) -> IntermediaryIdentity | None:
            return IntermediaryIdentity(
                id_paese="IT", id_codice="11122233344", denominazione="Flow Intermediary Srl"
            )

        async def transmit(self, *, xml: str, invoice_id: str, filename: str) -> TransmitResult:
            return TransmitResult(
                identificativo_sdi=f"SDI{invoice_id[:8].upper()}",
                conservation=ConservationStatus.ade_pending,
                channel=self.name,
            )

    set_channel_override(FakeCoop)
    try:
        yield
    finally:
        set_channel_override(None)


async def test_sdicoop_assigns_identificativo_and_receipt_correlation(
    _sdicoop: None,
) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        # The SdICoop (intermediary) path requires an active mandate.
        from flow_core.services import sdi_mandate

        issuer = await inv.get_default_issuer_profile(s, org_id=org)
        assert issuer is not None
        await sdi_mandate.grant_mandate(s, org_id=org, actor_id=user, issuer_profile_id=issuer.id)
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert tx.identificativo_sdi is not None
        assert tx.conservation_status is ConservationStatus.ade_pending
        # Flow stamped itself as terzo intermediario / soggetto emittente (TZ).
        assert "<SoggettoEmittente>TZ</SoggettoEmittente>" in (tx.xml or "")
        # RC receipt: delivered + AdE-covered (it transited SdI).
        rc = await inv.ingest_receipt(
            s,
            org_id=org,
            actor_id=user,
            identificativo_sdi=tx.identificativo_sdi,
            outcome="RC",
        )
        assert rc.sdi_status is SdiStatus.RC
        assert rc.state is InvoiceState.delivered
        assert rc.conservation_status is ConservationStatus.ade_covered


async def test_validation_and_isolation() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # No fiscal profile yet.
        client = await create_client(
            s,
            org_id=org,
            actor_id=user,
            name="C",
            profile=ClientInput(
                ragione_sociale="C",
                id_paese="IT",
                id_codice="11111111111",
                codice_destinatario="0000000",
            ),
        )
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client.id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="x",
            unit_price=Decimal(1),
        )
        with pytest.raises(NotFoundError):  # fiscal profile required
            await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)

    other_org, other_user = await _org()
    async with tenant_session(str(other_org), str(other_user)) as s:
        with pytest.raises(NotFoundError):
            await inv.get_invoice(s, org_id=other_org, invoice_id=d.id)


async def test_mark_paid_allowed_post_emission() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        await inv.add_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            description="svc",
            unit_price=Decimal(100),
        )
        await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        paid = await inv.mark_paid(s, org_id=org, actor_id=user, invoice_id=d.id)
    assert paid.payment_status.value == "paid"
    with pytest.raises(DomainError):
        async with tenant_session(str(org), str(user)) as s:
            await inv.ingest_receipt(
                s,
                org_id=org,
                actor_id=user,
                identificativo_sdi="nope",
                outcome="ZZ",
            )


async def test_issuer_piva_country_prefix_is_normalized() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        p = await inv.create_issuer_profile(
            s,
            org_id=org,
            actor_id=user,
            label="P",
            denominazione="Acme",
            piva="IT13438810015",  # VIES form: prefix glued to the number
            indirizzo="Via Roma 1",
            cap="00100",
            comune="Roma",
            is_default=True,
        )
        # Split into IdPaese + bare IdCodice (no prefix in the number).
        assert p.paese == "IT"
        assert p.piva == "13438810015"
        # A malformed code (not 11 digits after the prefix) is rejected.
        with pytest.raises(DomainError):
            await inv.create_issuer_profile(
                s,
                org_id=org,
                actor_id=user,
                label="Bad",
                denominazione="Bad",
                piva="IT123",
                indirizzo="Via Roma 1",
                cap="00100",
                comune="Roma",
            )
