"""Searching clients and tags instead of enumerating them.

A workspace that invoices through a payment connector grows one client per
paying customer, so every control that rendered "all clients" stops working
long before the data does -- the focus dropdown, the structural client select
on a note or a task, the tag grid. The fix is not to hide those clients (a
customer who pays for a consultancy has to be findable the moment you need
them) but to stop enumerating: search narrows, a limit caps, and an empty box
offers what has recent ACTIVITY rather than what sorts first alphabetically.

Recency is derived, not stored: ``task_tag`` and ``note_tag`` are two FK
columns with no timestamp, so "last used" is read off the things the tag is
attached to, plus the invoices issued to a client tag.
"""

from __future__ import annotations

import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.tag import TagKind
from mycelium_core.services import invoice as inv_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="Search",
        )
    return r.org_id, r.user_id


async def _client(org_id: uuid.UUID, user_id: uuid.UUID, name: str, vat: str) -> uuid.UUID:
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.resolve_or_create_client(
            s,
            org_id=org_id,
            actor_id=user_id,
            name=name,
            profile=ClientInput(
                legal_name=name,
                country_code="IT",
                vat_number=vat,
                address="Via Roma",
                civic_number="1",
                postal_code="00100",
                city="Roma",
                province="RM",
                sdi_code="ABCDEFG",
            ),
        )
        return tag.id


async def test_clients_are_searchable_by_name_and_by_fiscal_id() -> None:
    """The identifier in hand is often the fiscal one -- off a bank line, a
    provider dashboard, an email footer -- so searching only by name would send
    an operator back to scrolling the list this exists to replace."""
    org_id, user_id = await _org()
    await _client(org_id, user_id, "AIR CONSULTING GROUP SRL", "11278231003")
    await _client(org_id, user_id, "Beta Servizi SNC", "09876543210")

    async with tenant_session(str(org_id), str(user_id)) as s:
        by_name = await taxonomy.list_clients(s, org_id=org_id, q="consulting")
        assert [t.name for t, _p in by_name] == ["AIR CONSULTING GROUP SRL"]

        # Case-insensitive, and a fragment is enough.
        assert len(await taxonomy.list_clients(s, org_id=org_id, q="BETA")) == 1

        by_vat = await taxonomy.list_clients(s, org_id=org_id, q="11278231")
        assert [t.name for t, _p in by_vat] == ["AIR CONSULTING GROUP SRL"]

        assert await taxonomy.list_clients(s, org_id=org_id, q="nessuno") == []


async def test_the_limit_caps_what_a_picker_can_render() -> None:
    """The property that makes the control survive a connector: the response
    size is bounded by the caller, not by how many customers have paid."""
    org_id, user_id = await _org()
    for i in range(12):
        await _client(org_id, user_id, f"Cliente {i:02d}", f"9876543{i:04d}")

    async with tenant_session(str(org_id), str(user_id)) as s:
        assert len(await taxonomy.list_clients(s, org_id=org_id, limit=5)) == 5
        # Unbounded in COUNT by default: the Clients page, MCP and reports
        # still want every LIVE row, and only the pickers pass a limit.
        # (Archived rows are a separate axis, see the archived test below.)
        assert len(await taxonomy.list_clients(s, org_id=org_id)) >= 12


async def test_recent_orders_by_activity_not_alphabetically() -> None:
    """What makes an EMPTY search box useful.

    Alphabetical order over thousands of connector customers offers whoever
    happens to start with "A". Ordering by activity offers the handful anyone
    actually works with -- and for a client tag an invoice IS the activity,
    which is the signal that matters when the tag came from a connector.
    """
    org_id, user_id = await _org()
    # "Zeta" sorts last and is the one with a recent invoice; "Alfa" sorts
    # first and has none, so alphabetical and recency disagree on purpose.
    alfa = await _client(org_id, user_id, "Alfa Spa", "11111111111")
    zeta = await _client(org_id, user_id, "Zeta Srl", "22222222222")

    async with tenant_session(str(org_id), str(user_id)) as s:
        profile = await inv_svc.create_issuer_profile(
            s,
            org_id=org_id,
            actor_id=user_id,
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
        await inv_svc.create_draft(
            s,
            org_id=org_id,
            actor_id=user_id,
            client_tag_id=zeta,
            issuer_profile_id=profile.id,
        )

    async with tenant_session(str(org_id), str(user_id)) as s:
        plain = [t.id for t, _p in await taxonomy.list_clients(s, org_id=org_id)]
        assert plain.index(alfa) < plain.index(zeta), "default order is alphabetical"

        recent = [t.id for t, _p in await taxonomy.list_clients(s, org_id=org_id, recent=True)]
        assert recent[0] == zeta, "the invoiced client comes first"

        # And the tag surface agrees: the focus and the tag grid read the same
        # signal, so "recent" does not mean two different things in one app.
        tags = await taxonomy.list_tags(s, org_id=org_id, kind=TagKind.client, recent=True, limit=5)
        assert tags[0].id == zeta


async def test_search_and_recency_compose_on_the_tag_surface() -> None:
    """The two knobs are used together by the pickers: type to narrow, and get
    the most recently active of the matches first."""
    org_id, user_id = await _org()
    await _client(org_id, user_id, "Rossi Costruzioni", "33333333333")
    await _client(org_id, user_id, "Rossi Impianti", "44444444444")
    await _client(org_id, user_id, "Verdi Servizi", "55555555555")

    async with tenant_session(str(org_id), str(user_id)) as s:
        rossi = await taxonomy.list_tags(s, org_id=org_id, kind=TagKind.client, q="rossi", limit=10)
        assert {t.name for t in rossi} == {"Rossi Costruzioni", "Rossi Impianti"}
        assert len(await taxonomy.list_tags(s, org_id=org_id, kind=TagKind.client, limit=2)) == 2


async def _archive(org_id: uuid.UUID, user_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.get_tag(s, org_id=org_id, tag_id=tag_id)
        await taxonomy.update_tag(
            s,
            org_id=org_id,
            actor_id=user_id,
            tag_id=tag_id,
            expected_version=tag.version,
            status="archived",
        )


async def test_an_archived_client_is_gone_from_every_picker() -> None:
    """The reported bug: the client dropdown offered archived clients.

    A client is a tag, and archiving one means "stop offering it". The
    exclusion lives in ``list_clients`` and not in one dropdown because
    there are four of them (the focus search, the quick-add select, the
    new-invoice select, the connector triage) plus MCP and the CLI --
    filtering per surface leaves the other five leaking.

    Every knob is exercised: the picker's empty box is ``recent=True``,
    its typed box is ``q``, and both carry a ``limit``. A status filter
    applied AFTER the limit would silently return fewer live clients
    than asked for, which is why the predicate goes first.
    """
    org_id, user_id = await _org()
    live = await _client(org_id, user_id, "Alfa Viva", "66666666666")
    dead = await _client(org_id, user_id, "Alfa Chiusa", "77777777777")
    await _archive(org_id, user_id, dead)

    async with tenant_session(str(org_id), str(user_id)) as s:
        assert dead not in [t.id for t, _p in await taxonomy.list_clients(s, org_id=org_id)]
        for kwargs in (
            {"q": "alfa"},
            {"recent": True},
            {"limit": 20},
            {"q": "alfa", "recent": True, "limit": 20},
        ):
            rows = await taxonomy.list_clients(s, org_id=org_id, **kwargs)  # type: ignore[arg-type]
            ids = [t.id for t, _p in rows]
            assert dead not in ids, f"archived client leaked with {kwargs}"
            assert live in ids, f"live client lost with {kwargs}"

        # And it is not deleted, only hidden: the Clients page has to see it
        # to offer un-archive and purge, and the invoice list has to resolve
        # its name and its tariffa on last quarter's invoices.
        back = [
            t.id for t, _p in await taxonomy.list_clients(s, org_id=org_id, include_archived=True)
        ]
        assert dead in back and live in back


async def test_an_archived_project_is_gone_from_every_picker() -> None:
    """Same rule for projects, which had the same omission."""
    org_id, user_id = await _org()
    client_tag = await _client(org_id, user_id, "Committente", "88888888888")
    async with tenant_session(str(org_id), str(user_id)) as s:
        live = await taxonomy.create_project(
            s, org_id=org_id, actor_id=user_id, name="Vivo", client_tag_id=client_tag
        )
        dead = await taxonomy.create_project(
            s, org_id=org_id, actor_id=user_id, name="Chiuso", client_tag_id=client_tag
        )
        live_id, dead_id = live.id, dead.id
    await _archive(org_id, user_id, dead_id)

    async with tenant_session(str(org_id), str(user_id)) as s:
        ids = [t.id for t, _p in await taxonomy.list_projects(s, org_id=org_id)]
        assert live_id in ids and dead_id not in ids

        back = [
            t.id for t, _p in await taxonomy.list_projects(s, org_id=org_id, include_archived=True)
        ]
        assert dead_id in back
