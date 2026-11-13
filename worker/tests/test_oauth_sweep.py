"""The OAuth sweep removes what nobody completed, and only that (ADR-0069).

Dynamic Client Registration writes a row for any caller on the internet,
and ``/authorize`` writes one per attempt. The per-origin bounds limit how
fast that happens; this sweep is what makes the rows go away. It must not
touch a client somebody relies on: one a person registered by hand, or one
that has completed a consent.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.oauth_authorization_request import OAuthAuthorizationRequest
from mycelium_core.models.oauth_client import OAuthClient
from mycelium_core.services import oauth_clients, oauth_consent
from mycelium_core.services.auth import SignupResult, signup
from mycelium_worker import oauth_sweep

_CB = ["https://host.example/cb"]


def _ago(delta: datetime.timedelta) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC) - delta


async def _age(client_id: str, *, hours: int) -> None:
    async with admin_session() as s:
        await s.execute(
            update(OAuthClient)
            .where(OAuthClient.client_id == client_id)
            .values(created_at=_ago(datetime.timedelta(hours=hours)))
        )


async def _exists(client_id: str) -> bool:
    async with admin_session() as s:
        row = (
            await s.execute(select(OAuthClient.id).where(OAuthClient.client_id == client_id))
        ).scalar_one_or_none()
    return row is not None


async def test_sweep_removes_old_unused_dynamic_clients_and_nothing_else() -> None:
    async with admin_session() as s:
        stale = await oauth_clients.register_dynamic(
            s, client_name="stale", redirect_uris=_CB, origin_ip=None
        )
        fresh = await oauth_clients.register_dynamic(
            s, client_name="fresh", redirect_uris=_CB, origin_ip=None
        )
        used = await oauth_clients.register_dynamic(
            s, client_name="used", redirect_uris=_CB, origin_ip=None
        )
        stale_id, fresh_id, used_id = stale.client_id, fresh.client_id, used.client_id
        # A pending request on the stale client is deleted with it.
        await oauth_consent.open_request(
            s,
            client_id=stale.client_id,
            client_name=stale.client_name,
            client_registration=stale.registration,
            redirect_uri=_CB[0],
            code_challenge="c" * 43,
            state=None,
            resource=None,
            requested_scope=None,
            origin_ip=None,
        )
    async with admin_session() as s:
        await oauth_clients.mark_granted(s, client_id=used_id)

    r = await signup_person()
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        manual = await oauth_clients.register_manual(
            s, org_id=r.org_id, actor_id=r.user_id, client_name="mine", redirect_uris=_CB
        )
        manual_id = manual.client_id

    for cid in (stale_id, used_id):
        await _age(cid, hours=oauth_clients.UNUSED_CLIENT_TTL_HOURS + 1)
    # Manual rows are only writable by their workspace; age it as the owner.
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        await s.execute(
            update(OAuthClient)
            .where(OAuthClient.client_id == manual_id)
            .values(created_at=_ago(datetime.timedelta(days=30)))
        )

    await oauth_sweep.run_once()

    assert not await _exists(stale_id)
    assert await _exists(fresh_id)
    assert await _exists(used_id)
    async with tenant_session(str(r.org_id), str(r.user_id)) as s:
        kept = (
            await s.execute(select(OAuthClient.id).where(OAuthClient.client_id == manual_id))
        ).scalar_one_or_none()
    assert kept is not None
    async with admin_session() as s:
        orphans = (
            (
                await s.execute(
                    select(OAuthAuthorizationRequest).where(
                        OAuthAuthorizationRequest.client_id == stale_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert orphans == []


async def test_sweep_removes_long_expired_requests() -> None:
    async with admin_session() as s:
        client = await oauth_clients.register_dynamic(
            s, client_name="c", redirect_uris=_CB, origin_ip=None
        )
        await oauth_clients.mark_granted(s, client_id=client.client_id)
        await oauth_consent.open_request(
            s,
            client_id=client.client_id,
            client_name=client.client_name,
            client_registration=client.registration,
            redirect_uri=_CB[0],
            code_challenge="c" * 43,
            state=None,
            resource=None,
            requested_scope=None,
            origin_ip=None,
        )
        cid = client.client_id
    async with admin_session() as s:
        await s.execute(
            update(OAuthAuthorizationRequest)
            .where(OAuthAuthorizationRequest.client_id == cid)
            .values(expires_at=_ago(datetime.timedelta(days=8)))
        )
    await oauth_sweep.run_once()
    async with admin_session() as s:
        left = (
            (
                await s.execute(
                    select(OAuthAuthorizationRequest).where(
                        OAuthAuthorizationRequest.client_id == cid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert left == []


async def signup_person() -> SignupResult:
    async with admin_session() as s:
        return await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name="SWEEP",
        )
