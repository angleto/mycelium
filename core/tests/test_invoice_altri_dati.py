"""AltriDatiGestionali on an invoice line (FatturaPA 2.2.1.16,
migration 0088, ADR-0003 + ADR-0009).

What is legally load-bearing here: the block set is EMPTY by default (a
line that carries none emits none), the values that do get stored are
inside the XSD facets *before* they can reach a frozen XML, and the set
is editable only while the document is a draft.

The database is shared with other suites, so every assertion is scoped
to the invoice/line this test created -- never a global tally.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ConflictError, DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.services import invoice as inv
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput, create_client


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="ADG")
    return r.org_id, r.user_id


async def _setup(s, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    await inv.create_issuer_profile(
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


async def _draft_line(s, org: uuid.UUID, user: uuid.UUID, **kw):
    """A draft with one line, the fixture every test starts from."""
    client_id = await _setup(s, org, user)
    d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
    line = await inv.add_line(
        s,
        org_id=org,
        actor_id=user,
        invoice_id=d.id,
        description="consulenza",
        unit_price=Decimal("100.00"),
        **kw,
    )
    return d, line


async def test_a_line_carries_no_blocks_by_default() -> None:
    # The owner's requirement: empty by default. A line created without
    # ``altri_dati`` must have zero rows, so nothing is ever emitted for
    # the overwhelmingly common line.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(s, org, user)
        assert await inv.list_line_altri_dati(s, org_id=org, line_id=line.id) == []
        assert await inv.list_invoice_altri_dati(s, org_id=org, invoice_id=d.id) == {}


async def test_full_block_round_trips_with_every_field() -> None:
    # All four elements at once (TipoDato + the three optional
    # references), through add_line and back out of the DB.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(
            s,
            org,
            user,
            altri_dati=[
                inv.AltriDatiBlock(
                    tipo_dato="N.DOC.COMM",
                    riferimento_testo="DC-2026-0001",
                    riferimento_numero=Decimal("17.25"),
                    riferimento_data=dt.date(2026, 3, 1),
                )
            ],
        )
        rows = await inv.list_line_altri_dati(s, org_id=org, line_id=line.id)
        assert len(rows) == 1
        r = rows[0]
        assert r.tipo_dato == "N.DOC.COMM"
        assert r.riferimento_testo == "DC-2026-0001"
        assert r.riferimento_numero == Decimal("17.25")
        assert r.riferimento_data == dt.date(2026, 3, 1)
        assert r.ord == 1
        assert r.org_id == org
        # The invoice-wide accessor (what the XML/PDF builders use) sees
        # the same row under its line id.
        by_line = await inv.list_invoice_altri_dati(s, org_id=org, invoice_id=d.id)
        assert [x.id for x in by_line[line.id]] == [r.id]


async def test_conventional_shapes_are_storable_as_is() -> None:
    # The three binding conventions the UI offers as shortcuts. NB3 is
    # the interesting one: TipoDato alone, all three references empty.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _d, line = await _draft_line(
            s,
            org,
            user,
            altri_dati=[
                inv.AltriDatiBlock(tipo_dato="NB3"),
                inv.AltriDatiBlock(
                    tipo_dato="INTENTO", riferimento_testo="08060120341234567-000001"
                ),
            ],
        )
        rows = await inv.list_line_altri_dati(s, org_id=org, line_id=line.id)
        assert [(r.ord, r.tipo_dato) for r in rows] == [(1, "NB3"), (2, "INTENTO")]
        nb3 = rows[0]
        assert nb3.riferimento_testo is None
        assert nb3.riferimento_numero is None
        assert nb3.riferimento_data is None


async def test_replace_swaps_the_whole_set_and_resequences_ord() -> None:
    # REPLACE semantics: the caller sends the full list, ``ord`` is
    # re-assigned 1..n from that order, and the previous rows are gone.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(
            s,
            org,
            user,
            altri_dati=[
                inv.AltriDatiBlock(tipo_dato="A1"),
                inv.AltriDatiBlock(tipo_dato="A2"),
                inv.AltriDatiBlock(tipo_dato="A3"),
            ],
        )
        first_ids = {r.id for r in await inv.list_line_altri_dati(s, org_id=org, line_id=line.id)}
        rows = await inv.replace_line_altri_dati(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            line_id=line.id,
            blocks=[
                inv.AltriDatiBlock(tipo_dato="B1", riferimento_numero=Decimal("2.00")),
                inv.AltriDatiBlock(tipo_dato="B2"),
            ],
        )
        assert [(r.ord, r.tipo_dato) for r in rows] == [(1, "B1"), (2, "B2")]
        assert not first_ids & {r.id for r in rows}  # the old rows were replaced
        # And an empty list clears the set entirely.
        assert (
            await inv.replace_line_altri_dati(
                s, org_id=org, actor_id=user, invoice_id=d.id, line_id=line.id, blocks=[]
            )
            == []
        )
        assert await inv.list_line_altri_dati(s, org_id=org, line_id=line.id) == []


async def test_update_line_keeps_blocks_unless_told_otherwise() -> None:
    # Tri-state: omitting ``altri_dati`` on a line edit must NOT drop the
    # blocks (a price fix is not a request to clear them); passing [] is
    # the explicit clear.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(
            s, org, user, altri_dati=[inv.AltriDatiBlock(tipo_dato="INTENTO")]
        )
        await inv.update_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            line_id=line.id,
            description="consulenza (rev)",
            unit_price=Decimal("120.00"),
            quantity=Decimal(1),
        )
        kept = await inv.list_line_altri_dati(s, org_id=org, line_id=line.id)
        assert [r.tipo_dato for r in kept] == ["INTENTO"]
        await inv.update_line(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            line_id=line.id,
            description="consulenza (rev)",
            unit_price=Decimal("120.00"),
            quantity=Decimal(1),
            altri_dati=[],
        )
        assert await inv.list_line_altri_dati(s, org_id=org, line_id=line.id) == []


async def test_blocks_die_with_their_line() -> None:
    # FK ON DELETE CASCADE (migration 0088): deleting the line takes its
    # blocks with it, leaving no orphan rows behind.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(s, org, user, altri_dati=[inv.AltriDatiBlock(tipo_dato="NB3")])
        await inv.delete_line(s, org_id=org, actor_id=user, invoice_id=d.id, line_id=line.id)
        assert await inv.list_line_altri_dati(s, org_id=org, line_id=line.id) == []
        assert await inv.list_invoice_altri_dati(s, org_id=org, invoice_id=d.id) == {}


# --- validation (the XSD facets, checked before anything is written) ---

_REJECTED = [
    # TipoDato is REQUIRED (String10Type has no empty member).
    pytest.param(inv.AltriDatiBlock(tipo_dato="   "), id="tipo_dato_blank"),
    # String10Type: at most 10 chars.
    pytest.param(inv.AltriDatiBlock(tipo_dato="ABCDEFGHIJK"), id="tipo_dato_too_long"),
    # String10Type is Basic-Latin ONLY (no accents, no symbols).
    pytest.param(inv.AltriDatiBlock(tipo_dato="INTENTÒ"), id="tipo_dato_not_basic_latin"),
    # String60LatinType: at most 60 chars.
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="INTENTO", riferimento_testo="x" * 61),
        id="testo_too_long",
    ),
    # String60LatinType stops at U+00FF: an arrow / CJK char is out.
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="INTENTO", riferimento_testo="prot → 001"),
        id="testo_not_latin1",
    ),
    # Amount8DecimalType: at most 8 decimals (numeric(21,8) would
    # otherwise round the 9th away silently).
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="X", riferimento_numero=Decimal("1.123456789")),
        id="numero_too_many_decimals",
    ),
    # Amount8DecimalType: at most 11 integer digits.
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="X", riferimento_numero=Decimal("100000000000.00")),
        id="numero_too_many_integer_digits",
    ),
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="X", riferimento_numero=Decimal("NaN")),
        id="numero_not_finite",
    ),
    pytest.param(
        inv.AltriDatiBlock(tipo_dato="X", riferimento_data="2026-03-01"),  # type: ignore[arg-type]
        id="data_not_a_date",
    ),
]


@pytest.mark.parametrize("block", _REJECTED)
async def test_invalid_block_is_refused_with_a_coded_error(block: inv.AltriDatiBlock) -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(s, org, user)
        with pytest.raises(DomainError) as e:
            await inv.replace_line_altri_dati(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                line_id=line.id,
                blocks=[block],
            )
        # A coded DomainError (ADR-0017), never a bare ValueError.
        assert e.value.code is MessageCode.INVOICE_ALTRI_DATI_INVALID
        # Nothing was written: validation runs before the delete+insert.
        assert await inv.list_line_altri_dati(s, org_id=org, line_id=line.id) == []


async def test_too_many_blocks_on_one_line_is_refused() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(s, org, user)
        with pytest.raises(DomainError) as e:
            await inv.replace_line_altri_dati(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                line_id=line.id,
                blocks=[inv.AltriDatiBlock(tipo_dato="X")] * (inv._ALTRI_DATI_MAX_BLOCKS + 1),
            )
        assert e.value.code is MessageCode.INVOICE_ALTRI_DATI_INVALID


async def test_invalid_block_does_not_leave_a_half_created_line() -> None:
    # add_line validates BEFORE inserting the line: a rejected block must
    # not leave an orphan line on the draft.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        client_id = await _setup(s, org, user)
        d = await inv.create_draft(s, org_id=org, actor_id=user, client_tag_id=client_id, year=2026)
        with pytest.raises(DomainError):
            await inv.add_line(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                description="consulenza",
                unit_price=Decimal("100.00"),
                altri_dati=[inv.AltriDatiBlock(tipo_dato="")],
            )
        assert await inv.list_lines(s, org_id=org, invoice_id=d.id) == []


async def test_accepted_edges_are_not_over_rejected() -> None:
    # The facets are limits, not a narrower house style: exactly 10 chars
    # of TipoDato, 60 of testo, 8 decimals and a negative amount are all
    # valid Amount8DecimalType/String*Type values and must round-trip.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        _d, line = await _draft_line(
            s,
            org,
            user,
            altri_dati=[
                inv.AltriDatiBlock(
                    tipo_dato="ABCDEFGHIJ",
                    # Latin-1 accents are inside String60LatinType.
                    riferimento_testo="perità " + "x" * 53,
                    riferimento_numero=Decimal("-1.12345678"),
                )
            ],
        )
        r = (await inv.list_line_altri_dati(s, org_id=org, line_id=line.id))[0]
        assert r.tipo_dato == "ABCDEFGHIJ"
        assert len(r.riferimento_testo or "") == 60
        assert r.riferimento_numero == Decimal("-1.12345678")


# --- tenant isolation (ADR-0002/0015, migration 0088) ---


async def test_another_tenant_cannot_see_or_write_the_blocks() -> None:
    # A new org-scoped table without an RLS policy is either invisible or
    # a cross-tenant leak, and nothing else in the suite sweeps for one.
    # ``list_line_altri_dati`` filters on invoice_line_id ALONE, so the
    # policy is the only fence being tested here: hand it a foreign line
    # id and it must come back empty.
    org_a, user_a = await _org()
    org_b, user_b = await _org()
    async with tenant_session(str(org_a), str(user_a)) as sa:
        _d, line_a = await _draft_line(
            sa, org_a, user_a, altri_dati=[inv.AltriDatiBlock(tipo_dato="INTENTO")]
        )
        assert len(await inv.list_line_altri_dati(sa, org_id=org_a, line_id=line_a.id)) == 1
    async with tenant_session(str(org_b), str(user_b)) as sb:
        assert await inv.list_line_altri_dati(sb, org_id=org_b, line_id=line_a.id) == []
        # And the write path is fenced too, not just the read: the other
        # tenant's line is simply not there.
        with pytest.raises(NotFoundError):
            await inv.get_line(sb, org_id=org_b, invoice_id=_d.id, line_id=line_a.id)
    # Org A still sees its own row: the isolation is mutual, not a delete.
    async with tenant_session(str(org_a), str(user_a)) as sa2:
        rows = await inv.list_line_altri_dati(sa2, org_id=org_a, line_id=line_a.id)
        assert [r.tipo_dato for r in rows] == ["INTENTO"]


async def test_the_table_carries_force_rls_and_its_policy() -> None:
    # FORCE, not just ENABLE: without it the owner role bypasses the
    # policy and a maintenance session reads every tenant's blocks.
    async with admin_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'invoice_line_altri_dati'"
                )
            )
        ).one()
        assert row.relrowsecurity is True
        assert row.relforcerowsecurity is True
        # Same predicate on USING and WITH CHECK as the parent table's:
        # a read fence without a write fence still lets a tenant plant a
        # row under someone else's org_id.
        preds = (
            await s.execute(
                text(
                    "SELECT c.relname, pg_get_expr(p.polqual, p.polrelid) AS using_expr, "
                    "pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr "
                    "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    "WHERE c.relname IN ('invoice_line_altri_dati', 'invoice_lines')"
                )
            )
        ).all()
        by_table = {r.relname: (r.using_expr, r.check_expr) for r in preds}
        assert "invoice_line_altri_dati" in by_table, "the table has NO policy"
        assert by_table["invoice_line_altri_dati"] == by_table["invoice_lines"]
        assert by_table["invoice_line_altri_dati"][1] is not None


# --- immutability after emission (ADR-0009 / ADR-0046) ---


async def test_transmitted_invoice_refuses_the_edit() -> None:
    # Same guard as every other line edit: once transmitted the document
    # is append-only and its XML is frozen, so a later block could not
    # reach what was sent. Reading stays allowed.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(
            s, org, user, altri_dati=[inv.AltriDatiBlock(tipo_dato="INTENTO")]
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert tx.number is not None
        with pytest.raises(ConflictError) as e:
            await inv.replace_line_altri_dati(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                line_id=line.id,
                blocks=[inv.AltriDatiBlock(tipo_dato="NB3")],
            )
        assert e.value.code is MessageCode.INVOICE_NOT_DRAFT
        # The emitted set is unchanged and still readable.
        rows = await inv.list_line_altri_dati(s, org_id=org, line_id=line.id)
        assert [r.tipo_dato for r in rows] == ["INTENTO"]


# --- the blocks actually reach the document (owner requirement) ---


async def test_blocks_reach_the_frozen_xml_the_preview_and_the_pdf() -> None:
    # The end-to-end wiring, not the emitter in isolation: storing a
    # block and echoing it back over the API is worth nothing if
    # transmit()/get_xml_preview()/render_pdf() never pass the rows to
    # the builders. The owner's requirement is that a filled block
    # appears in BOTH the XML and the PDF, and the XML is frozen at
    # transmit (ADR-0009 / ADR-0046) -- so a block missed here is missed
    # forever for that document.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, line = await _draft_line(
            s,
            org,
            user,
            altri_dati=[
                inv.AltriDatiBlock(tipo_dato="INTENTO", riferimento_testo="08060120347895-000001"),
                inv.AltriDatiBlock(
                    tipo_dato="N.DOC.COMM",
                    riferimento_testo="0001",
                    riferimento_numero=Decimal("7"),
                    riferimento_data=dt.date(2026, 2, 2),
                ),
            ],
        )
        # The live ANTEPRIMA of the still-draft document.
        preview = await inv.get_xml_preview(s, org_id=org, invoice_id=d.id)
        assert "<TipoDato>INTENTO</TipoDato>" in preview
        assert "<RiferimentoTesto>08060120347895-000001</RiferimentoTesto>" in preview
        # ord order survives the round trip through the DB.
        assert preview.index("<TipoDato>INTENTO</TipoDato>") < preview.index(
            "<TipoDato>N.DOC.COMM</TipoDato>"
        )

        # The courtesy PDF: the blocks add ink no other difference can
        # explain (same document, same line, blocks cleared in between).
        _number, with_blocks = await inv.render_pdf(s, org_id=org, invoice_id=d.id)
        await inv.replace_line_altri_dati(
            s, org_id=org, actor_id=user, invoice_id=d.id, line_id=line.id, blocks=[]
        )
        _number, without = await inv.render_pdf(s, org_id=org, invoice_id=d.id)
        assert len(with_blocks) > len(without)

        # And the frozen document: what SdI receives.
        await inv.replace_line_altri_dati(
            s,
            org_id=org,
            actor_id=user,
            invoice_id=d.id,
            line_id=line.id,
            blocks=[inv.AltriDatiBlock(tipo_dato="INTENTO", riferimento_testo="12345/2026-1")],
        )
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert tx.xml is not None
        assert "<AltriDatiGestionali><TipoDato>INTENTO</TipoDato>" in tx.xml
        assert "<RiferimentoTesto>12345/2026-1</RiferimentoTesto>" in tx.xml
        # The cleared second block is gone from the emitted document.
        assert "N.DOC.COMM" not in tx.xml


async def test_a_document_without_blocks_emits_no_element() -> None:
    # The default case must stay untouched: no block anywhere in the XML
    # of an invoice whose lines declare none.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, _line = await _draft_line(s, org, user)
        tx = await inv.transmit(s, org_id=org, actor_id=user, invoice_id=d.id)
        assert tx.xml is not None
        assert "AltriDatiGestionali" not in tx.xml


async def test_a_foreign_line_id_is_not_found() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        d, _line = await _draft_line(s, org, user)
        with pytest.raises(NotFoundError):
            await inv.replace_line_altri_dati(
                s,
                org_id=org,
                actor_id=user,
                invoice_id=d.id,
                line_id=uuid.uuid4(),
                blocks=[inv.AltriDatiBlock(tipo_dato="X")],
            )
