"""F7 MCP co-equality (DB-backed): invoicing tools reuse the same
service layer as REST (docs/adr/0001)."""

from __future__ import annotations

import uuid

from flow_core.db import admin_session, tenant_session
from flow_core.services.auth import signup
from flow_core.services.taxonomy import ClientInput, create_client
from flow_mcp.server import (
    add_invoice_line,
    create_invoice,
    invoice_credit_note,
    set_issuer_profile,
    transmit_invoice,
)


async def test_mcp_invoicing() -> None:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="MCP7",
        )
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        client = await create_client(
            s,
            org_id=r.org_id,
            actor_id=r.user_id,
            name="Cli",
            profile=ClientInput(
                ragione_sociale="Cli",
                id_paese="IT",
                id_codice="01234567890",
                codice_destinatario="0000000",
                indirizzo="Via Milano 2",
                cap="20100",
                comune="Milano",
            ),
        )
        client_id = client.id
    token, org = r.token, str(r.org_id)

    await set_issuer_profile(
        token=token,
        org_id=org,
        denominazione="Acme",
        piva="09876543210",
        indirizzo="Via X 1",
        cap="00100",
        comune="Roma",
    )
    d = await create_invoice(token=token, org_id=org, client_tag_id=str(client_id))
    await add_invoice_line(
        token=token,
        org_id=org,
        invoice_id=d["id"],
        description="svc",
        unit_price=100.0,
    )
    tx = await transmit_invoice(token=token, org_id=org, invoice_id=d["id"])
    assert tx["number"] == 1 and tx["state"] == "transmitted"

    note = await invoice_credit_note(token=token, org_id=org, parent_invoice_id=d["id"])
    assert note["document_type"] == "TD04"
