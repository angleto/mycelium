"""Payment-connector management router (ADR-0051).

REST + GUI only, never MCP: configuring a connector mints the credential that
lets an outside system emit fiscal documents in this org's name, which is the
same chicken-and-egg carve-out that keeps issuer keys and agent tokens off the
assistant surface. A thin adapter over
``mycelium_core.services.payment_connectors``; every mutation is owner-gated
INSIDE the service. Nested under the issuer profile the connector issues for,
like ``api-keys`` and ``webhook-endpoints``.

- ``GET    /issuer-profiles/{id}/payment-connectors``                 -> [Out]
- ``POST   /issuer-profiles/{id}/payment-connectors``                 -> CreateOut (secrets once)
- ``PATCH  /issuer-profiles/{id}/payment-connectors/{cid}``           -> Out
- ``POST   .../{cid}/rotate-signing-secret``                          -> CreateOut (secret once)
- ``POST   .../{cid}/rotate-api-key``                                 -> CreateOut (key once)
- ``DELETE .../{cid}/api-key``                                        -> Out (drops the 2nd factor)
- ``DELETE .../{cid}``                                                -> 204 revoke, ``?hard`` purge
- ``GET    .../{cid}/events``                                         -> [EventOut] (triage)
- ``POST   .../{cid}/events/{eid}/retry``                             -> EventOut

The secrets appear in a response body exactly twice in this module's lifetime --
at create and at rotate -- and never in a read route.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal, NamedTuple

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_core.config import get_settings
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.invoice import Invoice, InvoiceState
from mycelium_core.models.membership import Role
from mycelium_core.models.payment_connector import (
    AUTOMATION_MODES,
    DELIVERY_OUTCOMES,
    EMISSION_EVENTS,
    PROVIDERS,
    REFUND_EVENTS,
    VAT_PRICING,
    PaymentConnector,
    PaymentConnectorEvent,
    PaymentWebhookDelivery,
)
from mycelium_core.services import payment_connectors as svc
from mycelium_core.services.rbac import ensure_role

router = APIRouter(tags=["payment-connectors"])


# --- request / response models ---------------------------------------------


#: A supplied signing secret is the ENTIRE authority of a public unauthenticated
#: endpoint, so it has a floor. 16 characters admits every real provider secret
#: (Stripe's ``whsec_`` values are far longer) while refusing the hand-typed
#: short string an operator might otherwise reach for now that pasting your own
#: key is an offered path on the native contract, where Mycelium would
#: otherwise have minted 32 bytes of entropy.
_MIN_SIGNING_SECRET = 16


class PaymentConnectorIn(BaseModel):
    # Refuse what we do not understand. With the default extra="ignore" a
    # misspelled field is dropped silently and the caller is told 200 OK, which
    # on this surface means being told a connector was configured a way it was
    # not -- and on a fiscal connector that reads as "it stopped filing" while
    # it keeps filing.
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(default="stripe", max_length=20)
    #: Stripe's own ``whsec_...``, copied from the Stripe dashboard when the
    #: endpoint is created there. Omitted for the native contract, where
    #: Mycelium is the authority and mints one.
    signing_secret: str | None = Field(default=None, min_length=_MIN_SIGNING_SECRET, max_length=200)
    #: Mint the optional second ingress factor as well.
    with_api_key: bool = False
    enabled: bool = False
    invoice_mode: str = "transmit"
    credit_note_mode: str = "transmit"
    emission_event: str = "invoice.paid"
    refund_event: str = "refund.created"
    payment_sync_enabled: bool = True
    numbering: Literal["client", "series", "provider"] = "client"
    series: str | None = Field(default=None, max_length=20)
    default_purpose: str | None = Field(default=None, max_length=200)
    default_vat_rate: Decimal | None = None
    default_vat_nature: str | None = Field(default=None, max_length=4)
    default_line_description: str | None = Field(default=None, max_length=200)
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_country_code: str | None = Field(default=None, max_length=2)
    vat_pricing: str = "auto"
    # Ordered candidate key names per field, not one name: a real provider
    # account accumulates spellings for the same field. First present wins, so
    # the order is the precedence -- current spelling first, legacy as a tail.
    metadata_vat_keys: list[str] = Field(default=["vatId", "vat_number", "partita_iva"])
    metadata_tax_code_keys: list[str] = Field(default=["fiscal_code", "tax_code", "codice_fiscale"])
    metadata_sdi_keys: list[str] = Field(default=["codice_destinatario", "sdi_code", "sdi"])
    metadata_pec_keys: list[str] = Field(default=["pec"])


class PaymentConnectorPatchIn(BaseModel):
    """Every field optional; only the ones present are written, so two admins
    editing different settings do not clobber each other's.

    ``extra="forbid"`` is load-bearing rather than tidy: without it a PATCH of
    ``{"invoicemode": "draft"}`` answers 200 OK having written nothing, and the
    operator believes the connector stopped transmitting while it keeps filing
    documents with SdI on every payment. It also makes the service's own
    unknown-field guard reachable instead of dead code.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    invoice_mode: str | None = None
    credit_note_mode: str | None = None
    emission_event: str | None = None
    refund_event: str | None = None
    payment_sync_enabled: bool | None = None
    numbering: Literal["client", "series", "provider"] | None = None
    series: str | None = Field(default=None, max_length=20)
    default_purpose: str | None = Field(default=None, max_length=200)
    default_vat_rate: Decimal | None = None
    default_vat_nature: str | None = Field(default=None, max_length=4)
    default_line_description: str | None = Field(default=None, max_length=200)
    default_payment_conditions_code: str | None = Field(default=None, max_length=4)
    default_payment_method_code: str | None = Field(default=None, max_length=4)
    default_country_code: str | None = Field(default=None, max_length=2)
    vat_pricing: str | None = None
    metadata_vat_keys: list[str] | None = None
    metadata_tax_code_keys: list[str] | None = None
    metadata_sdi_keys: list[str] | None = None
    metadata_pec_keys: list[str] | None = None


class SubscriptionEventOut(BaseModel):
    """One event the provider must be configured to deliver.

    ``purpose`` is a stable key, not prose: the SPA owns the wording, so the
    explanation exists once per language instead of once in English here and
    again in every translation.
    """

    event_type: str
    purpose: str
    required: bool


class PaymentConnectorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    issuer_profile_id: uuid.UUID
    provider: str
    label: str
    enabled: bool
    invoice_mode: str
    credit_note_mode: str
    emission_event: str
    payment_sync_enabled: bool
    numbering: str
    series: str | None
    default_purpose: str | None
    default_vat_rate: Decimal | None
    default_vat_nature: str | None
    default_line_description: str | None
    default_payment_conditions_code: str | None
    default_payment_method_code: str | None
    default_country_code: str | None
    vat_pricing: str
    metadata_vat_keys: list[str]
    metadata_tax_code_keys: list[str]
    metadata_sdi_keys: list[str]
    metadata_pec_keys: list[str]
    created_at: datetime.datetime
    updated_at: datetime.datetime
    revoked_at: datetime.datetime | None
    last_event_at: datetime.datetime | None
    version: int
    refund_event: str
    #: Never the secret itself -- only whether one is installed at all. False
    #: means the connector exists but cannot verify a delivery yet, which is the
    #: normal state between creating it and registering its URL at the provider.
    has_signing_secret: bool
    #: Never the secret itself -- only whether the second factor is armed.
    has_api_key: bool
    #: The URL to paste into the provider's dashboard.
    webhook_url: str
    #: The events to enable alongside that URL, derived from THIS connector's
    #: settings by the mapper that will receive them. Served with the connector
    #: rather than written down in a runbook because the answer changes with the
    #: configuration, and a checklist that has drifted is worse than none.
    subscription: list[SubscriptionEventOut]


class PaymentConnectorCreateOut(PaymentConnectorOut):
    """The only shape that ever carries plaintext credentials.

    ``signing_secret`` is NULL when there is nothing to show: a vendor connector
    is created before its provider has issued one, and the SPA's next
    instruction is to register the webhook URL rather than to copy a secret.
    """

    signing_secret: str | None = None
    api_key: str | None = None


class RotateSigningSecretIn(BaseModel):
    """The new signing secret, in the BODY.

    A query string is logged verbatim by ordinary access logging (nginx's
    combined format records ``$request``) and is the part of a request that
    routinely reaches proxy logs, APM traces and browser history. Every other
    credential here is deliberately hardened -- Fernet at rest, peppered hash
    for the ingress key, never echoed by a read route -- so carrying this one in
    a URL would be the single weak link.
    """

    model_config = ConfigDict(extra="forbid")

    signing_secret: str | None = Field(default=None, min_length=_MIN_SIGNING_SECRET, max_length=200)


class EventActionsOut(BaseModel):
    """What this row may actually do, decided HERE and rendered as given.

    The SPA used to re-derive these rules and got them wrong in both
    directions: Retry was offered whenever the status was not ``done`` while
    the service accepts exactly three statuses, so ``ignored`` and ``pending``
    rows showed a button that answered 409; and "Make sendable" was offered to
    every role while its route is owner-only, so a member got a 403 on click.
    A capability advertised and then refused is worse than an absent one --
    the operator cannot tell a permission problem from a broken feature.

    Deciding it once on the server is also what keeps the actions from
    contradicting each other as more are added: they are computed from one
    view of the row, not from four independent guesses in TSX.
    """

    #: ``retry_event`` accepts these three statuses and no others.
    retry: bool
    #: Owner-only at the router, and only meaningful for a shadow document.
    promote: bool
    #: Owner-only too, and only while the document is STILL a shadow: promoting
    #: clears ``dry_run`` on the invoice, and re-shooting a comparison artefact
    #: for a document nobody is comparing any more is meaningless.
    reshoot: bool
    #: Throw the composed document away and re-run the payload. Offered only
    #: for an untouched draft: a number or a file name already allocated means
    #: a send was attempted, and deleting that draft would burn a fiscal
    #: number and destroy the NomeFile dedupe that makes a resend safe. The
    #: server re-checks all of it -- this only decides whether to show it.
    recompose: bool
    #: Retry on an event that ALREADY composed a document does not recompose
    #: it: ``_process_emission`` short-circuits on the object claim into
    #: ``_settle``, which on a transmit-mode connector files the existing
    #: draft as it stands. That is the opposite of what "retry" suggests, so
    #: the button says what it will do instead of what it is called.
    settles_existing_draft: bool


class PaymentConnectorEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_event_id: str
    event_type: str
    status: str
    attempt_count: int
    max_attempts: int
    occurred_at: datetime.datetime | None
    created_at: datetime.datetime
    processed_at: datetime.datetime | None
    next_attempt_at: datetime.datetime
    last_error: str | None
    error_detail: str | None
    invoice_id: uuid.UUID | None
    #: The provider customer a parked payment is waiting on. It is what the
    #: operator associates with a client to unblock it, so it has to be visible
    #: on the row rather than buried in the frozen payload.
    provider_customer_id: str | None
    #: The counterpart as the PAYLOAD named them, derived on read rather than
    #: stored: a provider customer id identifies a row in someone else's
    #: system, and an operator deciding what to do with a parked payment needs
    #: the company it belongs to. Null when the event named nobody -- a refund
    #: and a reconciliation legitimately do not.
    counterpart_name: str | None
    counterpart_email: str | None
    dry_run: bool
    #: What this row may do, computed server-side. See ``EventActionsOut``.
    actions: EventActionsOut
    #: Whether a shadow document was produced and can be downloaded. The XML
    #: itself is not projected here: it is large and carries the counterpart's
    #: data, so it has its own route.
    has_dry_run_xml: bool


class PaymentWebhookDeliveryOut(BaseModel):
    """One inbound delivery attempt. The body is represented by its digest, not
    reproduced: a refused delivery's bytes are unauthenticated input."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    outcome: str
    http_status: int
    provider_event_id: str | None
    event_id: uuid.UUID | None
    body_bytes: int
    body_sha256_hex: str | None
    signature_present: bool
    api_key_present: bool
    received_at: datetime.datetime


class ConnectorVocabularyOut(BaseModel):
    """The closed vocabularies, served rather than duplicated in the SPA, so a
    widening is a backend change only."""

    providers: list[str]
    automation_modes: list[str]
    emission_events: list[str]
    refund_events: list[str]
    vat_pricings: list[str]
    delivery_outcomes: list[str]


# --- helpers ---------------------------------------------------------------


def _webhook_url(connector: PaymentConnector) -> str:
    s = get_settings()
    base = (s.payment_connector_base_url or s.frontend_base_url or "").rstrip("/")
    return f"{base}/api/v1/connectors/{connector.provider}/{connector.id}"


#: Fields of the DTO that are computed here rather than copied off the row.
_DERIVED_FIELDS = frozenset({"has_signing_secret", "has_api_key", "webhook_url", "subscription"})


def _out(row: PaymentConnector) -> PaymentConnectorOut:
    return PaymentConnectorOut(
        **{
            field: getattr(row, field)
            for field in PaymentConnectorOut.model_fields
            if field not in _DERIVED_FIELDS
        },
        has_signing_secret=row.signing_secret_ciphertext is not None,
        has_api_key=row.api_key_hash is not None,
        webhook_url=_webhook_url(row),
        subscription=[
            SubscriptionEventOut(
                event_type=event.event_type, purpose=event.purpose, required=event.required
            )
            for event in svc.subscription_for(row)
        ],
    )


def _create_out(
    row: PaymentConnector, signing_secret: str | None, api_key: str | None
) -> PaymentConnectorCreateOut:
    return PaymentConnectorCreateOut(
        **_out(row).model_dump(), signing_secret=signing_secret, api_key=api_key
    )


#: Fields of the event DTO computed here rather than copied off the row.
_DERIVED_EVENT_FIELDS = frozenset(
    {"has_dry_run_xml", "counterpart_name", "counterpart_email", "actions"}
)


class _LinkedDrafts(NamedTuple):
    """Which of the documents a page of events composed are still drafts, and
    which of those are still SHADOW drafts.

    The two are not the same question and the row needs both: a retry settles
    any draft, while re-shooting only makes sense while the document is still a
    comparison artefact. ``promote_dry_run`` clears ``dry_run`` on the invoice,
    so a promoted document is a plain draft and must stop offering it.
    """

    drafts: frozenset[uuid.UUID]
    shadow_drafts: frozenset[uuid.UUID]
    #: Drafts on which nothing has been spent yet: no fiscal number, no file
    #: name, no frozen XML. Those three being NULL is what makes a document
    #: safe to delete and re-derive.
    recomposable_drafts: frozenset[uuid.UUID]


#: A module-level empty value so the projection can be called without a page
#: lookup (a single-row route, a test) without a mutable-looking default.
_NO_LINKED_DRAFTS = _LinkedDrafts(frozenset(), frozenset(), frozenset())


async def _draft_invoice_ids(
    ctx: TenantCtx, rows: Sequence[PaymentConnectorEvent]
) -> _LinkedDrafts:
    """Read in one statement for the whole page, not one query per row. RLS
    scopes it to the tenant, so an id belonging to another org simply does not
    come back."""
    ids = {r.invoice_id for r in rows if r.invoice_id is not None}
    if not ids:
        return _NO_LINKED_DRAFTS
    found = (
        await ctx.session.execute(
            select(
                Invoice.id, Invoice.dry_run, Invoice.number, Invoice.nome_file, Invoice.xml
            ).where(Invoice.id.in_(ids), Invoice.state == InvoiceState.draft)
        )
    ).all()
    return _LinkedDrafts(
        frozenset(r[0] for r in found),
        frozenset(r[0] for r in found if r[1]),
        frozenset(r[0] for r in found if r[2] is None and r[3] is None and r[4] is None),
    )


def _event_out(
    row: PaymentConnectorEvent,
    *,
    provider: str,
    role: Role,
    linked: _LinkedDrafts = _NO_LINKED_DRAFTS,
) -> PaymentConnectorEventOut:
    counterpart = svc.counterpart_of(row, provider=provider)
    return PaymentConnectorEventOut(
        **{
            f: getattr(row, f)
            for f in PaymentConnectorEventOut.model_fields
            if f not in _DERIVED_EVENT_FIELDS
        },
        has_dry_run_xml=row.dry_run_xml is not None,
        counterpart_name=counterpart.name,
        counterpart_email=counterpart.email,
        actions=EventActionsOut(
            retry=row.status in svc.RETRYABLE_EVENT_STATUSES,
            promote=row.dry_run_xml is not None and role is Role.owner,
            reshoot=(
                row.dry_run_xml is not None
                and role is Role.owner
                and row.invoice_id in linked.shadow_drafts
            ),
            recompose=(
                role is Role.owner
                and row.status not in {"pending", "processing"}
                and row.invoice_id in linked.recomposable_drafts
            ),
            settles_existing_draft=row.invoice_id in linked.drafts,
        ),
    )


async def _assert_in_issuer(
    ctx: TenantCtx, issuer_profile_id: uuid.UUID, connector_id: uuid.UUID
) -> str:
    """The nested connector must belong to the path's issuer profile. 404 on a
    mismatch, never 403: the surface must not confirm a connector under another
    issuer.

    Returns the connector's ``provider``: every route already runs this check,
    and the event projection needs to know whose dialect the stored payloads
    are in. Reading it here costs nothing (the same row, one more column) and
    saves a second query on the two routes that project events.
    """
    found = (
        await ctx.session.execute(
            select(PaymentConnector.provider).where(
                PaymentConnector.id == connector_id,
                PaymentConnector.issuer_profile_id == issuer_profile_id,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_NOT_FOUND)
    return found


# --- endpoints -------------------------------------------------------------


@router.get("/payment-connectors/vocabulary", response_model=ConnectorVocabularyOut)
async def vocabulary(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ConnectorVocabularyOut:
    return ConnectorVocabularyOut(
        providers=list(PROVIDERS),
        automation_modes=list(AUTOMATION_MODES),
        emission_events=list(EMISSION_EVENTS),
        refund_events=list(REFUND_EVENTS),
        vat_pricings=list(VAT_PRICING),
        delivery_outcomes=list(DELIVERY_OUTCOMES),
    )


@router.get(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors",
    response_model=list[PaymentConnectorOut],
)
async def list_connectors(
    issuer_profile_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[PaymentConnectorOut]:
    rows = await svc.list_connectors(
        ctx.session, org_id=ctx.org_id, issuer_profile_id=issuer_profile_id
    )
    return [_out(r) for r in rows]


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors",
    response_model=PaymentConnectorCreateOut,
)
async def create_connector(
    issuer_profile_id: uuid.UUID,
    body: PaymentConnectorIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorCreateOut:
    """Create a connector and return its credentials ONCE."""
    ensure_role(ctx.role, Role.owner)
    fields: dict[str, Any] = body.model_dump(
        exclude={"label", "provider", "signing_secret", "with_api_key"}
    )
    row, secret, key = await svc.create_connector(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        issuer_profile_id=issuer_profile_id,
        label=body.label,
        provider=body.provider,
        signing_secret=body.signing_secret,
        with_api_key=body.with_api_key,
        **fields,
    )
    return _create_out(row, secret, key)


@router.patch(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}",
    response_model=PaymentConnectorOut,
)
async def update_connector(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    body: PaymentConnectorPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorOut:
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    values = body.model_dump(exclude_unset=True)
    row = await svc.update_connector(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        values=values,
    )
    return _out(row)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/rotate-signing-secret",
    response_model=PaymentConnectorCreateOut,
)
async def rotate_signing_secret(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    body: RotateSigningSecretIn | None = None,
) -> PaymentConnectorCreateOut:
    """Install a new signing secret; the old one keeps verifying for the grace
    window so events already queued at the provider are not lost."""
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row, secret = await svc.rotate_signing_secret(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        signing_secret=body.signing_secret if body else None,
    )
    return _create_out(row, secret, None)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/rotate-api-key",
    response_model=PaymentConnectorCreateOut,
)
async def rotate_api_key(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorCreateOut:
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row, key = await svc.rotate_api_key(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, connector_id=connector_id
    )
    # The signing secret is NOT re-shown here: rotating one credential must not
    # re-expose the other.
    return _create_out(row, "", key)


@router.delete(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/api-key",
    response_model=PaymentConnectorOut,
)
async def clear_api_key(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorOut:
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = await svc.clear_api_key(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, connector_id=connector_id
    )
    return _out(row)


@router.delete(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connector(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    hard: Annotated[bool, Query()] = False,
) -> None:
    """Revoke (default) or purge an already-revoked connector (``?hard=true``).

    Two-stage like every other credential here: revoking stops the ingress
    immediately while the event history stays readable for reconciliation.
    """
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    if hard:
        await svc.purge_connector(
            ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, connector_id=connector_id
        )
        return
    await svc.revoke_connector(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, connector_id=connector_id
    )


@router.get(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events",
    response_model=list[PaymentConnectorEventOut],
)
async def list_events(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    event_status: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PaymentConnectorEventOut]:
    """The triage list. Filter by ``status=needs_attention`` for the quarantine.

    The raw provider payload is still NOT projected: it is a nested vendor
    object carrying the counterpart's full record, and it has its own route for
    when an operator asks to see exactly what arrived. What IS lifted out of it
    is the counterpart's name and email -- deliberately, because the column
    used to show a provider customer id and nothing else, which identifies the
    payment in Stripe's database and not in the operator's head.
    """
    provider = await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    rows: list[PaymentConnectorEvent] = await svc.list_events(
        ctx.session,
        org_id=ctx.org_id,
        connector_id=connector_id,
        status=event_status,
        limit=limit,
    )
    # One extra query, not one per row: which of the linked documents are still
    # DRAFTS. That is what decides whether Retry would compose something or
    # merely settle a document this event already produced -- a distinction the
    # operator cannot make from the row otherwise, and the one that turns
    # "retry" into "file the pre-fix draft as it stands".
    linked = await _draft_invoice_ids(ctx, rows)
    return [_event_out(r, provider=provider, role=ctx.role, linked=linked) for r in rows]


@router.get(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/deliveries",
    response_model=list[PaymentWebhookDeliveryOut],
)
async def list_deliveries(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    refused_only: Annotated[bool, Query()] = False,
    outcome: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PaymentWebhookDeliveryOut]:
    """The delivery ledger: every inbound request this connector received.

    ``refused_only=true`` is the security view (bad signature, disabled,
    malformed). This is what answers "the provider says it delivered it".
    """
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    rows: list[PaymentWebhookDelivery] = await svc.list_deliveries(
        ctx.session,
        org_id=ctx.org_id,
        connector_id=connector_id,
        outcome=outcome,
        refused_only=refused_only,
        limit=limit,
    )
    return [
        PaymentWebhookDeliveryOut(
            id=d.id,
            provider=d.provider,
            outcome=d.outcome,
            http_status=d.http_status,
            provider_event_id=d.provider_event_id,
            event_id=d.event_id,
            body_bytes=d.body_bytes,
            body_sha256_hex=d.body_sha256.hex() if d.body_sha256 else None,
            signature_present=d.signature_present,
            api_key_present=d.api_key_present,
            received_at=d.received_at,
        )
        for d in rows
    ]


class AssignCustomerClientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_customer_id: str = Field(min_length=1, max_length=255)
    client_tag_id: uuid.UUID


class AssignCustomerClientOut(BaseModel):
    #: Payments this association just returned to the queue.
    rearmed: int


class DiscardDryRunOut(BaseModel):
    discarded: int


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/assign-customer",
    response_model=AssignCustomerClientOut,
)
async def assign_customer_client(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    body: AssignCustomerClientIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AssignCustomerClientOut:
    """Point a provider customer at an existing client, by hand.

    The manual half of "the customer supplied their billing data late". The
    automatic half needs the data to arrive THROUGH the provider; when it
    arrives any other way there is nothing tying the mycelium client to the
    provider's customer id, so a retry alone can never unblock those payments.

    Refuses a client that is not yet invoiceable, naming what is missing, so
    the failure lands on the record the operator is looking at rather than on
    a retry later.
    """
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    rearmed = await svc.assign_customer_client(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        provider_customer_id=body.provider_customer_id,
        client_tag_id=body.client_tag_id,
    )
    return AssignCustomerClientOut(rearmed=rearmed)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/discard-dry-run",
    response_model=DiscardDryRunOut,
)
async def discard_dry_run(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> DiscardDryRunOut:
    """Throw away everything a shadow run composed: its claims and its drafts.

    The shadow claims have to go first: their FK to the invoice is RESTRICT (it
    is what stops a live claim from ever dangling), so the drafts cannot be
    deleted while the claims pin them.
    """
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    discarded = await svc.discard_dry_run(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, connector_id=connector_id
    )
    return DiscardDryRunOut(discarded=discarded)


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
    "/events/{event_id}/recompose",
    response_model=PaymentConnectorEventOut,
)
async def recompose_event(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorEventOut:
    """Discard the document this event composed and re-run its frozen payload.

    Owner-only: it deletes a document and re-arms an emission, which is the
    same weight as promoting or discarding a shadow run, not the weight of
    reading the ledger. Refuses anything but an untouched draft.
    """
    ensure_role(ctx.role, Role.owner)
    provider = await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = await svc.recompose_event(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        event_id=event_id,
    )
    return _event_out(
        row, provider=provider, role=ctx.role, linked=await _draft_invoice_ids(ctx, [row])
    )


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
    "/events/{event_id}/reshoot-dry-run-xml",
    response_model=PaymentConnectorEventOut,
)
async def reshoot_dry_run_xml(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorEventOut:
    """Rebuild this event's frozen shadow document from the invoice as it is now.

    Owner-only, like the other two verbs that act on the shadow universe: it
    REPLACES the artefact a parallel run was diffing against the incumbent, so
    it is a decision about the comparison, not a way of reading it.

    A document that no longer validates answers with the validator's own words
    rather than a success and a stale blob.
    """
    ensure_role(ctx.role, Role.owner)
    provider = await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = await svc.reshoot_dry_run_xml(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        event_id=event_id,
    )
    return _event_out(
        row,
        provider=provider,
        role=ctx.role,
        linked=await _draft_invoice_ids(ctx, [row]),
    )


class PromoteDryRunOut(BaseModel):
    """The promoted document, identified enough to open it in the invoice list."""

    invoice_id: uuid.UUID
    series: str
    year: int
    #: NULL until the document is really transmitted: promotion deliberately
    #: does not allocate a fiscal number, so a document takes its number when it
    #: is filed and not a moment earlier.
    number: int | None


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
    "/events/{event_id}/promote",
    response_model=PromoteDryRunOut,
)
async def promote_dry_run(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PromoteDryRunOut:
    """Turn one shadow document into a real, sendable draft.

    The counterpart of ``discard-dry-run``, per document rather than per
    connector: discard is for the payments the incumbent provider invoiced,
    this is for the ones Mycelium has to invoice after all. It moves the claim
    rows out of the shadow universe with the document, so a later redelivery of
    the same payment resolves to it instead of composing a second invoice, and
    it refuses outright when a real document already covers that payment.

    Owner-only. It converts a comparison artefact into something that can be
    filed in the workspace's name, which is a different decision from reading
    the ledger.
    """
    ensure_role(ctx.role, Role.owner)
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    # Scoped by connector, like every other by-event route here: an event id
    # from another connector must read as absent, not as forbidden.
    event = (
        await ctx.session.execute(
            select(PaymentConnectorEvent).where(
                PaymentConnectorEvent.id == event_id,
                PaymentConnectorEvent.connector_id == connector_id,
            )
        )
    ).scalar_one_or_none()
    if event is None or event.invoice_id is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_FOUND)
    inv = await svc.promote_dry_run(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        invoice_id=event.invoice_id,
    )
    return PromoteDryRunOut(invoice_id=inv.id, series=inv.series, year=inv.year, number=inv.number)


@router.get(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
    "/events/{event_id}/payload"
)
async def event_payload(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """The provider event exactly as it arrived.

    Not projected onto the event LIST, and served per event instead: the list is
    a triage surface read many rows at a time, and a Stripe event is a large
    nested object carrying the counterpart's fiscal identity. Fetching one on
    demand keeps the common read small and makes looking at a customer's data a
    deliberate act rather than a side effect of opening a page.

    Safe to serve, and this is the line that matters: an EVENT is authenticated
    -- it exists only because a MAC computed with the connector's signing secret
    verified over these exact bytes -- so its payload is provider-authored. The
    delivery ledger, which records what we turned AWAY, is attacker-controlled
    and therefore keeps a digest and never the body.
    """
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = (
        await ctx.session.execute(
            select(PaymentConnectorEvent).where(
                PaymentConnectorEvent.id == event_id,
                PaymentConnectorEvent.connector_id == connector_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_FOUND)
    return Response(
        content=json.dumps(row.payload, indent=2, ensure_ascii=False, sort_keys=True),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}"
    "/events/{event_id}/dry-run-xml"
)
async def download_dry_run_xml(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> Response:
    """The FatturaPA a shadow run WOULD have sent, exactly as generated.

    This is the artefact a parallel run exists to produce: download it and diff
    it against what the incumbent provider filed for the same payment.
    """
    await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = (
        await ctx.session.execute(
            select(PaymentConnectorEvent).where(
                PaymentConnectorEvent.id == event_id,
                PaymentConnectorEvent.connector_id == connector_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.dry_run_xml is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_FOUND)
    return Response(
        content=row.dry_run_xml,
        media_type="application/xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/issuer-profiles/{issuer_profile_id}/payment-connectors/{connector_id}/events/{event_id}/retry",
    response_model=PaymentConnectorEventOut,
)
async def retry_event(
    issuer_profile_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> PaymentConnectorEventOut:
    """Re-arm a parked or dead event once the operator fixed what blocked it."""
    provider = await _assert_in_issuer(ctx, issuer_profile_id, connector_id)
    row = await svc.retry_event(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        connector_id=connector_id,
        event_id=event_id,
    )
    return _event_out(
        row,
        provider=provider,
        role=ctx.role,
        linked=await _draft_invoice_ids(ctx, [row]),
    )
