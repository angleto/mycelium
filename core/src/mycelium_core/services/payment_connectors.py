"""Inbound payment connectors: configuration, ingress and the fiscal runner
(ADR-0051).

Three concerns live here, in this order:

1. CONFIGURATION -- owner-gated CRUD over ``payment_connectors``, including
   mint/rotate of the two credentials. The webhook signing secret is a Fernet
   envelope (we must recompute a MAC we did not generate); the optional ingress
   API key is a one-way peppered hash (we only ever compare it). Both rotate
   with a grace window, so a rotation never drops a redelivery signed with the
   old secret.

2. INGRESS -- ``resolve_for_ingress`` finds the tenant with no tenant context
   through the SECURITY DEFINER resolver, and ``ingest`` writes the event with
   ``INSERT ... ON CONFLICT DO NOTHING``. That is the whole HTTP path: verify,
   persist, answer. No fiscal work happens while the provider is waiting,
   because an SdI dispatch can take two minutes and every provider's webhook
   timeout is far shorter -- a synchronous emission would be reported as failed
   and redelivered while the first attempt was still filing.

3. THE RUNNER -- ``claim_due`` takes rows with ``FOR UPDATE SKIP LOCKED`` and
   COMMITS the claim before any work starts (unlike the outbound deliverer,
   which holds its transaction across the network call), then ``process`` does
   the fiscal work for one event in its own transaction. Claim-then-work in
   separate transactions is what makes the lease meaningful: a pod that dies
   mid-emission leaves a committed ``processing`` row whose lease expires, and
   the retry resumes rather than restarts, because the object links were
   committed before the dispatch.

The ordering rule that everything else depends on: the link claim in
``payment_object_links`` is written and COMMITTED before the invoice is
transmitted. A crash after that point finds the link on retry and resumes the
existing document's transmission; without it the retry would compose a second
draft and burn a second fiscal number for one payment.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Select, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.crypto import decrypt_secret, encrypt_secret
from mycelium_core.db import tenant_checkpoint
from mycelium_core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UnprocessableError,
)
from mycelium_core.i18n import MessageCode
from mycelium_core.models.client_profile import ClientProfile
from mycelium_core.models.invoice import DocumentType, Invoice, InvoiceState, PaymentStatus
from mycelium_core.models.membership import Role
from mycelium_core.models.payment_connector import (
    AUTOMATION_MODES,
    EMISSION_EVENTS,
    PROVIDERS,
    PROVIDERS_WITH_INGRESS_KEY,
    REFUND_EVENTS,
    VAT_PRICING,
    PaymentConnector,
    PaymentConnectorEvent,
    PaymentCustomerLink,
    PaymentObjectLink,
    PaymentWebhookDelivery,
)
from mycelium_core.models.tag import TagKind
from mycelium_core.services import audit, taxonomy
from mycelium_core.services import invoice as invoice_svc
from mycelium_core.services.payment_events import (
    CreditNoteIntent,
    CustomerProfileIntent,
    EmissionIntent,
    IgnoreIntent,
    Intent,
    LineIn,
    MapperConfig,
    PartyDigest,
    PartyIn,
    PayloadError,
    PaymentSyncIntent,
    ProviderEvent,
    SubscriptionContext,
    VerificationSecrets,
    get_mapper,
)
from mycelium_core.services.rbac import require_role
from mycelium_core.services.taxonomy import ClientInput

# --- credential material ---------------------------------------------------

RAW_KEY_PREFIX = "mycelium_pc_"
_RAW_KEY_ENTROPY_BYTES = 32
SIGNING_SECRET_PREFIX = "whsec_"  # noqa: S105 (matches the provider convention)
_SIGNING_SECRET_ENTROPY_BYTES = 32


#: Domain separator. The ingress key shares the issuer-key pepper (one deployment
#: secret, one rotation runbook), so the MESSAGE has to carry the domain: without
#: it, a string that hashes to a valid issuer-key digest would also hash to a
#: valid connector-key digest under the same pepper, and the two credential
#: namespaces would be silently interchangeable.
_KEY_DOMAIN = b"payment_connector.api_key.v1:"


def _hash_key(raw: str) -> bytes:
    """Keyed hash of an ingress API key.

    Same construction as ``issuer_api_keys._hash`` -- HMAC under a dedicated
    pepper, so a database leak alone does not yield usable credentials -- with a
    domain prefix so the two credential families cannot collide.
    """
    return hmac.new(
        get_settings().issuer_key_pepper.encode(),
        _KEY_DOMAIN + raw.encode(),
        hashlib.sha256,
    ).digest()


def generate_api_key() -> str:
    return f"{RAW_KEY_PREFIX}{secrets.token_urlsafe(_RAW_KEY_ENTROPY_BYTES)}"


def generate_signing_secret() -> str:
    return f"{SIGNING_SECRET_PREFIX}{secrets.token_urlsafe(_SIGNING_SECRET_ENTROPY_BYTES)}"


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


# --- runner control flow ---------------------------------------------------


class QuarantineError(Exception):
    """The event is valid but cannot become a valid document without a human.

    Terminal on purpose: retrying a payment whose customer has no VAT number
    will never succeed, and burning ten attempts to discover that only delays
    the operator seeing it. The event lands in ``needs_attention`` with a stable
    slug and is re-runnable from the UI once the data is fixed.
    """

    def __init__(self, slug: str, detail: str | None = None) -> None:
        self.slug = slug
        self.detail = detail
        super().__init__(f"{slug}: {detail}" if detail else slug)


class MissingBillingDataError(Exception):
    """The counterpart's billing block is incomplete. Not an operator problem.

    Separate from :class:`QuarantineError` because the two need opposite
    handling: this one resolves when the CUSTOMER gets round to entering their
    data, and re-arms itself the moment a provider customer event carries it, so
    it never belongs in the queue a human is asked to read.
    """

    def __init__(self, missing: str, customer_key: str | None = None) -> None:
        self.missing = missing
        self.customer_key = customer_key
        super().__init__(missing)


class RetryLaterError(Exception):
    """Not actionable YET, but plausibly will be.

    The canonical case is a refund whose invoice has not been emitted (or whose
    dispatch has not settled) because provider event ordering is not guaranteed.
    Goes back to ``pending`` with backoff, and only becomes ``needs_attention``
    once the attempts run out.
    """

    def __init__(self, slug: str, detail: str | None = None) -> None:
        self.slug = slug
        self.detail = detail
        super().__init__(f"{slug}: {detail}" if detail else slug)


@dataclass(frozen=True, slots=True)
class ResolvedConnector:
    """What the unauthenticated ingress path needs, and nothing more."""

    connector_id: uuid.UUID
    org_id: uuid.UUID
    issuer_profile_id: uuid.UUID
    provider: str
    enabled: bool
    #: NULL while a vendor connector is waiting for the secret its provider
    #: issues. Creating the connector has to come FIRST -- its id is what makes
    #: the webhook URL, and the provider only mints a secret once you have
    #: registered that URL -- so "exists but cannot verify yet" is a real state
    #: and not an anomaly.
    signing_secret: str | None
    previous_signing_secret: str | None
    api_key_hash: bytes | None
    previous_api_key_hash: bytes | None

    def secrets(self) -> VerificationSecrets | None:
        """The live secrets, or None when none is installed yet.

        None means the ingress CANNOT verify, which is a refusal and never a
        pass: the caller is turned away exactly like a bad signature. Returning
        an empty secret instead would put an attacker-guessable value on the
        one path that has no other authority.
        """
        if self.signing_secret is None:
            return None
        return VerificationSecrets(
            current=self.signing_secret, previous=self.previous_signing_secret
        )

    def api_key_matches(self, presented: str | None) -> bool:
        """Constant-time check of the OPTIONAL second ingress factor.

        A connector with no key configured accepts any request that already
        passed the signature check. When a key IS configured, both the current
        and the unexpired rotated hash are compared, and neither comparison
        short-circuits.
        """
        if self.api_key_hash is None and self.previous_api_key_hash is None:
            return True
        if not presented:
            return False
        digest = _hash_key(presented)
        ok = False
        for candidate in (self.api_key_hash, self.previous_api_key_hash):
            if candidate is not None and hmac.compare_digest(digest, candidate):
                ok = True
        return ok


def subscription_for(connector: PaymentConnector) -> tuple[ProviderEvent, ...]:
    """The events this connector's provider has to be configured to deliver.

    Read straight off the row, so the instructions an operator follows cannot
    disagree with the connector they are configuring: flip ``credit_note_mode``
    to ``off`` and the refund events drop out of the list on the next render.
    """
    mapper = get_mapper(connector.provider)
    return mapper.subscription(
        SubscriptionContext(
            emission_event=connector.emission_event,
            refund_event=connector.refund_event,
            emits=connector.invoice_mode != "off",
            credit_notes=connector.credit_note_mode != "off",
            payment_sync=connector.payment_sync_enabled,
        )
    )


def mapper_config(connector: PaymentConnector) -> MapperConfig:
    return MapperConfig(
        emission_event=connector.emission_event,
        refund_event=connector.refund_event,
        metadata_vat_keys=tuple(connector.metadata_vat_keys),
        metadata_tax_code_keys=tuple(connector.metadata_tax_code_keys),
        metadata_sdi_keys=tuple(connector.metadata_sdi_keys),
        metadata_pec_keys=tuple(connector.metadata_pec_keys),
        default_country_code=connector.default_country_code,
        default_line_description=connector.default_line_description,
        default_vat_rate=connector.default_vat_rate,
        default_vat_nature=connector.default_vat_nature,
        default_purpose=connector.default_purpose,
        vat_pricing=connector.vat_pricing,
    )


# --- configuration CRUD ----------------------------------------------------


def _validate_vocabulary(
    *,
    provider: str,
    invoice_mode: str,
    credit_note_mode: str,
    emission_event: str,
    refund_event: str,
    vat_pricing: str,
) -> None:
    if provider not in PROVIDERS:
        raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_PROVIDER_INVALID, detail=provider)
    for mode in (invoice_mode, credit_note_mode):
        if mode not in AUTOMATION_MODES:
            raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_MODE_INVALID, detail=mode)
    if emission_event not in EMISSION_EVENTS:
        raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_EVENT_INVALID, detail=emission_event)
    if refund_event not in REFUND_EVENTS:
        raise UnprocessableError(
            MessageCode.PAYMENT_CONNECTOR_REFUND_EVENT_INVALID, detail=refund_event
        )
    if vat_pricing not in VAT_PRICING:
        raise UnprocessableError(
            MessageCode.PAYMENT_CONNECTOR_VAT_PRICING_INVALID, detail=vat_pricing
        )


#: Fields a PATCH may touch. An explicit whitelist, like ``_PROFILE_FIELDS``:
#: a blind setattr loop would let a caller write ``org_id`` or a secret column.
PATCHABLE_FIELDS = frozenset(
    {
        "label",
        "enabled",
        "invoice_mode",
        "credit_note_mode",
        "emission_event",
        "refund_event",
        "payment_sync_enabled",
        "series",
        "default_purpose",
        "default_vat_rate",
        "default_vat_nature",
        "default_line_description",
        "default_payment_conditions_code",
        "default_payment_method_code",
        "default_country_code",
        "vat_pricing",
        "metadata_vat_keys",
        "metadata_tax_code_keys",
        "metadata_sdi_keys",
        "metadata_pec_keys",
    }
)


async def create_connector(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    issuer_profile_id: uuid.UUID,
    label: str,
    provider: str = "stripe",
    signing_secret: str | None = None,
    with_api_key: bool = False,
    **fields: Any,
) -> tuple[PaymentConnector, str | None, str | None]:
    """Create a connector. Returns ``(row, signing_secret, api_key)``.

    Both plaintexts are returned EXACTLY here and at rotation, and never stored
    in recoverable form for the key (nor echoed by any read route). For Stripe
    the signing secret is not ours to choose -- it is the ``whsec_...`` Stripe
    shows when the endpoint is created there -- so it is an input; when omitted
    (the native contract, where we are the authority) one is minted.
    """
    await require_role(session, org_id, actor_id, Role.owner)
    # Fail early on an issuer that does not exist / is not ours: a connector
    # pointing at nothing would accept webhooks and quarantine every one.
    await invoice_svc.get_issuer_profile(session, org_id=org_id, profile_id=issuer_profile_id)

    invoice_mode = str(fields.get("invoice_mode", "transmit"))
    credit_note_mode = str(fields.get("credit_note_mode", "transmit"))
    emission_event = str(fields.get("emission_event", "invoice.paid"))
    refund_event = str(fields.get("refund_event", "refund.created"))
    vat_pricing = str(fields.get("vat_pricing", "auto"))
    _validate_vocabulary(
        provider=provider,
        invoice_mode=invoice_mode,
        credit_note_mode=credit_note_mode,
        emission_event=emission_event,
        refund_event=refund_event,
        vat_pricing=vat_pricing,
    )

    # Minting is only meaningful where WE are the authority. For a vendor adapter
    # the secret belongs to the vendor, so an omitted one is NOT minted -- a
    # secret the provider never issued produces a connector that looks healthy
    # (enabled, no error, no events) while refusing every delivery, because the
    # MAC can never match.
    #
    # It is also not refused. The provider only issues a secret once the webhook
    # URL is registered as an endpoint, and that URL contains this connector's
    # id, so the connector has to exist FIRST. Demanding the secret here closed a
    # circle with no entry point. The connector is instead born without one, and
    # cannot be ENABLED until a real secret is installed -- the gate sits on the
    # state that actually receives money events rather than on the paperwork.
    raw_secret = (
        signing_secret
        if signing_secret is not None
        else (generate_signing_secret() if provider == "mycelium" else None)
    )
    if with_api_key:
        _assert_ingress_key_supported(provider)
    raw_key = generate_api_key() if with_api_key else None
    row = PaymentConnector(
        org_id=org_id,
        issuer_profile_id=issuer_profile_id,
        created_by=actor_id,
        provider=provider,
        label=label,
        signing_secret_ciphertext=encrypt_secret(raw_secret) if raw_secret else None,
        api_key_hash=_hash_key(raw_key) if raw_key else None,
    )
    for key, value in fields.items():
        if key in PATCHABLE_FIELDS:
            setattr(row, key, value)
    session.add(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="create",
        diff={"provider": provider, "label": label, "issuer_profile_id": str(issuer_profile_id)},
    )
    return row, raw_secret, raw_key


async def list_connectors(
    session: AsyncSession, *, org_id: uuid.UUID, issuer_profile_id: uuid.UUID | None = None
) -> list[PaymentConnector]:
    stmt: Select[tuple[PaymentConnector]] = select(PaymentConnector).where(
        PaymentConnector.org_id == org_id
    )
    if issuer_profile_id is not None:
        stmt = stmt.where(PaymentConnector.issuer_profile_id == issuer_profile_id)
    stmt = stmt.order_by(PaymentConnector.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def get_connector(
    session: AsyncSession, *, org_id: uuid.UUID, connector_id: uuid.UUID
) -> PaymentConnector:
    row = (
        await session.execute(
            select(PaymentConnector).where(
                PaymentConnector.id == connector_id, PaymentConnector.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_NOT_FOUND)
    return row


async def update_connector(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
    values: Mapping[str, Any],
) -> PaymentConnector:
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    unknown = set(values) - PATCHABLE_FIELDS
    if unknown:
        raise DomainError(MessageCode.DOMAIN_ERROR, detail=f"unknown fields: {sorted(unknown)}")
    merged = {
        "provider": row.provider,
        "invoice_mode": values.get("invoice_mode", row.invoice_mode),
        "credit_note_mode": values.get("credit_note_mode", row.credit_note_mode),
        "emission_event": values.get("emission_event", row.emission_event),
        "refund_event": values.get("refund_event", row.refund_event),
        "vat_pricing": values.get("vat_pricing", row.vat_pricing),
    }
    _validate_vocabulary(
        provider=str(merged["provider"]),
        invoice_mode=str(merged["invoice_mode"]),
        credit_note_mode=str(merged["credit_note_mode"]),
        emission_event=str(merged["emission_event"]),
        refund_event=str(merged["refund_event"]),
        vat_pricing=str(merged["vat_pricing"]),
    )
    if values.get("enabled") and row.signing_secret_ciphertext is None:
        # THE gate that replaced "you must supply a secret to create it".
        # Enabling is the moment a connector starts accepting money events, and
        # one with no secret cannot verify a single delivery: it would refuse
        # everything while presenting as live, which is the failure mode the
        # old creation-time refusal existed to prevent. Here it costs nothing,
        # because by now the operator HAS the URL and can fetch the secret.
        raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_SECRET_MISSING, detail=row.provider)
    if not values:
        # A write that writes nothing is not a write: bumping the concurrency
        # counter would fail a concurrent editor's version check for no reason,
        # and an empty diff is noise in the audit trail.
        return row
    for key, value in values.items():
        setattr(row, key, value)
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return row


async def rotate_signing_secret(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
    signing_secret: str | None = None,
) -> tuple[PaymentConnector, str]:
    """Install a new signing secret, keeping the old one live for the grace
    window so events already in the provider's retry queue still verify."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    if signing_secret is None and row.provider != "mycelium":
        # The same rule ``create_connector`` enforces, and rotation is the more
        # likely way to trip it: an operator rolling a Stripe endpoint secret who
        # submits an empty value would otherwise install a secret Stripe has
        # never heard of. The connector would stay enabled, report no error, and
        # refuse every delivery as signature_invalid -- with the previous secret
        # already demoted to the grace copy, so it recovers on its own only until
        # that expires.
        raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_SECRET_REQUIRED, detail=row.provider)
    raw = signing_secret or generate_signing_secret()
    grace = get_settings().payment_connector_secret_grace_hours
    row.previous_signing_secret_ciphertext = row.signing_secret_ciphertext
    row.previous_signing_secret_expires_at = _now() + datetime.timedelta(hours=grace)
    row.signing_secret_ciphertext = encrypt_secret(raw)
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="rotate_signing_secret",
    )
    return row, raw


def _assert_ingress_key_supported(provider: str) -> None:
    """Refuse the second factor where the sender cannot present it.

    Arming a key a provider has no way to send does not harden the endpoint, it
    silences it: the key is configured, no delivery carries it, and every event
    is refused. The refusal is collapsed with a bad signature on purpose, so the
    symptom an operator sees is "nothing arrives" with no cause attached.
    """
    if provider not in PROVIDERS_WITH_INGRESS_KEY:
        raise UnprocessableError(MessageCode.PAYMENT_CONNECTOR_KEY_UNSUPPORTED, detail=provider)


async def rotate_api_key(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> tuple[PaymentConnector, str]:
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    _assert_ingress_key_supported(row.provider)
    raw = generate_api_key()
    grace = get_settings().payment_connector_secret_grace_hours
    if row.api_key_hash is not None:
        row.previous_api_key_hash = row.api_key_hash
        row.previous_api_key_expires_at = _now() + datetime.timedelta(hours=grace)
    row.api_key_hash = _hash_key(raw)
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="rotate_api_key",
    )
    return row, raw


async def clear_api_key(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, connector_id: uuid.UUID
) -> PaymentConnector:
    """Drop the optional second factor. The signature remains mandatory."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    row.api_key_hash = None
    row.previous_api_key_hash = None
    row.previous_api_key_expires_at = None
    row.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="clear_api_key",
    )
    return row


async def revoke_connector(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, connector_id: uuid.UUID
) -> PaymentConnector:
    """Stop accepting events. The resolver returns nothing for a revoked row, so
    the ingress 404s without distinguishing revoked from never-existed."""
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.enabled = False
        row.version += 1
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="payment_connector",
            entity_id=row.id,
            action="revoke",
        )
    return row


async def purge_connector(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, connector_id: uuid.UUID
) -> None:
    """Hard-delete a revoked connector and its event history.

    Two-stage like every other credential in the repo: only a revoked row can be
    purged. Note the object links CASCADE with it, so a purge deliberately
    forgets which documents came from this connector -- the invoices themselves
    are untouched (their FK is RESTRICT, but the links go, not the invoices).
    """
    await require_role(session, org_id, actor_id, Role.owner)
    row = await get_connector(session, org_id=org_id, connector_id=connector_id)
    if row.revoked_at is None:
        raise ConflictError(MessageCode.PAYMENT_CONNECTOR_NOT_REVOKED)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=row.id,
        action="purge",
    )
    await session.delete(row)
    await session.flush()


# --- ingress ---------------------------------------------------------------

_RESOLVE_SQL = text(
    "SELECT out_org_id, out_issuer_profile_id, out_provider, out_enabled, "
    "out_signing_secret_ciphertext, out_previous_signing_secret_ciphertext, "
    "out_api_key_hash, out_previous_api_key_hash "
    "FROM public.resolve_payment_connector(CAST(:cid AS uuid))"
)


async def resolve_for_ingress(
    session: AsyncSession, *, connector_id: uuid.UUID
) -> ResolvedConnector | None:
    """Find a connector with NO tenant context, via the SECURITY DEFINER
    resolver. Returns None for unknown and for revoked alike."""
    row = (await session.execute(_RESOLVE_SQL, {"cid": str(connector_id)})).mappings().first()
    if row is None or row["out_org_id"] is None:
        return None
    previous_ct = row["out_previous_signing_secret_ciphertext"]
    return ResolvedConnector(
        connector_id=connector_id,
        org_id=row["out_org_id"],
        issuer_profile_id=row["out_issuer_profile_id"],
        provider=row["out_provider"],
        enabled=bool(row["out_enabled"]),
        signing_secret=(
            decrypt_secret(row["out_signing_secret_ciphertext"])
            if row["out_signing_secret_ciphertext"]
            else None
        ),
        previous_signing_secret=decrypt_secret(previous_ct) if previous_ct else None,
        api_key_hash=row["out_api_key_hash"],
        previous_api_key_hash=row["out_previous_api_key_hash"],
    )


async def ingest(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
    provider_event_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    occurred_at: datetime.datetime | None,
) -> bool:
    """Persist one verified event. True when it was new, False on a redelivery.

    ``ON CONFLICT DO NOTHING`` on ``(connector_id, provider_event_id)`` is the
    entire at-least-once story: a provider retry, a client-side retry and two
    replicas racing the same POST all land on one row, and the caller answers
    2xx either way (a redelivery is a success from the sender's point of view).
    """
    settings = get_settings()
    stmt = (
        pg_insert(PaymentConnectorEvent)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            connector_id=connector_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
            payload=dict(payload),
            occurred_at=occurred_at,
            status="pending",
            attempt_count=0,
            max_attempts=settings.payment_connector_max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["connector_id", "provider_event_id"])
        .returning(PaymentConnectorEvent.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    if inserted is not None:
        await session.execute(
            update(PaymentConnector)
            .where(PaymentConnector.id == connector_id)
            .values(last_event_at=_now())
        )
    return inserted is not None


def body_digest(raw: bytes) -> bytes:
    """SHA-256 of the exact bytes received.

    Stored instead of the body: for an accepted event the frozen payload is
    already on the event row, and for a REFUSED one the bytes are
    unauthenticated attacker-controlled data we would be persisting on their
    say-so. The digest still makes the row verifiable -- anyone holding the
    original body can prove it produced this row.
    """
    return hashlib.sha256(raw).digest()


_REFUSAL_BUDGET_SQL = text(
    """
    INSERT INTO payment_connector_refusals (connector_id, org_id, window_start, count)
    VALUES (:cid, :oid, now(), 1)
    ON CONFLICT (connector_id) DO UPDATE SET
      window_start = CASE
        WHEN payment_connector_refusals.window_start < now() - make_interval(secs => :w)
        THEN now() ELSE payment_connector_refusals.window_start END,
      count = CASE
        WHEN payment_connector_refusals.window_start < now() - make_interval(secs => :w)
        THEN 1 ELSE payment_connector_refusals.count + 1 END
    RETURNING count
    """
)


async def note_refusal(
    session: AsyncSession, *, org_id: uuid.UUID, connector_id: uuid.UUID
) -> bool:
    """Count one refused delivery. False once the window's budget is spent.

    A single atomic upsert, because this runs on the path whose whole purpose is
    to be cheaper than the work it is protecting: the caller uses the answer to
    decide whether to APPEND to the delivery ledger, and appending is the cost an
    unauthenticated flood would otherwise impose without bound.

    Counting refusals rather than requests is what keeps this invisible to real
    traffic: producing a valid signature requires the secret, so a signed burst
    is the provider and is never throttled however it spikes.
    """
    settings = get_settings()
    count = (
        await session.execute(
            _REFUSAL_BUDGET_SQL,
            {
                "cid": str(connector_id),
                "oid": str(org_id),
                "w": settings.payment_connector_refusal_window_seconds,
            },
        )
    ).scalar_one()
    return int(count) <= settings.payment_connector_refusal_budget


async def record_delivery(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
    provider: str,
    outcome: str,
    http_status: int,
    raw_body: bytes,
    signature_present: bool,
    api_key_present: bool,
    provider_event_id: str | None = None,
    event_id: uuid.UUID | None = None,
) -> PaymentWebhookDelivery:
    """Append one delivery attempt to the ledger, accepted or refused.

    Every inbound request against a RESOLVED connector leaves a row here, so
    "the provider says it sent it" is answerable from the database rather than
    from whichever pod's log survived. Requests for a connector id that does not
    resolve are deliberately NOT recorded: there is no tenant to attribute them
    to, and writing them anywhere an attacker can choose would make this table
    the injection point it exists to audit.
    """
    row = PaymentWebhookDelivery(
        org_id=org_id,
        connector_id=connector_id,
        provider=provider,
        outcome=outcome,
        http_status=http_status,
        event_id=event_id,
        provider_event_id=provider_event_id[:255] if provider_event_id else None,
        body_bytes=len(raw_body),
        body_sha256=body_digest(raw_body),
        signature_present=signature_present,
        api_key_present=api_key_present,
        received_at=_now(),
    )
    session.add(row)
    await session.flush()
    return row


async def list_deliveries(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
    outcome: str | None = None,
    refused_only: bool = False,
    limit: int = 50,
) -> list[PaymentWebhookDelivery]:
    stmt = select(PaymentWebhookDelivery).where(
        PaymentWebhookDelivery.org_id == org_id,
        PaymentWebhookDelivery.connector_id == connector_id,
    )
    if outcome is not None:
        stmt = stmt.where(PaymentWebhookDelivery.outcome == outcome)
    if refused_only:
        stmt = stmt.where(PaymentWebhookDelivery.outcome.not_in(("accepted", "duplicate")))
    stmt = stmt.order_by(PaymentWebhookDelivery.received_at.desc()).limit(min(max(limit, 1), 200))
    return list((await session.execute(stmt)).scalars().all())


# --- claiming --------------------------------------------------------------


def backoff_seconds(attempt: int) -> int:
    s = get_settings()
    delay = s.payment_connector_backoff_base_seconds * int(2 ** max(0, attempt - 1))
    return min(delay, s.payment_connector_backoff_cap_seconds)


async def reclaim_expired(session: AsyncSession, *, org_id: uuid.UUID) -> int:
    """Return events whose worker died to the pending pool.

    Safe only because the lease is longer than the whole SdI dispatch budget:
    an expired lease provably means no dispatch is still in flight for it. The
    retry then resumes the committed document rather than composing a new one.
    """
    s = get_settings()
    cut = _now() - datetime.timedelta(seconds=s.payment_connector_lease_seconds)
    result = await session.execute(
        text(
            "UPDATE payment_connector_events SET status = 'pending', "
            "last_error = 'lease_expired', updated_at = now() "
            "WHERE org_id = :org AND status = 'processing' AND last_attempt_at < :cut"
        ),
        {"org": str(org_id), "cut": cut},
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def claim_due(
    session: AsyncSession, *, org_id: uuid.UUID, batch: int | None = None
) -> list[uuid.UUID]:
    """Claim due events and mark them in flight. The CALLER must commit before
    doing any work: an uncommitted claim is not a lease, it is a lock that
    disappears on crash and lets a second worker start the same emission."""
    limit = batch if batch is not None else get_settings().payment_connector_batch
    rows = (
        (
            await session.execute(
                select(PaymentConnectorEvent.id)
                .where(
                    PaymentConnectorEvent.org_id == org_id,
                    PaymentConnectorEvent.status == "pending",
                    PaymentConnectorEvent.next_attempt_at <= _now(),
                )
                .order_by(PaymentConnectorEvent.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    ids = list(rows)
    if ids:
        await session.execute(
            update(PaymentConnectorEvent)
            .where(PaymentConnectorEvent.id.in_(ids))
            .values(status="processing", last_attempt_at=_now(), updated_at=_now())
        )
    return ids


# --- money helpers ---------------------------------------------------------

_CENT = Decimal("0.01")
_UNIT4 = Decimal("0.0001")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_UNIT4, rounding=ROUND_HALF_UP)


def net_unit_price(line: LineIn, *, fallback_rate: Decimal | None) -> Decimal:
    """The NET unit price to hand to ``invoice.add_line``.

    ``add_line`` treats ``unit_price`` as net and adds VAT on top, so a gross
    figure has to be divided out first. With no resolvable rate a gross figure
    is passed through unchanged: inventing a rate here would be worse than
    letting the issuer regime decide, and a 0%/forfettario issuer has no split
    to make anyway.
    """
    if not line.price_includes_vat:
        return _q4(line.unit_price)
    rate = line.vat_rate if line.vat_rate is not None else fallback_rate
    if rate is None or rate == 0:
        return _q4(line.unit_price)
    return _q4(line.unit_price / (Decimal(1) + rate / Decimal(100)))


def allocate_partial(
    lines: Sequence[tuple[uuid.UUID, Decimal]], target: Decimal
) -> dict[uuid.UUID, Decimal]:
    """Scale line totals down to ``target``, preserving the per-line split.

    ``lines`` is (line_id, current_total). Uses largest-remainder so the scaled
    amounts sum EXACTLY to the target instead of drifting by a cent per line.
    The residual VAT rounding is still per-rate-group and recomputed by the
    invoice service, so a multi-rate partial can land a cent off the refunded
    gross; a provider that sends explicit credit-note lines bypasses this path
    entirely and is exact.
    """
    total = sum((amount for _, amount in lines), Decimal(0))
    if total <= 0:
        return {}
    scaled: dict[uuid.UUID, Decimal] = {}
    floors: list[tuple[Decimal, uuid.UUID]] = []
    running = Decimal(0)
    for line_id, amount in lines:
        exact = amount * target / total
        low = exact.quantize(_CENT, rounding="ROUND_DOWN")
        scaled[line_id] = low
        running += low
        floors.append((exact - low, line_id))
    remainder = int(((target - running) / _CENT).to_integral_value(rounding=ROUND_HALF_UP))
    for _, line_id in sorted(floors, key=lambda pair: pair[0], reverse=True):
        if remainder <= 0:
            break
        scaled[line_id] += _CENT
        remainder -= 1
    return scaled


# --- fiscal runner ---------------------------------------------------------


async def _find_linked_invoice(
    session: AsyncSession,
    *,
    connector_id: uuid.UUID,
    keys: Sequence[tuple[str, str]],
    dry_run: bool = False,
) -> uuid.UUID | None:
    """Resolve a provider object to the document already emitted for it.

    ``dry_run`` selects the claim UNIVERSE, and the separation is the whole
    reason a shadow run is reversible: a live lookup must never see a shadow
    claim, or switching the mode off would resume a shadow draft and file it.
    """
    if not keys:
        return None
    clauses = [
        (PaymentObjectLink.object_kind == kind) & (PaymentObjectLink.object_id == ident)
        for kind, ident in keys
    ]
    condition = clauses[0]
    for extra in clauses[1:]:
        condition = condition | extra
    return (
        await session.execute(
            select(PaymentObjectLink.invoice_id)
            .where(
                PaymentObjectLink.connector_id == connector_id,
                PaymentObjectLink.dry_run.is_(dry_run),
                condition,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _claim_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
    keys: Sequence[tuple[str, str]],
    invoice_id: uuid.UUID,
    dry_run: bool = False,
) -> None:
    for kind, ident in keys:
        await session.execute(
            pg_insert(PaymentObjectLink)
            .values(
                id=uuid.uuid4(),
                org_id=org_id,
                connector_id=connector_id,
                object_kind=kind,
                object_id=ident,
                invoice_id=invoice_id,
                dry_run=dry_run,
            )
            .on_conflict_do_nothing(
                index_elements=["connector_id", "object_kind", "object_id", "dry_run"]
            )
        )


async def register_customer(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    customer_key: str,
    party: PartyIn,
) -> uuid.UUID | None:
    """Apply what a provider CUSTOMER event says to the org's real anagrafica.

    This is the piece that makes a Stripe integration work at all. A Stripe
    webhook payload cannot be expanded and an invoice event names its customer
    by id alone -- measured on a real account, 1212 of 1212 ``invoice.paid``
    events -- so the VAT number, codice destinatario and PEC stored on the
    customer NEVER travel with the invoice that needs them. They arrive here,
    on a different event, at a different time.

    They are written into ``client_profile``, not into a connector-owned copy:
    one record, editable by a human, already carrying RLS, versioning, audit and
    revisions. ``fill_client_gaps`` can only fill EMPTY fields, so an operator's
    correction always outlives whatever the provider says afterwards.

    A customer with no fiscal identity yet creates nothing: there is no client to
    register and ``resolve_or_create_client`` would refuse anyway. Its payments
    wait in ``no_billing_data`` until an event like this one carries the data.
    """
    party = apply_addressing_rules(party, connector=connector)
    linked = await _linked_tag(session, connector_id=connector.id, customer_key=customer_key)
    if linked is not None:
        await taxonomy.fill_client_gaps(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            tag_id=linked,
            profile=_client_input(party, connector=connector),
        )
        await rearm_waiting_events(
            session, connector_id=connector.id, customer_key=customer_key, party=party
        )
        return linked

    if not (party.vat_number or party.tax_code):
        return None  # nothing identifiable to register yet

    try:
        tag = await taxonomy.resolve_or_create_client(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            name=party.legal_name,
            profile=_client_input(party, connector=connector),
        )
    except DomainError:
        # A malformed fiscal id from a provider record is not worth failing the
        # event over: the payment path will report it precisely when it needs it.
        return None
    await _link_customer(
        session, connector=connector, customer_key=customer_key, client_tag_id=tag.id
    )
    # An existing client may predate this connector and still have holes.
    await taxonomy.fill_client_gaps(
        session,
        org_id=connector.org_id,
        actor_id=connector.id,
        tag_id=tag.id,
        profile=_client_input(party, connector=connector),
    )
    await rearm_waiting_events(
        session, connector_id=connector.id, customer_key=customer_key, party=party
    )
    return tag.id


async def rearm_waiting_events(
    session: AsyncSession,
    *,
    connector_id: uuid.UUID,
    customer_key: str,
    party: PartyIn,
) -> int:
    """Wake the payments that were waiting on THIS customer's billing data.

    The point of a separate ``no_billing_data`` state: the blocker is the
    customer, and the customer just moved. Requiring an operator to notice and
    press Retry would stop the automation exactly where it is most valuable -- a
    subscription charged monthly to someone who completed their details late
    would otherwise sit parked forever.

    Only fires when the profile actually carries a fiscal identity, so a customer
    event about something else (a new card, a renamed company) does not churn the
    queue. Re-armed events re-validate from scratch, so anything still incomplete
    simply parks again: a premature wake costs one attempt, never a wrong
    document.
    """
    if not (party.vat_number or party.tax_code):
        return 0
    return await _rearm_customer(session, connector_id=connector_id, customer_key=customer_key)


async def _rearm_customer(
    session: AsyncSession, *, connector_id: uuid.UUID, customer_key: str
) -> int:
    """Return every payment parked on this customer's missing data to the queue.

    Resets the attempt budget: the spent attempts measured a condition that no
    longer holds. A re-armed event re-validates from scratch, so anything still
    incomplete simply parks again -- a premature wake costs one attempt, never a
    wrong document.
    """
    result = await session.execute(
        update(PaymentConnectorEvent)
        .where(
            PaymentConnectorEvent.connector_id == connector_id,
            PaymentConnectorEvent.provider_customer_id == customer_key,
            PaymentConnectorEvent.status == "no_billing_data",
        )
        .values(
            status="pending",
            attempt_count=0,
            next_attempt_at=_now(),
            last_error=None,
            error_detail=None,
            updated_at=_now(),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _linked_tag(
    session: AsyncSession, *, connector_id: uuid.UUID, customer_key: str | None
) -> uuid.UUID | None:
    if not customer_key:
        return None
    return (
        await session.execute(
            select(PaymentCustomerLink.client_tag_id).where(
                PaymentCustomerLink.connector_id == connector_id,
                PaymentCustomerLink.provider_customer_id == customer_key,
            )
        )
    ).scalar_one_or_none()


async def _link_customer(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    customer_key: str,
    client_tag_id: uuid.UUID,
) -> None:
    await session.execute(
        pg_insert(PaymentCustomerLink)
        .values(
            id=uuid.uuid4(),
            org_id=connector.org_id,
            connector_id=connector.id,
            provider_customer_id=customer_key,
            client_tag_id=client_tag_id,
        )
        .on_conflict_do_nothing(index_elements=["connector_id", "provider_customer_id"])
    )


#: FatturaPA's prescribed codice destinatario for a counterpart that is not
#: established in Italy. Not a default and not a fallback: a foreign recipient
#: has no Italian recipient code by construction, and the standard says the
#: field carries seven X. Anything else is a scarto.
FOREIGN_SDI_CODE = "XXXXXXX"


def apply_addressing_rules(party: PartyIn, *, connector: PaymentConnector) -> PartyIn:
    """Fill what the STANDARD determines, never what is merely missing.

    There is exactly one such rule, and it is not a default: a counterpart whose
    country is not IT is addressed with ``XXXXXXX``. Everything else about how a
    document is delivered has to come from the counterpart, because ``0000000``
    -- the "no channel, leave it in the fiscal drawer" code -- cannot be used to
    actually send. A connector-wide default for it would make an invoice look
    emittable while producing a document that goes nowhere, which is worse than
    parking the payment and saying so.
    """
    country = (party.country or party.country_code or connector.default_country_code or "").upper()
    sdi_code = party.sdi_code
    if not sdi_code and country and country != "IT":
        sdi_code = FOREIGN_SDI_CODE
    return replace(
        party,
        country_code=party.country_code or connector.default_country_code,
        country=party.country or connector.default_country_code,
        sdi_code=sdi_code,
    )


def _client_input(party: PartyIn, *, connector: PaymentConnector) -> ClientInput:
    """Map the neutral counterpart onto ``taxonomy.ClientInput``.

    Normalises what the dedup key is matched on: ``resolve_or_create_client``
    strips the tax code for the LOOKUP but persists it verbatim, so a padded or
    lower-case value from a provider metadata field would create a duplicate
    client on the next event. Normalising here means both sides agree.
    """
    tax_code = (party.tax_code or "").strip().upper() or None
    vat = (party.vat_number or "").strip().upper() or None
    return ClientInput(
        legal_name=party.legal_name,
        first_name=party.first_name,
        last_name=party.last_name,
        country_code=party.country_code or connector.default_country_code,
        vat_number=vat,
        tax_code=tax_code,
        address=party.address,
        civic_number=party.civic_number,
        postal_code=party.postal_code,
        city=party.city,
        province=(party.province or "").strip().upper() or None,
        country=party.country or party.country_code or connector.default_country_code,
        sdi_code=party.sdi_code,
        pec=party.pec,
    )


def _assert_invoiceable(party: PartyIn) -> None:
    """Refuse to compose what ``invoice._validate`` will reject at transmit.

    Checking here rather than at transmit is the whole point of the quarantine:
    the operator sees "this payment is missing a codice fiscale" against the
    event, instead of an orphan draft that fails days later with no context.
    The list mirrors ``_validate`` exactly.
    """
    missing: list[str] = []
    if not (party.vat_number or party.tax_code):
        missing.append("vat_number|tax_code")
    # A document is only emittable when it can actually be DELIVERED: a real
    # codice destinatario, or a PEC, or -- for a counterpart outside Italy --
    # the prescribed XXXXXXX that ``apply_addressing_rules`` has already set.
    # 0000000 is explicitly not enough: it cannot be used to send.
    if not (party.sdi_code or party.pec):
        missing.append("sdi_code|pec")
    if not party.address:
        missing.append("address")
    if not party.postal_code:
        missing.append("postal_code")
    if not party.city:
        missing.append("city")
    if missing:
        raise MissingBillingDataError(", ".join(missing))


async def _assert_client_invoiceable(
    session: AsyncSession, *, org_id: uuid.UUID, tag_id: uuid.UUID, customer_key: str | None
) -> None:
    """The same completeness gate, applied to the STORED client record.

    Once a client exists it is the record the document is built from, so it is
    the one that has to be complete. An invoice event that happens to repeat an
    address proves nothing about what ``invoice._validate`` will read at
    transmit time.
    """
    row = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == tag_id))
    ).scalar_one_or_none()
    if row is None:
        raise MissingBillingDataError("client profile missing", customer_key)
    missing: list[str] = []
    if not (row.vat_number or row.tax_code):
        missing.append("vat_number|tax_code")
    if not (row.sdi_code or row.pec):
        missing.append("sdi_code|pec")
    if not row.address:
        missing.append("address")
    if not row.postal_code:
        missing.append("postal_code")
    if not row.city:
        missing.append("city")
    if missing:
        raise MissingBillingDataError(", ".join(missing), customer_key)


async def _resolve_client(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    intent: EmissionIntent,
) -> uuid.UUID:
    """Provider customer -> client tag, race-safe for this connector.

    Two redeliveries of one payment can be processed by two workers at once.
    ``resolve_or_create_client`` is a SELECT-then-INSERT with no unique
    constraint behind it, so both would miss and both would insert, leaving two
    client tags with the same VAT and two per-client sezionali. The transaction
    advisory lock serialises the lookup-create-link sequence for one
    (connector, customer); the link's UNIQUE constraint is what makes the
    outcome durable afterwards.

    NOTE the residual: this covers concurrency WITHIN this connector, which is
    the case at-least-once delivery creates. A client created concurrently by
    the SPA with the same VAT can still duplicate, because ``client_profile``
    carries no uniqueness on fiscal identity -- a pre-existing gap this
    subsystem does not widen.
    """
    customer_key = intent.customer_key
    if customer_key:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"pc:{connector.id}:{customer_key}"},
        )
        linked = await _linked_tag(session, connector_id=connector.id, customer_key=customer_key)
        if linked is not None:
            # The client record already holds whatever the customer events
            # taught us, and it is what the FatturaPA is built from. Check IT,
            # not the invoice event, which structurally cannot carry the fiscal
            # identity.
            await _assert_client_invoiceable(
                session, org_id=connector.org_id, tag_id=linked, customer_key=customer_key
            )
            return linked

    party = apply_addressing_rules(intent.party, connector=connector)
    try:
        _assert_invoiceable(party)
    except MissingBillingDataError as exc:
        # Attach the customer so a later customer event re-arms exactly the
        # payments that were waiting on this person's data.
        raise MissingBillingDataError(exc.missing, customer_key) from None
    try:
        tag = await taxonomy.resolve_or_create_client(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            name=party.legal_name,
            profile=_client_input(party, connector=connector),
        )
    except DomainError as exc:
        raise QuarantineError("client_rejected", str(exc)) from exc

    if customer_key:
        await _link_customer(
            session, connector=connector, customer_key=customer_key, client_tag_id=tag.id
        )
    return tag.id


def _retryable_dispatch(inv: Invoice) -> bool:
    """The ADR-0046 retryability predicate, verbatim."""
    return (
        inv.state is InvoiceState.transmitted
        and inv.identificativo_sdi is None
        and inv.sdi_dispatch_started_at is not None
    )


async def _settle(
    session: AsyncSession, *, connector: PaymentConnector, invoice_id: uuid.UUID, mode: str
) -> Invoice:
    """Bring an already-claimed document to its settled state.

    Called when a link already exists, which happens on a redelivery and after a
    crash between the link claim and the dispatch. A draft is transmitted, an
    unsettled dispatch is RESUMED (never re-composed), and anything settled is
    returned untouched -- calling transmit on a filed document would 409.

    ``mode`` is the switch that governs THIS document, and the caller passes it
    explicitly: invoices and credit notes carry independent settings, and a
    dashboard refund fires two events sharing one refund id, so the second pass
    through here is the normal course of events rather than an edge case.
    Reading ``invoice_mode`` unconditionally would file, irreversibly, a storno
    the operator configured to hold as a draft -- and burn a fiscal number on it.
    """
    inv = await invoice_svc.get_invoice(session, org_id=connector.org_id, invoice_id=invoice_id)
    if mode != "transmit":
        return inv
    if inv.state is InvoiceState.draft or _retryable_dispatch(inv):
        return await invoice_svc.transmit(
            session, org_id=connector.org_id, actor_id=connector.id, invoice_id=inv.id
        )
    return inv


async def _mark_paid_if_needed(
    session: AsyncSession, *, connector: PaymentConnector, inv: Invoice
) -> None:
    if not connector.payment_sync_enabled:
        return
    if connector.invoice_mode == "dry_run":
        # Nothing was emitted, so there is no payment state to mirror. Writing
        # one would put a real-looking mutation on a document that does not
        # exist as far as the tax authority is concerned.
        return
    if inv.payment_status is PaymentStatus.paid:
        return
    if inv.state is InvoiceState.draft:
        return
    await invoice_svc.mark_paid(
        session, org_id=connector.org_id, actor_id=connector.id, invoice_id=inv.id
    )


async def _process_emission(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    event: PaymentConnectorEvent,
    intent: EmissionIntent,
) -> str:
    if connector.invoice_mode == "off":
        raise QuarantineError("invoice_mode_manual")

    dry_run = connector.invoice_mode == "dry_run"
    event.dry_run = dry_run
    existing = await _find_linked_invoice(
        session, connector_id=connector.id, keys=intent.object_keys, dry_run=dry_run
    )
    if existing is not None:
        inv = await _settle(
            session, connector=connector, invoice_id=existing, mode=connector.invoice_mode
        )
        if intent.paid and not dry_run:
            await _mark_paid_if_needed(session, connector=connector, inv=inv)
        event.invoice_id = inv.id
        return "done"

    client_tag_id = await _resolve_client(session, connector=connector, intent=intent)
    inv = await invoice_svc.create_draft(
        session,
        org_id=connector.org_id,
        actor_id=connector.id,
        client_tag_id=client_tag_id,
        issuer_profile_id=connector.issuer_profile_id,
        series=connector.series,
        purpose=intent.purpose or connector.default_purpose,
        document_type=DocumentType.TD01,
    )
    draft_values: dict[str, object] = {}
    # The provider's currency is authoritative and Invoice defaults to EUR, so a
    # USD charge would otherwise be filed as EUR amounts on a EUR document.
    currency = (intent.currency or "EUR").upper()[:3]
    if currency and currency != inv.currency:
        draft_values["currency"] = currency
    if connector.default_payment_method_code:
        draft_values["payment_method_code"] = connector.default_payment_method_code
    if connector.default_payment_conditions_code:
        draft_values["payment_conditions_code"] = connector.default_payment_conditions_code
    if draft_values:
        await invoice_svc.update_draft(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            invoice_id=inv.id,
            values=draft_values,
        )
    for line in intent.lines:
        await invoice_svc.add_line(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            invoice_id=inv.id,
            description=line.description,
            unit_price=net_unit_price(line, fallback_rate=connector.default_vat_rate),
            quantity=line.quantity,
            vat_rate=line.vat_rate,
            vat_nature=line.vat_nature,
        )

    # THE ordering rule: claim every provider id for this document and COMMIT,
    # so a crash inside the dispatch resumes instead of composing a duplicate.
    await _claim_links(
        session,
        org_id=connector.org_id,
        connector_id=connector.id,
        keys=intent.object_keys,
        invoice_id=inv.id,
        dry_run=dry_run,
    )
    event.invoice_id = inv.id
    await session.flush()

    if dry_run:
        # Everything a real emission does, stopping one step before SdI: build
        # the document and validate it against the official XSD, so the reason
        # a shadow run fails is the same reason a real one would have.
        # ``get_xml_preview`` allocates nothing -- the XML carries a would-be
        # number and the ANTEPRIMA progressivo -- so shadowing can never
        # consume a sequence a real document will need.
        try:
            event.dry_run_xml = await invoice_svc.get_xml_preview(
                session, org_id=connector.org_id, invoice_id=inv.id
            )
        except DomainError as exc:
            # A shadow run exists to surface exactly this. Retrying it would be
            # pointless (the data will not change on its own) and would bury the
            # finding under attempts; park it with the validator's own words,
            # which name the field and the rule that rejected it.
            raise QuarantineError("dry_run_invalid_document", str(exc)[:512]) from exc
        # The document says WHY it was not sent. Without this it is an archived
        # draft like any other, and "held back because we are shadowing" reads
        # the same as "incomplete" or "rejected and being redone" -- on the one
        # surface where confusing the two means filing a duplicate or never
        # filing at all.
        inv.dry_run = True
        # Out of the active list: a shadow document must not sit where an
        # operator browses documents they may transmit. It stays fully
        # inspectable (XML, PDF, totals) in the archived view.
        await invoice_svc.archive_invoice(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            invoice_id=inv.id,
            archived=True,
        )
        return "done"

    await tenant_checkpoint(session)

    if connector.invoice_mode == "transmit":
        inv = await invoice_svc.transmit(
            session, org_id=connector.org_id, actor_id=connector.id, invoice_id=inv.id
        )
    if intent.paid:
        await _mark_paid_if_needed(session, connector=connector, inv=inv)
    return "done"


async def _apply_partial(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    note: Invoice,
    intent: CreditNoteIntent,
    parent: Invoice,
) -> None:
    """Reduce a full-copy TD04 to the amount actually refunded.

    ``create_credit_note`` always copies every parent line, because FatturaPA
    has no partial-copy concept and the service exposes no amount parameter.
    Explicit provider lines replace the copy outright (exact, per-rate). A bare
    amount is allocated across the copied lines pro-rata, which preserves each
    line's aliquota -- the alternative, one flat "partial refund" line, would
    have to pick a single rate and would misstate the VAT on a mixed invoice.
    """
    lines = await invoice_svc.list_lines(session, org_id=connector.org_id, invoice_id=note.id)
    if intent.lines:
        for existing in lines:
            await invoice_svc.delete_line(
                session,
                org_id=connector.org_id,
                actor_id=connector.id,
                invoice_id=note.id,
                line_id=existing.id,
            )
        for line in intent.lines:
            await invoice_svc.add_line(
                session,
                org_id=connector.org_id,
                actor_id=connector.id,
                invoice_id=note.id,
                description=line.description,
                unit_price=net_unit_price(line, fallback_rate=connector.default_vat_rate),
                quantity=line.quantity,
                vat_rate=line.vat_rate,
                vat_nature=line.vat_nature,
            )
        return

    if intent.amount is None or parent.total <= 0 or intent.amount >= parent.total:
        return  # full reversal: the verbatim copy is already right

    ratio = intent.amount / parent.total
    current = [(ln.id, _q2(_q4(ln.quantity) * _q4(ln.unit_price))) for ln in lines]
    target_net = _q2(sum((amount for _, amount in current), Decimal(0)) * ratio)
    allocation = allocate_partial(current, target_net)
    by_id = {ln.id: ln for ln in lines}
    for line_id, amount in allocation.items():
        copied = by_id[line_id]
        if amount <= 0:
            await invoice_svc.delete_line(
                session,
                org_id=connector.org_id,
                actor_id=connector.id,
                invoice_id=note.id,
                line_id=line_id,
            )
            continue
        await invoice_svc.update_line(
            session,
            org_id=connector.org_id,
            actor_id=connector.id,
            invoice_id=note.id,
            line_id=line_id,
            description=copied.description,
            unit_price=amount,
            quantity=Decimal(1),
            vat_rate=copied.vat_rate,
            vat_nature=copied.vat_nature,
        )


async def _process_credit_note(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    event: PaymentConnectorEvent,
    intent: CreditNoteIntent,
) -> str:
    if connector.credit_note_mode == "off":
        raise QuarantineError("credit_note_manual")
    if connector.credit_note_mode == "dry_run":
        # A TD04 corrects an EMITTED document (ADR-0009): create_credit_note
        # refuses a parent that was never filed, and in shadow mode no parent
        # ever is. Rather than fake a parent -- which would validate nothing
        # real -- say so plainly. During a parallel run the incumbent is still
        # issuing the storni anyway.
        event.dry_run = True
        raise QuarantineError("dry_run_credit_note_unsupported")

    existing = await _find_linked_invoice(
        session, connector_id=connector.id, keys=intent.object_keys
    )
    if existing is not None:
        # Already reversed -- either a redelivery, or the sibling event (a
        # dashboard refund fires both credit_note.created and charge.refunded
        # and they share the refund id).
        note = await _settle(
            session, connector=connector, invoice_id=existing, mode=connector.credit_note_mode
        )
        event.invoice_id = note.id
        return "done"

    parent_id = await _find_linked_invoice(
        session, connector_id=connector.id, keys=intent.parent_keys
    )
    if parent_id is None:
        # Provider ordering is not guaranteed: the emission may not have been
        # processed yet. Retry rather than quarantine; the attempt budget turns
        # a genuinely orphan refund into a parked event on its own.
        raise RetryLaterError("parent_not_emitted")

    parent = await invoice_svc.get_invoice(session, org_id=connector.org_id, invoice_id=parent_id)
    if parent.state is InvoiceState.draft:
        raise QuarantineError("parent_not_transmitted")
    if _retryable_dispatch(parent):
        raise RetryLaterError("parent_dispatch_unsettled")
    if parent.state is InvoiceState.rejected:
        # A scartato invoice was never validly issued; its correction is a
        # resend, not a TD04. That is an operator decision.
        raise QuarantineError("parent_rejected")

    note = await invoice_svc.create_credit_note(
        session,
        org_id=connector.org_id,
        actor_id=connector.id,
        parent_invoice_id=parent_id,
        purpose=intent.reason or connector.default_purpose,
    )
    await _apply_partial(session, connector=connector, note=note, intent=intent, parent=parent)

    await _claim_links(
        session,
        org_id=connector.org_id,
        connector_id=connector.id,
        keys=intent.object_keys,
        invoice_id=note.id,
    )
    event.invoice_id = note.id
    await session.flush()
    await tenant_checkpoint(session)

    if connector.credit_note_mode == "transmit":
        await invoice_svc.transmit(
            session, org_id=connector.org_id, actor_id=connector.id, invoice_id=note.id
        )
    return "done"


async def _process_payment_sync(
    session: AsyncSession,
    *,
    connector: PaymentConnector,
    event: PaymentConnectorEvent,
    intent: PaymentSyncIntent,
) -> tuple[str, str | None]:
    if not connector.payment_sync_enabled:
        return "ignored", "payment_sync_off"
    invoice_id = await _find_linked_invoice(
        session, connector_id=connector.id, keys=intent.parent_keys
    )
    if invoice_id is None:
        # Money we did not invoice (a payment predating the connector, or one
        # whose emission trigger is a different event). Not an error -- but it
        # still needs a NAME. An ignored event with an empty reason is the one
        # thing an operator cannot act on: it reads as "something happened and
        # nobody will say what", when the answer is simply that no document of
        # ours matches this payment.
        return "ignored", "payment_without_invoice"
    inv = await invoice_svc.get_invoice(session, org_id=connector.org_id, invoice_id=invoice_id)
    await _mark_paid_if_needed(session, connector=connector, inv=inv)
    event.invoice_id = inv.id
    return "done", None


async def process_event(session: AsyncSession, *, org_id: uuid.UUID, event_id: uuid.UUID) -> str:
    """Run one claimed event to a terminal-or-retryable outcome.

    Returns the new status. Never raises for a per-event fault: the outcome is
    recorded ON the event, because one malformed payload must not stop the
    connector, exactly as one failing org must not stop a worker sweep.
    """
    event = (
        await session.execute(
            select(PaymentConnectorEvent).where(PaymentConnectorEvent.id == event_id)
        )
    ).scalar_one_or_none()
    if event is None:
        return "missing"
    connector = (
        await session.execute(
            select(PaymentConnector).where(PaymentConnector.id == event.connector_id)
        )
    ).scalar_one_or_none()
    if connector is None:
        return await _finish(session, event, status="ignored", slug="connector_gone")

    event.attempt_count += 1
    try:
        if not connector.enabled:
            return await _finish(session, event, status="ignored", slug="connector_disabled")
        mapper = get_mapper(connector.provider)
        intent: Intent = mapper.to_intent(event.payload, config=mapper_config(connector))
        if isinstance(intent, IgnoreIntent):
            return await _finish(session, event, status="ignored", slug=intent.reason)
        if isinstance(intent, CustomerProfileIntent):
            await register_customer(
                session,
                connector=connector,
                customer_key=intent.customer_key,
                party=intent.party,
            )
            return await _finish(session, event, status="done", slug=None)
        slug: str | None = None
        if isinstance(intent, EmissionIntent):
            status = await _process_emission(
                session, connector=connector, event=event, intent=intent
            )
        elif isinstance(intent, CreditNoteIntent):
            status = await _process_credit_note(
                session, connector=connector, event=event, intent=intent
            )
        else:
            # Reconciliation is the one branch that can legitimately end in
            # "nothing to do", so it is also the one that has to say why.
            status, slug = await _process_payment_sync(
                session, connector=connector, event=event, intent=intent
            )
        return await _finish(session, event, status=status, slug=slug)
    except PayloadError as exc:
        # Deterministic: a reprocess of the same frozen payload cannot succeed.
        return await _finish(
            session, event, status="needs_attention", slug="payload_invalid", detail=str(exc)
        )
    except MissingBillingDataError as exc:
        if exc.customer_key:
            event.provider_customer_id = exc.customer_key[:255]
        return await _finish(
            session,
            event,
            status="no_billing_data",
            slug="client_billing_data_missing",
            detail=exc.missing,
        )
    except QuarantineError as exc:
        return await _finish(
            session, event, status="needs_attention", slug=exc.slug, detail=exc.detail
        )
    except RetryLaterError as exc:
        return await _defer(session, event, slug=exc.slug, detail=exc.detail)
    except DomainError as exc:
        # A fiscal refusal (bad province, XSD violation, transmit conflict).
        # Retry: several of these are transient (an unsettled dispatch, a
        # mandate not yet active), and the attempt budget parks the rest.
        return await _defer(session, event, slug=exc.code.value, detail=str(exc)[:512])


async def _finish(
    session: AsyncSession,
    event: PaymentConnectorEvent,
    *,
    status: str,
    slug: str | None,
    detail: str | None = None,
) -> str:
    event.status = status
    event.processed_at = _now()
    event.last_error = slug[:160] if slug else None
    event.error_detail = detail[:512] if detail else None
    await session.flush()
    return status


async def _defer(
    session: AsyncSession, event: PaymentConnectorEvent, *, slug: str, detail: str | None
) -> str:
    """Back off, or park once the budget is spent."""
    if event.attempt_count >= event.max_attempts:
        return await _finish(session, event, status="dead", slug=slug, detail=detail)
    event.status = "pending"
    event.next_attempt_at = _now() + datetime.timedelta(
        seconds=backoff_seconds(event.attempt_count)
    )
    event.last_error = slug[:160]
    event.error_detail = detail[:512] if detail else None
    await session.flush()
    return "pending"


async def purge_expired(session: AsyncSession, *, org_id: uuid.UUID) -> tuple[int, int]:
    """Drop terminal event and delivery rows past the retention window.

    Returns ``(events, deliveries)``. Two rules make this safe to run
    unattended:

    - ``needs_attention`` and ``dead`` are NEVER swept. They are the operator's
      queue, and a payment that failed to become an invoice is exactly the row
      nobody may lose to a timer.
    - a delivery row whose event is swept keeps its own clock. The delivery
      ledger answers "did the provider deliver this", which stays a question
      long after the event that resulted from it stopped being interesting, and
      the FK is SET NULL precisely so the evidence outlives the event.

    The invoice itself is the durable fiscal record and is never touched here.
    """
    days = get_settings().payment_connector_event_retention_days
    cut = _now() - datetime.timedelta(days=days)
    events = await session.execute(
        text(
            "DELETE FROM payment_connector_events "
            "WHERE org_id = :org AND status IN ('done', 'ignored') AND created_at < :cut"
        ),
        {"org": str(org_id), "cut": cut},
    )
    deliveries = await session.execute(
        text("DELETE FROM payment_webhook_deliveries WHERE org_id = :org AND received_at < :cut"),
        {"org": str(org_id), "cut": cut},
    )
    return (
        int(getattr(events, "rowcount", 0) or 0),
        int(getattr(deliveries, "rowcount", 0) or 0),
    )


async def discard_dry_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> int:
    """Throw away a shadow run: its claims and the documents it composed.

    Needed because the two kinds of claim have opposite lifetimes. A LIVE claim
    must outlive everything -- it is what stops an old redelivery from filing a
    second document for one payment -- so its FK to the invoice is RESTRICT. A
    SHADOW claim exists only to dedup within the shadow period, and the operator
    who is done comparing wants the drafts out of the archive. That same
    RESTRICT then blocks deleting them, and surfaces as a raw
    ForeignKeyViolationError rather than anything a caller can act on.

    So the order matters and is the whole function: drop the shadow claims
    first, then the drafts they were pinning. Live claims are never touched, and
    a document that was actually transmitted is refused by ``delete_draft``
    itself, so this cannot reach a real invoice even if one were somehow linked.

    Returns the number of shadow documents discarded.
    """
    await require_role(session, org_id, actor_id, Role.member)
    rows = (
        (
            await session.execute(
                select(PaymentObjectLink.id, PaymentObjectLink.invoice_id).where(
                    PaymentObjectLink.connector_id == connector_id,
                    PaymentObjectLink.dry_run.is_(True),
                )
            )
        )
        .tuples()
        .all()
    )
    if not rows:
        return 0
    invoice_ids = {invoice_id for _link_id, invoice_id in rows}
    await session.execute(
        delete(PaymentObjectLink).where(PaymentObjectLink.id.in_([link_id for link_id, _ in rows]))
    )
    await session.flush()

    discarded = 0
    for invoice_id in invoice_ids:
        # Per-document savepoint: one draft an operator has meanwhile edited
        # into a non-deletable state must not abort the whole discard.
        try:
            async with session.begin_nested():
                await invoice_svc.delete_draft(
                    session, org_id=org_id, actor_id=actor_id, invoice_id=invoice_id
                )
            discarded += 1
        except DomainError:
            continue
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=connector_id,
        action="discard_dry_run",
        diff={"documents": discarded},
    )
    return discarded


async def promote_dry_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> Invoice:
    """Turn ONE shadow document into a real, sendable draft.

    The other exit from a parallel run. ``discard_dry_run`` throws the shadows
    away, which is right when an incumbent provider filed the real documents;
    this is for the payment that has to be invoiced by US after all -- the
    incumbent missed it, or the operator decides to cut over without waiting
    for the next event.

    Promotion is NOT "un-archive it and press send". The shadow's claim rows
    live in the shadow universe (``payment_object_links.dry_run``), where a live
    lookup cannot see them; leaving them there and transmitting the document
    would mean the next redelivery of that same payment finds no live claim and
    composes a SECOND document for money already invoiced. So the claims move
    with the document, in one transaction, and the unique index is what makes
    the move safe: if a live claim already exists for any of these objects,
    something real already covers this payment and the promotion is refused
    rather than filed.

    The document is left as a DRAFT, deliberately. Transmission stays the
    operator's existing, audited action -- which is also where the fiscal number
    is allocated, so a promoted document takes its number when it is really
    filed and not a moment earlier.
    """
    await require_role(session, org_id, actor_id, Role.member)
    inv = await invoice_svc.get_invoice(session, org_id=org_id, invoice_id=invoice_id)
    if not inv.dry_run:
        raise ConflictError(MessageCode.PAYMENT_CONNECTOR_NOT_DRY_RUN, detail=str(invoice_id))

    links = (
        (
            await session.execute(
                select(PaymentObjectLink).where(
                    PaymentObjectLink.connector_id == connector_id,
                    PaymentObjectLink.invoice_id == invoice_id,
                    PaymentObjectLink.dry_run.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    # Checked before writing, so the refusal is a DomainError the caller can
    # render rather than a unique-violation surfacing as a 500. The index still
    # backs it: this check and the constraint agree, and the constraint is the
    # one that holds under a concurrent promotion.
    for link in links:
        clash = (
            await session.execute(
                select(PaymentObjectLink.id).where(
                    PaymentObjectLink.connector_id == connector_id,
                    PaymentObjectLink.object_kind == link.object_kind,
                    PaymentObjectLink.object_id == link.object_id,
                    PaymentObjectLink.dry_run.is_(False),
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ConflictError(
                MessageCode.PAYMENT_CONNECTOR_ALREADY_EMITTED,
                detail=f"{link.object_kind}:{link.object_id}",
            )
    for link in links:
        link.dry_run = False

    inv.dry_run = False
    # Back into the active list: it is now a document waiting to be sent, and
    # the archive is where an operator does NOT look for those.
    await invoice_svc.archive_invoice(
        session, org_id=org_id, actor_id=actor_id, invoice_id=invoice_id, archived=False
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=connector_id,
        action="promote_dry_run",
        diff={"invoice_id": str(invoice_id), "claims_moved": len(links)},
    )
    return inv


# --- operator actions ------------------------------------------------------


async def list_events(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
) -> list[PaymentConnectorEvent]:
    stmt = select(PaymentConnectorEvent).where(
        PaymentConnectorEvent.org_id == org_id,
        PaymentConnectorEvent.connector_id == connector_id,
    )
    if status is not None:
        stmt = stmt.where(PaymentConnectorEvent.status == status)
    stmt = stmt.order_by(PaymentConnectorEvent.created_at.desc()).limit(min(max(limit, 1), 200))
    return list((await session.execute(stmt)).scalars().all())


def counterpart_of(event: PaymentConnectorEvent, *, provider: str) -> PartyDigest:
    """Name the counterpart of a stored event, for the operator's list.

    Derived from the FROZEN payload on read rather than denormalised into
    columns at ingest. Two reasons, and the second is the decisive one:

    * the payload is on the row already and is loaded with it, so this costs a
      few dict lookups per row and no query, no migration and no backfill;
    * a denormalised column would be empty for every event ALREADY parked --
      precisely the rows an operator is looking at when they ask whose payment
      this is. Reading the payload answers for the whole history on the first
      deploy.

    Total by construction: an unknown provider or a payload that names nobody
    is an empty digest. This is a triage column, and a projection that raised
    would take the whole list down with the one row it could not read.
    """
    try:
        mapper = get_mapper(provider)
    except PayloadError:  # pragma: no cover - guarded by the CHECK constraint
        return PartyDigest()
    payload = event.payload
    if not isinstance(payload, Mapping):  # pragma: no cover - JSONB always maps
        return PartyDigest()
    return mapper.describe_counterpart(payload)


async def assign_customer_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
    provider_customer_id: str,
    client_tag_id: uuid.UUID,
) -> int:
    """Point a provider customer at an existing client, by hand.

    The manual half of "the customer supplied their billing data late". The
    automatic half already works: a provider customer event carrying the data
    fills the client record and re-arms the payments waiting on it. But that
    only helps when the data arrives THROUGH the provider. When it arrives any
    other way -- the customer emails their VAT number, an accountant fills the
    anagrafica in -- there is nothing tying that mycelium client to the
    provider's customer id, so a retry re-derives the counterpart from the
    frozen event payload, finds it just as empty as the first time, and parks
    again. Fixing the anagrafica alone can never unblock those payments.

    This is the missing edge. It refuses a client that is not yet invoiceable
    rather than accepting the association and letting the retry fail for a
    second, less obvious reason: the caller is told which fields are still
    missing, on the record they are actually looking at.

    Returns the number of parked payments re-armed by the association.
    """
    await require_role(session, org_id, actor_id, Role.member)
    connector = await get_connector(session, org_id=org_id, connector_id=connector_id)
    tag = await taxonomy.get_tag(session, org_id=org_id, tag_id=client_tag_id)
    if tag.kind is not TagKind.client:
        raise UnprocessableError(MessageCode.TAG_KIND_MISMATCH, detail="client")
    # Fail here, not on the retry: the operator is holding the client record.
    #
    # ``MissingBillingDataError`` is INTERNAL control flow for the runner (it
    # decides which parked state an event lands in) and is not a DomainError,
    # so letting it escape a request would surface as an opaque 500 with none
    # of the field list an operator needs. Convert it at the boundary.
    try:
        await _assert_client_invoiceable(
            session, org_id=org_id, tag_id=client_tag_id, customer_key=provider_customer_id
        )
    except MissingBillingDataError as exc:
        raise UnprocessableError(
            MessageCode.PAYMENT_CONNECTOR_CLIENT_INCOMPLETE, detail=exc.missing
        ) from exc

    await session.execute(
        pg_insert(PaymentCustomerLink)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            connector_id=connector.id,
            provider_customer_id=provider_customer_id,
            client_tag_id=client_tag_id,
        )
        .on_conflict_do_update(
            index_elements=["connector_id", "provider_customer_id"],
            set_={"client_tag_id": client_tag_id, "updated_at": _now()},
        )
    )
    rearmed = await _rearm_customer(
        session, connector_id=connector.id, customer_key=provider_customer_id
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector",
        entity_id=connector.id,
        action="assign_customer_client",
        diff={
            "provider_customer_id": provider_customer_id,
            "client_tag_id": str(client_tag_id),
            "rearmed": rearmed,
        },
    )
    return rearmed


async def retry_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    connector_id: uuid.UUID,
    event_id: uuid.UUID,
) -> PaymentConnectorEvent:
    """Re-arm a parked or dead event after the operator fixed what blocked it.

    Resets the attempt budget: the previous attempts measured a condition that
    no longer holds. Only parked/dead rows qualify -- re-arming an in-flight one
    would race the worker that holds its lease.
    """
    await require_role(session, org_id, actor_id, Role.member)
    event = (
        await session.execute(
            select(PaymentConnectorEvent).where(
                PaymentConnectorEvent.id == event_id,
                PaymentConnectorEvent.org_id == org_id,
                PaymentConnectorEvent.connector_id == connector_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise NotFoundError(MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_FOUND)
    if event.status not in {"needs_attention", "no_billing_data", "dead"}:
        raise ConflictError(MessageCode.PAYMENT_CONNECTOR_EVENT_NOT_RETRYABLE)
    event.status = "pending"
    event.attempt_count = 0
    event.next_attempt_at = _now()
    event.last_error = None
    event.error_detail = None
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="payment_connector_event",
        entity_id=event.id,
        action="retry",
    )
    return event


__all__ = [
    "PATCHABLE_FIELDS",
    "RAW_KEY_PREFIX",
    "MissingBillingDataError",
    "QuarantineError",
    "ResolvedConnector",
    "RetryLaterError",
    "allocate_partial",
    "apply_addressing_rules",
    "assign_customer_client",
    "backoff_seconds",
    "body_digest",
    "claim_due",
    "clear_api_key",
    "counterpart_of",
    "create_connector",
    "discard_dry_run",
    "generate_api_key",
    "generate_signing_secret",
    "get_connector",
    "ingest",
    "list_connectors",
    "list_deliveries",
    "list_events",
    "mapper_config",
    "net_unit_price",
    "note_refusal",
    "process_event",
    "promote_dry_run",
    "purge_connector",
    "purge_expired",
    "rearm_waiting_events",
    "reclaim_expired",
    "record_delivery",
    "resolve_for_ingress",
    "retry_event",
    "revoke_connector",
    "rotate_api_key",
    "rotate_signing_secret",
    "update_connector",
]
