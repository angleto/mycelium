"""Payment-connector management REST (ADR-0051).

The owner-gated surface under ``/issuer-profiles/{id}/payment-connectors``. It
is the surface that MINTS the credential letting an outside system emit fiscal
documents in this workspace's name, so the properties asserted here are the ones
that keep that authority contained:

- the two plaintext credentials appear in a response body exactly once (create,
  rotate) and in NO read route -- the list is checked field-by-field AND on its
  serialised bytes, because a leak through an unexpected key name would still be
  a leak;
- rotating one credential never re-shows the other;
- every mutation is owner-only, while the read side stays open to a member (the
  triage list is an operator's job, not an owner's);
- PATCH is a true partial write: absent fields are untouched, an explicit null
  clears a nullable one, and a closed-vocabulary violation is refused with its
  own code instead of reaching the CHECK constraint as a 500;
- the issuer nesting is a HARD scope: a connector under a sibling issuer profile
  of the SAME workspace answers 404 (never 403) on every by-id route, and
  another workspace sees nothing at all;
- delete is two-stage (revoke, then purge only what is revoked);
- the event / delivery lists are scoped to their connector and never project the
  raw provider payload, and retry re-arms only a parked event.

Every test mints its own workspace and its own issuer profile. ``X-Workspace-Role``
is a DOWNGRADE lever (``deps.effective_role`` clamps the requested role to the
caller's entitlement), so the same signup account exercises both the owner and
the member path -- which is exactly what the SPA's per-tab "act as" switch does.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mycelium_api.main import app
from mycelium_core.db import tenant_session
from mycelium_core.models.payment_connector import (
    AUTOMATION_MODES,
    DELIVERY_OUTCOMES,
    EMISSION_EVENTS,
    PROVIDERS,
)
from mycelium_core.services import payment_connectors as svc

#: The route the advertised ``webhook_url`` has to land on. Asserting the string
#: alone would only restate ``_webhook_url``; asserting that the app actually
#: MOUNTS this path is what catches a URL the provider could never reach.
_INGRESS_ROUTE = "/api/v1/connectors/{provider}/{connector_id}"

#: Read routes must never carry credential material under ANY key name, so the
#: leak check is a substring scan over key names rather than a deny-list of the
#: three names we happen to have today.
_SECRET_KEY_MARKERS = ("secret", "ciphertext", "hash", "api_key")


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _signup(c: AsyncClient) -> tuple[dict[str, str], dict[str, str], uuid.UUID]:
    """A fresh workspace. Returns ``(owner headers, member headers, org id)``.

    Both header sets authenticate the SAME account: without ``X-Workspace-Role``
    the effective role clamps to ``member``, which is the least-privilege
    default of ``deps.effective_role``. Provisioning a second user would test
    the same gate through more moving parts.
    """
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "PC"},
        )
    ).json()
    base = {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}
    return {**base, "X-Workspace-Role": "owner"}, dict(base), uuid.UUID(a["workspace_id"])


async def _issuer(c: AsyncClient, h: dict[str, str], *, label: str, vat: str) -> str:
    r = await c.post(
        "/issuer-profiles",
        headers=h,
        json={
            "label": label,
            "legal_name": f"{label} Srl",
            "vat_number": vat,
            "address": "Via Roma 1",
            "postal_code": "00100",
            "city": "Roma",
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


async def _setup(c: AsyncClient) -> tuple[dict[str, str], dict[str, str], uuid.UUID, str]:
    """Workspace + one issuer profile: the minimum a connector can exist under."""
    owner, member, org = await _signup(c)
    issuer = await _issuer(c, owner, label="Principale", vat="01234567890")
    return owner, member, org, issuer


def _base(issuer: str) -> str:
    return f"/issuer-profiles/{issuer}/payment-connectors"


async def _create(c: AsyncClient, h: dict[str, str], issuer: str, **over: Any) -> dict[str, Any]:
    """Create a connector, asserting the call itself succeeded.

    The label is randomised because ``uq_payment_connectors_label`` makes it the
    natural key inside an issuer profile, and several tests put two connectors
    under one profile.
    """
    body: dict[str, Any] = {
        "label": f"stripe-{uuid.uuid4().hex[:6]}",
        # A vendor adapter cannot be handed a secret we invented: the provider
        # issues it, so the API refuses to mint one for provider != mycelium.
        "signing_secret": f"whsec_test_{uuid.uuid4().hex}",
    }
    body.update(over)
    r = await c.post(_base(issuer), headers=h, json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def _row(c: AsyncClient, h: dict[str, str], issuer: str, cid: str) -> dict[str, Any] | None:
    """The connector as the LIST route sees it -- the only read surface there is."""
    rows: list[dict[str, Any]] = (await c.get(_base(issuer), headers=h)).json()
    return next((r for r in rows if r["id"] == cid), None)


async def _ingest(
    org_id: uuid.UUID,
    connector_id: str,
    *,
    event_id: str,
    event_type: str = "invoice.paid",
    payload: dict[str, Any] | None = None,
) -> None:
    """Put one event on the ingress ledger.

    Through the service rather than the public webhook: this module tests the
    MANAGEMENT surface, and going through the signed ingress would make every
    event-list assertion depend on the feature flag and on a MAC the provider
    would normally compute.
    """
    cid = uuid.UUID(connector_id)
    async with tenant_session(str(org_id), str(cid), actor_kind="payment_connector") as s:
        await svc.ingest(
            s,
            org_id=org_id,
            connector_id=cid,
            provider_event_id=event_id,
            event_type=event_type,
            payload=payload if payload is not None else {"id": event_id, "type": event_type},
            occurred_at=None,
        )


async def _delivery(
    org_id: uuid.UUID,
    connector_id: str,
    *,
    outcome: str,
    http_status: int,
    provider: str = "stripe",
) -> None:
    cid = uuid.UUID(connector_id)
    async with tenant_session(str(org_id), str(cid), actor_kind="payment_connector") as s:
        await svc.record_delivery(
            s,
            org_id=org_id,
            connector_id=cid,
            provider=provider,
            outcome=outcome,
            http_status=http_status,
            raw_body=b'{"id":"evt_probe"}',
            signature_present=True,
            api_key_present=False,
        )


async def _run(org_id: uuid.UUID, connector_id: str, event_uuid: str) -> str:
    """Process one event the way the worker does, in the connector's own context."""
    cid = uuid.UUID(connector_id)
    async with tenant_session(str(org_id), str(cid), actor_kind="payment_connector") as s:
        return await svc.process_event(s, org_id=org_id, event_id=uuid.UUID(event_uuid))


async def _force_status(org_id: uuid.UUID, connector_id: str, event_uuid: str, status: str) -> None:
    """Pin an event to a terminal status the runner would take a full emission to
    reach. The retry gate is what is under test, not how the row got there."""
    cid = uuid.UUID(connector_id)
    async with tenant_session(str(org_id), str(cid), actor_kind="payment_connector") as s:
        await s.execute(
            text("UPDATE payment_connector_events SET status = :st WHERE id = :eid"),
            {"st": status, "eid": event_uuid},
        )


# --- credentials -----------------------------------------------------------


async def test_create_returns_both_credentials_once_and_the_ingress_url() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, with_api_key=True)

        # Both plaintexts, here and nowhere else.
        assert made["signing_secret"].startswith(svc.SIGNING_SECRET_PREFIX)
        assert made["api_key"] is not None
        assert made["api_key"].startswith(svc.RAW_KEY_PREFIX)
        assert made["has_api_key"] is True

        # The URL the operator pastes into the provider's dashboard must be the
        # path this app really serves, under the connector's OWN provider: the
        # ingress refuses a body addressed under the wrong provider segment.
        assert any(getattr(r, "path", None) == _INGRESS_ROUTE for r in app.routes)
        parsed = urlparse(made["webhook_url"])
        assert parsed.scheme and parsed.netloc
        assert parsed.path == f"/api/v1/connectors/stripe/{made['id']}"


async def test_create_without_api_key_leaves_the_second_factor_unarmed() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer)
        # No key was asked for, so none exists: the signature stays the only
        # ingress credential and ``has_api_key`` must say so.
        assert made["api_key"] is None
        assert made["has_api_key"] is False


async def test_create_accepts_the_providers_own_signing_secret() -> None:
    """For Stripe the secret is not ours to choose: it is the ``whsec_...`` the
    Stripe dashboard shows when the endpoint is created there."""
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, signing_secret="whsec_from_the_dashboard")
        assert made["signing_secret"] == "whsec_from_the_dashboard"


async def test_list_never_returns_secret_material() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, with_api_key=True)

        r = await c.get(_base(issuer), headers=owner)
        assert r.status_code == 200, r.text
        rows: list[dict[str, Any]] = r.json()
        assert len(rows) == 1
        row = rows[0]

        # No key may even LOOK like credential material...
        leaking = [k for k in row if any(m in k.lower() for m in _SECRET_KEY_MARKERS)]
        assert leaking == ["has_api_key"], f"unexpected credential-ish keys: {leaking}"
        assert isinstance(row["has_api_key"], bool)
        assert row["has_api_key"] is True

        # ...and the serialised payload must not contain either plaintext under
        # any shape at all (a nested object, a differently named field).
        blob = json.dumps(rows)
        assert made["signing_secret"] not in blob
        assert made["api_key"] not in blob


async def test_rotate_signing_secret_issues_a_new_one_without_reshowing_the_key() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, with_api_key=True)
        cid = made["id"]

        r = await c.post(f"{_base(issuer)}/{cid}/rotate-signing-secret", headers=owner)
        assert r.status_code == 200, r.text
        rotated = r.json()
        assert rotated["signing_secret"].startswith(svc.SIGNING_SECRET_PREFIX)
        assert rotated["signing_secret"] != made["signing_secret"]
        # Rotating one credential must not re-expose the other.
        assert rotated["api_key"] is None
        assert made["api_key"] not in r.text
        # The second factor is untouched by a signing-secret rotation.
        assert rotated["has_api_key"] is True


async def test_rotate_signing_secret_accepts_an_explicit_secret() -> None:
    """A Stripe rotation happens in Stripe: we install the value they show."""
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer))["id"]
        r = await c.post(
            f"{_base(issuer)}/{cid}/rotate-signing-secret",
            headers=owner,
            json={"signing_secret": "whsec_rotated_in_stripe"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["signing_secret"] == "whsec_rotated_in_stripe"


async def test_rotate_api_key_issues_a_new_key_and_does_not_reshow_the_secret() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, with_api_key=True)
        cid = made["id"]

        r = await c.post(f"{_base(issuer)}/{cid}/rotate-api-key", headers=owner)
        assert r.status_code == 200, r.text
        rotated = r.json()
        assert rotated["api_key"] is not None
        assert rotated["api_key"].startswith(svc.RAW_KEY_PREFIX)
        assert rotated["api_key"] != made["api_key"]
        # The signing secret is NOT part of this operation's answer.
        assert not rotated["signing_secret"]
        assert made["signing_secret"] not in r.text


async def test_clear_api_key_disarms_the_second_factor() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer, with_api_key=True))["id"]

        r = await c.request("DELETE", f"{_base(issuer)}/{cid}/api-key", headers=owner)
        assert r.status_code == 200, r.text
        assert r.json()["has_api_key"] is False
        row = await _row(c, owner, issuer, cid)
        assert row is not None and row["has_api_key"] is False


# --- authorization ---------------------------------------------------------


async def test_a_member_may_list_but_never_mutate() -> None:
    async with _client() as c:
        owner, member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, with_api_key=True, invoice_mode="draft")
        cid = made["id"]

        # Reading is an operator's job, so the list stays open to a member.
        lst = await c.get(_base(issuer), headers=member)
        assert lst.status_code == 200
        assert [r["id"] for r in lst.json()] == [cid]

        refused = {
            "create": await c.post(
                _base(issuer), headers=member, json={"label": f"x-{uuid.uuid4().hex[:6]}"}
            ),
            "patch": await c.patch(
                f"{_base(issuer)}/{cid}", headers=member, json={"invoice_mode": "transmit"}
            ),
            "rotate_secret": await c.post(
                f"{_base(issuer)}/{cid}/rotate-signing-secret", headers=member
            ),
            "rotate_key": await c.post(f"{_base(issuer)}/{cid}/rotate-api-key", headers=member),
            "clear_key": await c.request(
                "DELETE", f"{_base(issuer)}/{cid}/api-key", headers=member
            ),
            "delete": await c.request("DELETE", f"{_base(issuer)}/{cid}", headers=member),
        }
        for name, r in refused.items():
            assert r.status_code == 403, f"{name}: {r.status_code} {r.text}"
            assert r.json()["code"] == "rbac.role_insufficient", name

        # The refusals left nothing behind: still one connector, still a draft
        # connector, still revoked_at NULL, still armed.
        rows = (await c.get(_base(issuer), headers=owner)).json()
        assert len(rows) == 1
        assert rows[0]["invoice_mode"] == "draft"
        assert rows[0]["revoked_at"] is None
        assert rows[0]["has_api_key"] is True
        assert rows[0]["version"] == made["version"]


# --- partial update --------------------------------------------------------


async def test_patch_writes_only_the_fields_present() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(
            c,
            owner,
            issuer,
            invoice_mode="draft",
            credit_note_mode="off",
            emission_event="payment_intent.succeeded",
            payment_sync_enabled=False,
            series="PC",
            default_purpose="Abbonamenti",
            default_vat_rate="22.00",
            metadata_vat_keys=["piva", "vatId"],
        )
        cid = made["id"]

        r = await c.patch(f"{_base(issuer)}/{cid}", headers=owner, json={"enabled": True})
        assert r.status_code == 200, r.text
        after = r.json()

        assert after["enabled"] is True
        # Everything NOT sent is byte-for-byte what it was: two operators
        # editing different settings must not clobber each other.
        for field in (
            "label",
            "invoice_mode",
            "credit_note_mode",
            "emission_event",
            "payment_sync_enabled",
            "series",
            "default_purpose",
            "metadata_vat_keys",
            "metadata_tax_code_keys",
        ):
            assert after[field] == made[field], field
        assert Decimal(str(after["default_vat_rate"])) == Decimal("22.00")
        assert after["version"] == made["version"] + 1


async def test_patch_with_an_explicit_null_clears_a_nullable_field() -> None:
    """``null`` is a VALUE, not an omission: sending it must clear the column.

    A PATCH implemented with ``exclude_none`` instead of ``exclude_unset`` would
    silently swallow this and leave the old sezionale in place, which for a
    fiscal connector means documents keep landing in a series the operator
    believes they abandoned.
    """
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, series="PC", default_purpose="Abbonamenti")
        cid = made["id"]

        r = await c.patch(f"{_base(issuer)}/{cid}", headers=owner, json={"series": None})
        assert r.status_code == 200, r.text
        assert r.json()["series"] is None
        assert r.json()["default_purpose"] == "Abbonamenti"


async def test_patch_with_an_unknown_field_is_refused_not_silently_ignored() -> None:
    """KNOWN FAILURE -- pins a live defect, do not weaken or skip it.

    A typo'd field name must not answer 200. This is the silent-misconfiguration
    hazard of a fiscal connector: an operator PATCHing ``{"invoicemode":
    "draft"}`` who gets 200 OK *and a bumped version* believes the connector
    stopped transmitting, while it keeps filing with SdI on every payment.

    ``payment_connectors.update_connector`` has exactly this guard -- it raises
    on ``set(values) - PATCHABLE_FIELDS`` -- but the guard is UNREACHABLE from
    HTTP: ``PaymentConnectorPatchIn`` declares precisely the 20 patchable fields
    and pydantic's default ``extra="ignore"`` drops everything else before
    ``model_dump(exclude_unset=True)`` ever runs. So the router hands the
    service an EMPTY mapping, which it happily accepts.

    Observed: 200, ``invoice_mode`` still ``transmit`` (nothing was written) and
    ``version`` incremented anyway -- an optimistic-concurrency bump plus an
    audit row for a write that wrote nothing.

    The fix belongs to the router, not here: ``model_config =
    ConfigDict(extra="forbid")`` on ``PaymentConnectorPatchIn`` turns this into
    the 422 the service already intends. Left failing deliberately.
    """
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, invoice_mode="transmit")
        cid = made["id"]

        r = await c.patch(
            f"{_base(issuer)}/{cid}", headers=owner, json={"invoicemode": "draft", "org_id": cid}
        )
        assert r.status_code in (400, 422), f"unknown field accepted: {r.status_code} {r.text}"
        row = await _row(c, owner, issuer, cid)
        assert row is not None
        assert row["invoice_mode"] == "transmit"
        assert row["version"] == made["version"], "a write that wrote nothing must not bump version"


# --- closed vocabularies ---------------------------------------------------


async def test_create_refuses_every_closed_vocabulary_violation() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cases = {
            "payment_connector.provider_invalid": {"provider": "paypal"},
            "payment_connector.mode_invalid": {"invoice_mode": "sometimes"},
            "payment_connector.emission_event_invalid": {"emission_event": "invoice.exploded"},
        }
        for code, over in cases.items():
            body: dict[str, Any] = {"label": f"bad-{uuid.uuid4().hex[:6]}", **over}
            r = await c.post(_base(issuer), headers=owner, json=body)
            assert r.status_code == 422, f"{code}: {r.status_code} {r.text}"
            assert r.json()["code"] == code

        # credit_note_mode carries the SAME closed set as invoice_mode and is
        # validated independently -- automating invoices while keeping storni
        # manual is a legitimate posture, so both switches must be checked.
        r = await c.post(
            _base(issuer),
            headers=owner,
            json={"label": f"bad-{uuid.uuid4().hex[:6]}", "credit_note_mode": "maybe"},
        )
        assert r.status_code == 422 and r.json()["code"] == "payment_connector.mode_invalid"

        # None of the refused bodies left a row behind.
        assert (await c.get(_base(issuer), headers=owner)).json() == []


async def test_patch_refuses_a_vocabulary_violation_and_changes_nothing() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, invoice_mode="transmit")
        cid = made["id"]

        r = await c.patch(
            f"{_base(issuer)}/{cid}",
            headers=owner,
            json={"invoice_mode": "sometimes", "default_purpose": "should not stick"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "payment_connector.mode_invalid"

        # The refusal is atomic: the legal field travelling with the illegal one
        # must not have been written either.
        row = await _row(c, owner, issuer, cid)
        assert row is not None
        assert row["invoice_mode"] == "transmit"
        assert row["default_purpose"] == made["default_purpose"]
        assert row["version"] == made["version"]


async def test_vocabulary_route_serves_the_closed_sets() -> None:
    """Served rather than duplicated in the SPA, so widening one is a backend
    change only. A member may read it: it is configuration metadata."""
    async with _client() as c:
        _owner, member, _org, _issuer = await _setup(c)
        r = await c.get("/payment-connectors/vocabulary", headers=member)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["providers"] == list(PROVIDERS)
        assert body["automation_modes"] == list(AUTOMATION_MODES)
        assert body["emission_events"] == list(EMISSION_EVENTS)
        assert body["delivery_outcomes"] == list(DELIVERY_OUTCOMES)


# --- lifecycle -------------------------------------------------------------


async def test_delete_revokes_and_only_a_revoked_connector_can_be_purged() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer, enabled=True))["id"]

        # hard=true on a LIVE connector is refused: revoking first is what makes
        # the purge a deliberate second decision.
        r = await c.request("DELETE", f"{_base(issuer)}/{cid}?hard=true", headers=owner)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "payment_connector.not_revoked"
        still = await _row(c, owner, issuer, cid)
        assert still is not None and still["revoked_at"] is None

        # Soft delete: the ingress stops, the row stays readable for
        # reconciliation, and the enable switch is forced off with it.
        assert (
            await c.request("DELETE", f"{_base(issuer)}/{cid}", headers=owner)
        ).status_code == 204
        revoked = await _row(c, owner, issuer, cid)
        assert revoked is not None
        assert revoked["revoked_at"] is not None
        assert revoked["enabled"] is False

        # Purge: gone from the listing entirely.
        r = await c.request("DELETE", f"{_base(issuer)}/{cid}?hard=true", headers=owner)
        assert r.status_code == 204, r.text
        assert await _row(c, owner, issuer, cid) is None


async def test_revoke_is_idempotent_and_keeps_the_first_timestamp() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer))["id"]

        assert (
            await c.request("DELETE", f"{_base(issuer)}/{cid}", headers=owner)
        ).status_code == 204
        first = await _row(c, owner, issuer, cid)
        assert (
            await c.request("DELETE", f"{_base(issuer)}/{cid}", headers=owner)
        ).status_code == 204
        second = await _row(c, owner, issuer, cid)

        assert first is not None and second is not None
        # A second revoke must not move the clock: revoked_at is the moment the
        # ingress actually stopped accepting events.
        assert second["revoked_at"] == first["revoked_at"]
        assert second["version"] == first["version"]


# --- scoping ---------------------------------------------------------------


async def test_a_connector_under_a_sibling_issuer_profile_is_404_everywhere() -> None:
    """The issuer nesting is a hard scope, and the answer is 404, never 403:
    a 403 would confirm that a connector with that id exists in this workspace.
    """
    async with _client() as c:
        owner, _member, org, issuer_a = await _setup(c)
        issuer_b = await _issuer(c, owner, label="Secondaria", vat="11111111119")
        made = await _create(c, owner, issuer_a, with_api_key=True)
        cid = made["id"]
        await _ingest(org, cid, event_id="evt_scope")
        event_uuid = (await c.get(f"{_base(issuer_a)}/{cid}/events", headers=owner)).json()[0]["id"]

        wrong = _base(issuer_b)
        answers = {
            "patch": await c.patch(f"{wrong}/{cid}", headers=owner, json={"enabled": True}),
            "rotate_secret": await c.post(f"{wrong}/{cid}/rotate-signing-secret", headers=owner),
            "rotate_key": await c.post(f"{wrong}/{cid}/rotate-api-key", headers=owner),
            "clear_key": await c.request("DELETE", f"{wrong}/{cid}/api-key", headers=owner),
            "delete": await c.request("DELETE", f"{wrong}/{cid}", headers=owner),
            "events": await c.get(f"{wrong}/{cid}/events", headers=owner),
            "deliveries": await c.get(f"{wrong}/{cid}/deliveries", headers=owner),
            "retry": await c.post(f"{wrong}/{cid}/events/{event_uuid}/retry", headers=owner),
        }
        for name, r in answers.items():
            assert r.status_code == 404, f"{name}: {r.status_code} {r.text}"
            assert r.json()["code"] == "payment_connector.not_found", name

        # The connector is untouched under its own issuer.
        row = await _row(c, owner, issuer_a, cid)
        assert row is not None
        assert row["enabled"] is False
        assert row["revoked_at"] is None
        assert row["version"] == made["version"]
        # ...and it never appears in the sibling profile's listing.
        assert (await c.get(_base(issuer_b), headers=owner)).json() == []


async def test_another_workspace_can_neither_see_nor_touch_this_connector() -> None:
    async with _client() as c:
        owner_a, _member_a, org_a, issuer_a = await _setup(c)
        made = await _create(c, owner_a, issuer_a)
        cid = made["id"]
        await _ingest(org_a, cid, event_id="evt_cross")

        owner_b, _member_b, _org_b, issuer_b = await _setup(c)

        # B addressing A's issuer profile: RLS makes it an empty workspace.
        lst = await c.get(_base(issuer_a), headers=owner_b)
        assert lst.status_code == 200 and lst.json() == []

        answers = {
            "patch": await c.patch(
                f"{_base(issuer_a)}/{cid}", headers=owner_b, json={"enabled": True}
            ),
            "rotate_secret": await c.post(
                f"{_base(issuer_a)}/{cid}/rotate-signing-secret", headers=owner_b
            ),
            "delete": await c.request("DELETE", f"{_base(issuer_a)}/{cid}", headers=owner_b),
            "events": await c.get(f"{_base(issuer_a)}/{cid}/events", headers=owner_b),
            # ...and the same connector id nested under B's OWN issuer profile is
            # equally invisible, so guessing a uuid buys nothing.
            "own_issuer": await c.patch(
                f"{_base(issuer_b)}/{cid}", headers=owner_b, json={"enabled": True}
            ),
        }
        for name, r in answers.items():
            assert r.status_code == 404, f"{name}: {r.status_code} {r.text}"

        # B cannot mint a connector under A's issuer profile either.
        r = await c.post(_base(issuer_a), headers=owner_b, json={"label": "borrowed"})
        assert r.status_code == 404, r.text

        # And A's connector is still exactly as it was.
        row = await _row(c, owner_a, issuer_a, cid)
        assert row is not None and row["enabled"] is False and row["version"] == made["version"]


# --- event and delivery ledgers --------------------------------------------


async def test_events_are_scoped_to_their_connector_and_hide_the_raw_payload() -> None:
    async with _client() as c:
        owner, member, org, issuer = await _setup(c)
        first = (await _create(c, owner, issuer))["id"]
        second = (await _create(c, owner, issuer))["id"]

        await _ingest(org, first, event_id="evt_one")
        await _ingest(org, first, event_id="evt_two", event_type="charge.refunded")
        await _ingest(org, second, event_id="evt_other")

        # A member triages, so the list is not owner-gated.
        r = await c.get(f"{_base(issuer)}/{first}/events", headers=member)
        assert r.status_code == 200, r.text
        rows: list[dict[str, Any]] = r.json()
        assert {row["provider_event_id"] for row in rows} == {"evt_one", "evt_two"}
        assert all(row["status"] == "pending" for row in rows)

        # The raw provider payload carries the counterpart's personal data and
        # is only ever needed by the runner: it must not reach the SPA.
        assert all("payload" not in row for row in rows)

        # The sibling connector's ledger is its own.
        other = (await c.get(f"{_base(issuer)}/{second}/events", headers=member)).json()
        assert {row["provider_event_id"] for row in other} == {"evt_other"}

        # status= filters within the connector, it does not widen the scope.
        empty = await c.get(
            f"{_base(issuer)}/{first}/events", headers=member, params={"status": "needs_attention"}
        )
        assert empty.status_code == 200 and empty.json() == []


async def test_deliveries_are_scoped_and_refused_only_is_the_security_view() -> None:
    async with _client() as c:
        owner, member, org, issuer = await _setup(c)
        first = (await _create(c, owner, issuer))["id"]
        second = (await _create(c, owner, issuer))["id"]

        await _delivery(org, first, outcome="accepted", http_status=200)
        await _delivery(org, first, outcome="duplicate", http_status=200)
        await _delivery(org, first, outcome="signature_invalid", http_status=401)
        await _delivery(org, second, outcome="accepted", http_status=200)

        r = await c.get(f"{_base(issuer)}/{first}/deliveries", headers=member)
        assert r.status_code == 200, r.text
        rows: list[dict[str, Any]] = r.json()
        assert sorted(row["outcome"] for row in rows) == [
            "accepted",
            "duplicate",
            "signature_invalid",
        ]
        # The body is represented by its digest, never reproduced: a refused
        # delivery's bytes are unauthenticated attacker-controlled input.
        assert all("body" not in row or row.get("body") is None for row in rows)
        assert all(len(row["body_sha256_hex"]) == 64 for row in rows)

        refused = (
            await c.get(
                f"{_base(issuer)}/{first}/deliveries",
                headers=member,
                params={"refused_only": "true"},
            )
        ).json()
        assert [row["outcome"] for row in refused] == ["signature_invalid"]
        assert refused[0]["http_status"] == 401

        # An explicit outcome filter is exact, and the scope still holds.
        only_dup = (
            await c.get(
                f"{_base(issuer)}/{first}/deliveries",
                headers=member,
                params={"outcome": "duplicate"},
            )
        ).json()
        assert [row["outcome"] for row in only_dup] == ["duplicate"]

        other = (await c.get(f"{_base(issuer)}/{second}/deliveries", headers=member)).json()
        assert [row["outcome"] for row in other] == ["accepted"]


async def test_retry_rearms_a_parked_event_and_refuses_a_live_one() -> None:
    async with _client() as c:
        owner, member, org, issuer = await _setup(c)
        # enabled: a disabled connector short-circuits every event to 'ignored',
        # which would never reach the quarantine this test needs.
        cid = (await _create(c, owner, issuer, enabled=True))["id"]

        # An event with no ``data.object`` is deterministically un-processable,
        # so the runner parks it in needs_attention after one attempt.
        await _ingest(
            org, cid, event_id="evt_park", payload={"id": "evt_park", "type": "invoice.paid"}
        )
        parked_id = (await c.get(f"{_base(issuer)}/{cid}/events", headers=owner)).json()[0]["id"]
        assert await _run(org, cid, parked_id) == "needs_attention"

        before = (await c.get(f"{_base(issuer)}/{cid}/events", headers=owner)).json()[0]
        assert before["status"] == "needs_attention"
        assert before["attempt_count"] == 1
        assert before["last_error"] == "payload_invalid"

        r = await c.post(f"{_base(issuer)}/{cid}/events/{parked_id}/retry", headers=member)
        assert r.status_code == 200, r.text
        rearmed = r.json()
        assert rearmed["status"] == "pending"
        # The budget is reset: the spent attempts measured a condition the
        # operator has just fixed.
        assert rearmed["attempt_count"] == 0
        assert rearmed["last_error"] is None
        assert rearmed["error_detail"] is None


async def test_retry_refuses_an_event_that_is_pending_or_done() -> None:
    async with _client() as c:
        owner, _member, org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer, enabled=True))["id"]

        await _ingest(org, cid, event_id="evt_live")
        live = (await c.get(f"{_base(issuer)}/{cid}/events", headers=owner)).json()[0]["id"]

        # Freshly ingested = pending = a worker may claim it at any moment.
        # Re-arming it would race the lease.
        r = await c.post(f"{_base(issuer)}/{cid}/events/{live}/retry", headers=owner)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "payment_connector.event_not_retryable"

        # A settled event is not re-runnable either: it already produced (or
        # deliberately did not produce) its document.
        await _force_status(org, cid, live, "done")
        r = await c.post(f"{_base(issuer)}/{cid}/events/{live}/retry", headers=owner)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "payment_connector.event_not_retryable"

        after = (await c.get(f"{_base(issuer)}/{cid}/events", headers=owner)).json()[0]
        assert after["status"] == "done", "a refused retry must not move the event"


async def test_retry_of_an_unknown_event_is_404() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        cid = (await _create(c, owner, issuer))["id"]
        r = await c.post(
            f"{_base(issuer)}/{cid}/events/{uuid.uuid4()}/retry",
            headers=owner,
        )
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "payment_connector.event_not_found"


# --- shadow mode over HTTP -------------------------------------------------


async def test_dry_run_xml_download_is_issuer_scoped_and_404s_when_absent() -> None:
    """The generated XML is the deliverable of a shadow run, and it carries the
    counterpart's fiscal data: it must be reachable only through the connector
    that produced it."""
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, invoice_mode="dry_run")
        cid = made["id"]

        # A connector under a SIBLING issuer profile of the same org must not
        # be a path to it: 404, never 403.
        other = await _issuer(c, owner, label="Secondo", vat="09876543210")
        r = await c.get(
            f"/issuer-profiles/{other}/payment-connectors/{cid}/events/{uuid.uuid4()}/dry-run-xml",
            headers=owner,
        )
        assert r.status_code == 404, r.text

        # An event id that does not exist, or one with no shadow document, is
        # the same 404 -- the surface is not an event-existence oracle.
        r = await c.get(f"{_base(issuer)}/{cid}/events/{uuid.uuid4()}/dry-run-xml", headers=owner)
        assert r.status_code == 404, r.text


async def test_discard_dry_run_is_owner_gated_and_reports_what_it_removed() -> None:
    """Discarding a shadow run deletes documents. It is a mutation, so it is
    owner-gated like every other mutation on this surface."""
    async with _client() as c:
        owner, member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer, invoice_mode="dry_run")
        cid = made["id"]

        r = await c.post(f"{_base(issuer)}/{cid}/discard-dry-run", headers=member)
        assert r.status_code == 403, r.text

        r = await c.post(f"{_base(issuer)}/{cid}/discard-dry-run", headers=owner)
        assert r.status_code == 200, r.text
        # Nothing was shadowed yet, so nothing is discarded -- and the call is
        # a clean no-op rather than an error, so it stays safe to repeat.
        assert r.json() == {"discarded": 0}

        r = await c.post(
            f"/issuer-profiles/{uuid.uuid4()}/payment-connectors/{cid}/discard-dry-run",
            headers=owner,
        )
        assert r.status_code == 404, r.text


async def test_the_vocabulary_advertises_dry_run() -> None:
    """The SPA renders the modes from the backend, so a widened set must reach
    it without a frontend release."""
    async with _client() as c:
        owner, _member, _org, _issuer = await _setup(c)
        r = await c.get("/payment-connectors/vocabulary", headers=owner)
        assert r.status_code == 200, r.text
        assert "dry_run" in r.json()["automation_modes"]


async def test_assigning_an_incomplete_client_is_a_422_that_names_the_fields() -> None:
    """The refusal has to reach the operator as something they can act on.

    ``MissingBillingDataError`` is the runner's INTERNAL control flow and is not
    a DomainError, so letting it escape a request surfaces as an opaque 500 with
    none of the field list. The service-level test could not see that -- it
    asserted the exception, which is exactly the shape the HTTP layer cannot
    render. This one drives the real route.
    """
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer)
        cid = made["id"]

        # A client with a VAT number and an address but no way to deliver to it.
        r = await c.post(
            "/clients",
            headers=owner,
            json={
                "name": "Incompleta Srl",
                "legal_name": "Incompleta Srl",
                "country_code": "IT",
                "vat_number": "09876543210",
                "address": "Via Milano 9",
                "postal_code": "20100",
                "city": "Milano",
                "province": "MI",
                "country": "IT",
            },
        )
        assert r.status_code in (200, 201), r.text
        client_tag_id = r.json()["id"]

        r = await c.post(
            f"{_base(issuer)}/{cid}/assign-customer",
            headers=owner,
            json={"provider_customer_id": "cus_probe", "client_tag_id": client_tag_id},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["code"] == "payment_connector.client_incomplete"
        assert "sdi_code|pec" in body["detail"], body


async def test_assigning_a_complete_client_reports_what_it_rearmed() -> None:
    async with _client() as c:
        owner, _member, _org, issuer = await _setup(c)
        made = await _create(c, owner, issuer)
        cid = made["id"]

        r = await c.post(
            "/clients",
            headers=owner,
            json={
                "name": "Acme SpA",
                "legal_name": "Acme SpA",
                "country_code": "IT",
                "vat_number": "09876543210",
                "address": "Via Milano 9",
                "postal_code": "20100",
                "city": "Milano",
                "province": "MI",
                "country": "IT",
                "sdi_code": "ABCDEFG",
            },
        )
        assert r.status_code in (200, 201), r.text

        r = await c.post(
            f"{_base(issuer)}/{cid}/assign-customer",
            headers=owner,
            json={"provider_customer_id": "cus_ok", "client_tag_id": r.json()["id"]},
        )
        assert r.status_code == 200, r.text
        # Nothing was waiting on this customer, so the association is a clean
        # no-op rather than an error: it stays safe to do ahead of time.
        assert r.json() == {"rearmed": 0}
