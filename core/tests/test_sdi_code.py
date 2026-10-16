"""Codice destinatario validation, and the client write paths that apply it."""

from __future__ import annotations

import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.sdi_code import is_valid_sdi_code, normalize_sdi_code
from mycelium_core.services import taxonomy
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput


@pytest.mark.parametrize("code", ["ABCDEFG", "UFABCD", "0000000", "XXXXXXX"])
def test_the_facet_admits_six_and_seven_upper_case_alphanumerics(code: str) -> None:
    assert is_valid_sdi_code(normalize_sdi_code(code))


@pytest.mark.parametrize("code", ["ABCDEFGH", "ABCDE", "AB-C123"])
def test_anything_else_is_refused(code: str) -> None:
    assert not is_valid_sdi_code(normalize_sdi_code(code))


def test_normalization_upper_cases_and_strips() -> None:
    assert normalize_sdi_code(" abc defg ") == "ABCDEFG"
    assert normalize_sdi_code("  ") is None


async def test_a_client_code_is_stored_upper_case_and_an_invalid_one_refused() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SDI",
        )

    def client(sdi: str) -> ClientInput:
        return ClientInput(legal_name="Acme", vat_number="09876543210", sdi_code=sdi)

    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        tag = await taxonomy.create_client(
            s, org_id=r.org_id, actor_id=r.user_id, name="A", profile=client("abcdefg")
        )
        assert tag.id is not None
    # Raised OUT of the session, as a request does: the refusal must roll back.
    with pytest.raises(DomainError):
        async with tenant_session(str(r.org_id), str(r.user_id)) as s:
            await taxonomy.update_client(
                s,
                org_id=r.org_id,
                actor_id=r.user_id,
                tag_id=tag.id,
                fields={"sdi_code": "AB-C123"},
            )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        clients = await taxonomy.list_clients(s, org_id=r.org_id)
        assert [p.sdi_code for t, p in clients if t.id == tag.id] == ["ABCDEFG"]
