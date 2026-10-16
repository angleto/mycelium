"""Codice fiscale validation, and every place a client's one is written.

The check digits are tested against an INDEPENDENT oracle where one exists:
the 11-digit form uses the partita IVA check digit, which is the Luhn
algorithm, so a separate Luhn implementation here would catch a mistake in
the module's own arithmetic. The 16-character form is pinned on the
canonical published example and on a single-character corruption of it.
"""

from __future__ import annotations

import importlib.util
import os
import random
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from sqlalchemy import update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.services import invoice as inv_svc
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput
from mycelium_core.tax_code import is_valid_tax_code, normalize_tax_code


def _luhn(code: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(code)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def test_the_published_example_passes_and_a_corrupted_one_does_not() -> None:
    assert is_valid_tax_code("RSSMRA85T10A562S")
    assert not is_valid_tax_code("RSSMRA85T10A562T")
    assert not is_valid_tax_code("RSSMRA85T10A563S")


def test_the_numeric_form_agrees_with_luhn() -> None:
    rng = random.Random(20261016)  # noqa: S311 -- a seeded sample of inputs, not a secret
    for _ in range(5000):
        code = "".join(rng.choice("0123456789") for _ in range(11))
        assert is_valid_tax_code(code) == _luhn(code), code


@pytest.mark.parametrize(
    "value",
    [
        "0",  # the placeholder measured in production
        "0000000000",  # wrong length, digits
        "IT01234567890",  # a partita IVA with its prefix, typed into the CF field
        "RSSMRA85T10A562SX",  # 17 characters
        "RSSMRA85Z10A562S",  # Z is not a month letter
    ],
)
def test_values_that_are_not_codici_fiscali_are_refused(value: str) -> None:
    assert not is_valid_tax_code(normalize_tax_code(value))


def test_a_foreign_counterpart_is_held_to_the_xsd_facet_only() -> None:
    """AdE FAQ 63: a foreign consumer's CodiceFiscale repeats the foreign id."""
    assert is_valid_tax_code("DE123456789AB", "DE")
    assert not is_valid_tax_code("DE123456789AB", "IT")
    assert not is_valid_tax_code("DE123456789AB")
    assert not is_valid_tax_code("0", "CH")


def test_normalization_strips_whitespace_and_upper_cases() -> None:
    assert normalize_tax_code("  rssmra85t10 a562s ") == "RSSMRA85T10A562S"
    assert normalize_tax_code("   ") is None
    assert is_valid_tax_code(None)


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="CF",
        )
    return r.org_id, r.user_id


def _client(tax_code: str | None, vat_number: str | None = None) -> ClientInput:
    return ClientInput(
        legal_name="Mario Rossi",
        country_code="IT",
        vat_number=vat_number,
        tax_code=tax_code,
        address="Via Milano 9",
        postal_code="20100",
        city="Milano",
        province="MI",
        country="IT",
        sdi_code="ABCDEFG",
    )


async def test_every_client_write_path_refuses_a_placeholder() -> None:
    org_id, user_id = await _org()
    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(DomainError):
            await taxonomy.create_client(
                s, org_id=org_id, actor_id=user_id, name="A", profile=_client("0")
            )
    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(DomainError):
            await taxonomy.resolve_or_create_client(
                s,
                org_id=org_id,
                actor_id=user_id,
                name="B",
                profile=_client("0", vat_number="09876543210"),
            )
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.create_client(
            s, org_id=org_id, actor_id=user_id, name="C", profile=_client(None, "09876543210")
        )
        tag_id = tag.id
    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(DomainError):
            await taxonomy.fill_client_gaps(
                s, org_id=org_id, actor_id=user_id, tag_id=tag_id, profile=_client("0")
            )
    async with tenant_session(str(org_id), str(user_id)) as s:
        with pytest.raises(DomainError):
            await taxonomy.update_client(
                s, org_id=org_id, actor_id=user_id, tag_id=tag_id, fields={"tax_code": "0"}
            )


async def test_a_valid_code_is_stored_normalized() -> None:
    org_id, user_id = await _org()
    async with tenant_session(str(org_id), str(user_id)) as s:
        tag = await taxonomy.create_client(
            s,
            org_id=org_id,
            actor_id=user_id,
            name="D",
            profile=_client(" rssmra85t10a562s "),
        )
        row = await s.get(ClientProfile, tag.id)
        assert row is not None
        assert row.tax_code == "RSSMRA85T10A562S"


async def test_a_record_written_before_the_check_is_refused_at_transmit() -> None:
    """The write paths cannot repair rows that predate them. The pre-transmit
    validation names the field instead of letting the XSD, or SdI, find it."""
    org_id, user_id = await _org()
    async with tenant_session(str(org_id), str(user_id)) as s:
        await inv_svc.create_issuer_profile(
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
        tag = await taxonomy.create_client(
            s, org_id=org_id, actor_id=user_id, name="E", profile=_client(None, "09876543210")
        )
        # The legacy shape: written directly, the way a pre-check row exists.
        await s.execute(
            update(ClientProfile).where(ClientProfile.tag_id == tag.id).values(tax_code="0")
        )
        inv = await inv_svc.create_draft(
            s, org_id=org_id, actor_id=user_id, client_tag_id=tag.id, purpose="Servizio"
        )
        await inv_svc.add_line(
            s,
            org_id=org_id,
            actor_id=user_id,
            invoice_id=inv.id,
            description="Servizio",
            unit_price=Decimal(100),
            quantity=Decimal(1),
            vat_rate=Decimal(22),
        )
        with pytest.raises(DomainError) as excinfo:
            await inv_svc.get_xml_preview(s, org_id=org_id, invoice_id=inv.id)
        assert "tax_code" in str(excinfo.value)


_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _migration(filename: str) -> ModuleType:
    """A migration module by file name (they start with a digit, so no import)."""
    spec = importlib.util.spec_from_file_location(
        filename.removesuffix(".py"), _VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _owner_transaction() -> Iterator[sa.Connection]:
    """The migration runner's role, inside a transaction that is ROLLED BACK:
    a data migration is not scoped to one workspace and the test database is
    shared by the rest of the suite."""
    url = os.environ.get("MYCELIUM_DATABASE_URL_SYNC")
    if not url:
        pytest.skip("MYCELIUM_DATABASE_URL_SYNC not set")
    engine = sa.create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                yield conn
            finally:
                tx.rollback()
    finally:
        engine.dispose()


async def test_0019_clears_malformed_codes_and_keeps_valid_ones() -> None:
    org_id, user_id = await _org()
    tags: dict[str, uuid.UUID] = {}
    async with tenant_session(str(org_id), str(user_id)) as s:
        for name in ("zero", "prefixed", "person", "numeric", "padded", "none"):
            tag = await taxonomy.create_client(
                s,
                org_id=org_id,
                actor_id=user_id,
                name=f"{name}-{uuid.uuid4().hex[:6]}",
                profile=_client(None, vat_number="09876543210"),
            )
            tags[name] = tag.id
    async with tenant_session(str(org_id), str(user_id)) as s:
        for name in ("foreign_id", "foreign_zero"):
            tag = await taxonomy.create_client(
                s,
                org_id=org_id,
                actor_id=user_id,
                name=f"{name}-{uuid.uuid4().hex[:6]}",
                profile=ClientInput(
                    legal_name="Muster AG",
                    country_code="DE",
                    vat_number="DE123456789",
                    address="Hauptstrasse 1",
                    postal_code="10115",
                    city="Berlin",
                    country="DE",
                    sdi_code="XXXXXXX",
                ),
            )
            tags[name] = tag.id
    legacy = {
        "foreign_id": "DE123456789AB",
        "foreign_zero": "0",
        "zero": "0",
        "prefixed": "IT01234567890",
        "person": "RSSMRA85T10A562S",
        "numeric": "02223334448",
        "padded": " rssmra85t10a562s ",
    }
    m = _migration("0019_clear_malformed_client_tax_codes.py")
    with _owner_transaction() as conn:
        for name, value in legacy.items():
            conn.execute(
                sa.text("UPDATE client_profile SET tax_code = :v WHERE tag_id = :t"),
                {"v": value, "t": tags[name]},
            )
        conn.execute(sa.text(m.CLEAR_SQL))
        after = {
            name: conn.execute(
                sa.text("SELECT tax_code FROM client_profile WHERE tag_id = :t"), {"t": tag}
            ).scalar_one()
            for name, tag in tags.items()
        }
    assert after == {
        "foreign_id": "DE123456789AB",
        "foreign_zero": None,
        "zero": None,
        "prefixed": None,
        "person": "RSSMRA85T10A562S",
        "numeric": "02223334448",
        "padded": " rssmra85t10a562s ",
        "none": None,
    }
