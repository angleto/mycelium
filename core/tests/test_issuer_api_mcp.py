"""MCP invoice read tools + the per-tool scope gate (phase 5 of task 19b7e874).

The read tools (list_invoices / get_invoice / get_invoice_xml /
list_issuer_profiles) reuse the same service layer as REST (ADR-0001), and the
write tools are now gated: a scoped assistant lacking ``invoices:write`` is
refused (T22), while stdio / bare-token callers keep full access.
"""

from __future__ import annotations

import secrets
import uuid

import pytest

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ForbiddenError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.services.auth import signup
from mycelium_core.services.taxonomy import ClientInput, create_client
from mycelium_mcp.server import (
    _PRINCIPAL,
    add_invoice_line,
    create_invoice,
    get_invoice,
    get_invoice_xml,
    list_invoices,
    list_issuer_profiles,
    set_issuer_profile,
    transmit_invoice,
)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_mcp_invoice_read_tools() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="MCPR")
    org, user, token = str(r.org_id), r.user_id, r.token
    async with tenant_session(org, str(user)) as s:
        client = await create_client(
            s,
            org_id=r.org_id,
            actor_id=user,
            name="Cli",
            profile=ClientInput(
                legal_name="Cli",
                country_code="IT",
                vat_number="09876543210",
                sdi_code="0000000",
                address="Via Milano 2",
                postal_code="20100",
                city="Milano",
            ),
        )
        client_id = str(client.id)
    # Compose + transmit via the (write) MCP tools -- stdio flow, gate inert.
    await set_issuer_profile(
        token,
        org,
        legal_name="Acme Srl",
        vat_number="01234567890",
        address="Via Roma 1",
        postal_code="00100",
        city="Roma",
    )
    inv = await create_invoice(token, org, client_id)
    await add_invoice_line(token, org, inv["id"], "consulting", 100.0)
    tx = await transmit_invoice(token, org, inv["id"])
    assert tx["state"] == "transmitted"
    inv_id = inv["id"]

    # Read tools.
    listed = await list_invoices(token, org, client_tag_id=client_id)
    assert listed[0]["id"] == inv_id  # newest-first: "last invoice of client X"
    got = await get_invoice(token, org, inv_id)
    assert got["state"] == "transmitted"
    xml = await get_invoice_xml(token, org, inv_id)
    assert "FatturaElettronica" in xml["xml"]
    profs = await list_issuer_profiles(token, org)
    assert any(p["legal_name"] == "Acme Srl" for p in profs)


async def test_t22_mcp_write_gated_by_scope() -> None:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="MCPS")
    org, user = r.org_id, r.user_id
    # A bound assistant scoped to read-only, and its agent token.
    async with tenant_session(str(org), str(user)) as s:
        assistant = AiAssistant(
            org_id=org,
            user_id=user,
            label="read-only",
            handle=f"ro-{uuid.uuid4().hex[:8]}",
            scope=["invoices:read"],
            is_active=True,
        )
        s.add(assistant)
        await s.flush()
        tok = AgentToken(
            org_id=org,
            user_id=user,
            name="t",
            prefix=f"mycelium_at_{secrets.token_hex(4)}",
            token_hash=secrets.token_bytes(32),
            scope="mcp",
            assistant_id=assistant.id,
        )
        s.add(tok)
        await s.flush()
        token_id = tok.id

    # Simulate the HTTP transport: the bearer middleware publishes the principal.
    reset = _PRINCIPAL.set((user, org, token_id))
    try:
        # A write tool is refused (the assistant lacks invoices:write)...
        with pytest.raises(ForbiddenError) as exc:
            await transmit_invoice("", "", str(uuid.uuid4()))
        assert exc.value.code == MessageCode.MCP_SCOPE_DENIED
        # ...a read tool is allowed (it holds invoices:read).
        assert isinstance(await list_invoices("", ""), list)
    finally:
        _PRINCIPAL.reset(reset)
