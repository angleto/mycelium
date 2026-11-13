"""The authorization-code grant for public clients, with a person in it.

Four calls, the order between them is the design, and it is the device
grant's order (``device_authorization.py``) for the device grant's reason:

``open_request``  ``/authorize`` records what the client asked for and
                  hands back an opaque request id for the consent page.
                  Nothing is granted, no code exists.
``approve`` /     a logged-in person answers on the consent page. Approval
``deny``          records who, in which workspace, under which label and
                  scope, and mints the authorization CODE. Not a credential.
``redeem``        ``/token``: the client presents the code, its PKCE
                  verifier, and the redirect URI it asked with. The claim
                  yields the approval, which the credential issuer
                  (``oauth.issuance``) turns into a token in the same
                  transaction, as the person who approved it.

This module is the consent half of the authorization flow and knows nothing
about how the client was identified: ``oauth.flow`` resolves the client
through the strategy registry and hands over a client id, a display name and
a kind.

**Single use, everywhere.** The request id is spent by the first answer; the
code is spent by the first exchange, including a failed one -- a code
presented with the wrong verifier or redirect is burned, because the party
presenting it is either confused or not the client, and neither should get a
second try at it. Both are stored as digests.

**What the consent decision binds.** The workspace comes from the person's
session, never from anything the client sent; the scope is what the person
ticked, checked against the same role rule as creating an assistant by hand
(``ai_assistants.check_grantable``) when they press Approve, and again by
``create_assistant`` at the exchange.

What this does NOT protect against, said because the limit is part of the
control:

- A person who approves a client they did not mean to. The consent page
  shows where the code will go (the redirect host) because that is the one
  thing a registrant cannot fake; it cannot make anybody read it.
- A compromised client. Whoever holds the client's redirect endpoint and its
  PKCE verifier holds the credential, which is what OAuth means by a public
  client. Revocation is per assistant, in the settings.
- What the token is, and whether it can be refreshed, is the issuer's.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import re
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core import security_events
from mycelium_core.errors import NotFoundError, QuotaExceededError
from mycelium_core.i18n import MessageCode
from mycelium_core.mcp_scopes import DEFAULT_SCOPES, SCOPE_CATALOG, VALID_SCOPE_KEYS
from mycelium_core.models.oauth_authorization_request import OAuthAuthorizationRequest
from mycelium_core.oauth.issuance import ApprovedConsent
from mycelium_core.services import ai_assistants

# Long enough to log in, pass a second factor and read the page; short
# enough that an abandoned request does not stay answerable all afternoon.
REQUEST_TTL_SECONDS = 600
# From approval to exchange. A client redeems within a second of the redirect;
# ten minutes is the conventional ceiling and leaves room for a slow host.
CODE_TTL_SECONDS = 600

# Open requests per network origin, refused past the limit; and a ceiling on
# the unanswered requests in the table, which EVICTS the oldest rather than
# refusing the newcomer. The same reasoning as the registration bound in
# ``oauth_clients``: an unanswered request grants nothing, so dropping the
# oldest costs at most a person who left a consent page open, while refusing
# would let one unattributable caller lock everybody out.
OPEN_LIMIT_PER_ORIGIN = 30
OPEN_WINDOW_SECONDS = 600
OPEN_GLOBAL_CEILING = 5000

# Bounds on what the client sends and we echo back or store.
MAX_STATE_LENGTH = 2048
MAX_SCOPE_PARAM_LENGTH = 2048
MAX_RESOURCE_LENGTH = 2048
MAX_LABEL_LENGTH = 255


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


# RFC 7636 §4.1: 43 to 128 characters of the unreserved set. A shorter or
# looser verifier is a weaker secret than the specification promises the
# client it is using, so it is refused rather than hashed.
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
# An S256 challenge is the unpadded base64url of a SHA-256: exactly 43 chars.
_S256_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def valid_s256_challenge(code_challenge: str) -> bool:
    return bool(_S256_CHALLENGE.match(code_challenge))


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """RFC 7636 S256, the only method this server accepts (plain would let a
    code interceptor redeem it). One implementation for both flows."""
    if not _VERIFIER.match(code_verifier):
        return False
    try:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    except UnicodeEncodeError:
        return False
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def with_query(uri: str, params: dict[str, str]) -> str:
    """``uri`` with ``params`` appended, keeping a query it already has
    (RFC 6749 §3.1.2 requires that)."""
    sep = "&" if "?" in uri else "?"
    return f"{uri}{sep}{urlencode(params)}"


_DANGEROUS_KEYS = frozenset(s.key for s in SCOPE_CATALOG if s.category == "danger")


def suggested_scope(requested: str | None) -> list[str]:
    """What the consent page preselects. The client's ``scope`` parameter
    when every word of it is a catalogue key, else the read-only default: a
    client asking for scopes we do not have (``openid``, ``mcp``, ...) is
    common and harmless, and what it asked for cannot be honoured anyway.

    Never a dangerous key. The official MCP SDK asks for every key in
    ``scopes_supported``, so "what the client asked for" is routinely
    "everything", and preselecting it ticked deletions without the
    confirmation the picker asks for when a person ticks one by hand. A
    request may preselect read and write keys; a dangerous one is only ever
    ticked by the person. The person decides in every case."""
    if requested:
        keys = [k for k in requested.split() if k]
        if keys and all(k in VALID_SCOPE_KEYS for k in keys):
            safe = [k for k in dict.fromkeys(keys) if k not in _DANGEROUS_KEYS]
            if safe:
                return safe
    return list(DEFAULT_SCOPES)


async def check_open_rate(session: AsyncSession, *, origin_ip: str | None) -> None:
    """Refuse an origin (an ``oauth.origin`` bucket) piling up requests nobody
    answers. Cheap, and called BEFORE the client is resolved, so a caller
    over its budget does not get a metadata document fetched either. An
    unattributable origin is held only by the ceiling (``make_room``)."""
    if origin_ip is None:
        return
    since = _now() - datetime.timedelta(seconds=OPEN_WINDOW_SECONDS)
    from_origin = (
        await session.execute(
            select(func.count())
            .select_from(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.approved_at.is_(None),
                OAuthAuthorizationRequest.denied_at.is_(None),
                OAuthAuthorizationRequest.expires_at > _now(),
                OAuthAuthorizationRequest.origin_ip == origin_ip,
                OAuthAuthorizationRequest.created_at >= since,
            )
        )
    ).scalar_one()
    if from_origin >= OPEN_LIMIT_PER_ORIGIN:
        security_events.emit(
            "oauth.authorize_rate_limited", origin_ip=origin_ip, limit=OPEN_LIMIT_PER_ORIGIN
        )
        raise QuotaExceededError(MessageCode.OAUTH_TOO_MANY_PENDING)


async def make_room(session: AsyncSession) -> None:
    """Keep the unanswered requests under the ceiling by deleting the
    oldest, expired ones first by construction (they are the oldest)."""
    unanswered = (
        OAuthAuthorizationRequest.approved_at.is_(None),
        OAuthAuthorizationRequest.denied_at.is_(None),
    )
    total = (
        await session.execute(
            select(func.count()).select_from(OAuthAuthorizationRequest).where(*unanswered)
        )
    ).scalar_one()
    excess = total - OPEN_GLOBAL_CEILING + 1
    if excess <= 0:
        return
    candidates = (
        select(OAuthAuthorizationRequest.id)
        .where(*unanswered)
        .order_by(OAuthAuthorizationRequest.created_at, OAuthAuthorizationRequest.id)
        .limit(excess)
    )
    # The predicate again IN the DELETE: a request approved between choosing
    # it and deleting it is an answered request and stays. Only requests
    # nobody has answered are ever evicted; an approved one is a person's
    # decision in flight (second security review, 2026-11-13).
    doomed = (
        (
            await session.execute(
                sa_delete(OAuthAuthorizationRequest)
                .where(OAuthAuthorizationRequest.id.in_(candidates), *unanswered)
                .returning(OAuthAuthorizationRequest.id)
            )
        )
        .scalars()
        .all()
    )
    security_events.emit(
        "oauth.authorize_ceiling_evicted", evicted=len(doomed), limit=OPEN_GLOBAL_CEILING
    )


async def open_request(
    session: AsyncSession,
    *,
    client_id: str,
    client_name: str,
    client_registration: str,
    redirect_uri: str,
    code_challenge: str,
    state: str | None,
    resource: str | None,
    requested_scope: str | None,
    origin_ip: str | None,
) -> str:
    """Record the request and return the opaque id the consent page is
    opened with. The caller (``oauth.flow``) has resolved the client and
    validated every parameter against it; this stores them, with the
    client's name and kind as resolved NOW, so neither the consent page nor
    the exchange has to resolve the client again."""
    raw = secrets.token_urlsafe(32)
    session.add(
        OAuthAuthorizationRequest(
            request_id_hash=_hash(raw),
            client_id=client_id,
            client_name=client_name[:100],
            client_registration=client_registration,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            resource=resource,
            requested_scope=requested_scope,
            expires_at=_now() + datetime.timedelta(seconds=REQUEST_TTL_SECONDS),
            origin_ip=origin_ip,
        )
    )
    await session.flush()
    return raw


@dataclass(frozen=True)
class PendingConsent:
    """What the consent page is shown. Read from the server's rows, never
    from the browser's URL, so the page cannot be told a different client
    or redirect than the one the code will go to."""

    request: OAuthAuthorizationRequest
    suggested_scope: list[str]


async def _find_open_row(session: AsyncSession, *, request_id: str) -> OAuthAuthorizationRequest:
    """The live request behind an id. Every shape of "not live" -- never
    existed, answered, expired -- is one answer, as in the device flow:
    the distinctions only help somebody sweeping the space."""
    row = (
        await session.execute(
            select(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.request_id_hash == _hash(request_id),
                OAuthAuthorizationRequest.approved_at.is_(None),
                OAuthAuthorizationRequest.denied_at.is_(None),
                OAuthAuthorizationRequest.redeemed_at.is_(None),
                OAuthAuthorizationRequest.expires_at > _now(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.OAUTH_REQUEST_INVALID)
    return row


ClientEnabled = Callable[[str], bool]


async def _find_answerable_row(
    session: AsyncSession, *, request_id: str, client_enabled: ClientEnabled
) -> OAuthAuthorizationRequest:
    """An open request whose client's strategy is STILL switched on. A
    strategy turned off after the request was opened must not be able to
    complete a consent through it; the request reads as absent."""
    row = await _find_open_row(session, request_id=request_id)
    if not client_enabled(row.client_id):
        raise NotFoundError(MessageCode.OAUTH_REQUEST_INVALID)
    return row


async def describe(
    session: AsyncSession, *, request_id: str, client_enabled: ClientEnabled
) -> PendingConsent:
    """For the consent page. Everything about the client comes from the
    snapshot taken at /authorize, so this does no resolution and no network
    I/O whatever kind of client asked."""
    row = await _find_answerable_row(session, request_id=request_id, client_enabled=client_enabled)
    return PendingConsent(request=row, suggested_scope=suggested_scope(row.requested_scope))


async def approve(
    session: AsyncSession,
    *,
    request_id: str,
    approver_id: uuid.UUID,
    org_id: uuid.UUID,
    label: str,
    scope: Sequence[str],
    client_enabled: ClientEnabled,
) -> str:
    """Answer yes, and return where the browser goes next:
    ``redirect_uri?code=…&state=…``.

    ``session`` is the person's tenant session, so the role check reads
    their membership in the workspace they are looking at. The claim is a
    conditional UPDATE: one request, one answer, however many tabs."""
    row = await _find_answerable_row(session, request_id=request_id, client_enabled=client_enabled)
    granted = await ai_assistants.check_grantable(
        session, org_id=org_id, actor_id=approver_id, scope=scope
    )
    code = secrets.token_urlsafe(32)
    now = _now()
    claimed = (
        await session.execute(
            update(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.id == row.id,
                OAuthAuthorizationRequest.approved_at.is_(None),
                OAuthAuthorizationRequest.denied_at.is_(None),
            )
            .values(
                approved_at=now,
                answered_by_id=approver_id,
                approved_org_id=org_id,
                label=label[:MAX_LABEL_LENGTH],
                granted_scope=granted,
                code_hash=_hash(code),
                code_expires_at=now + datetime.timedelta(seconds=CODE_TTL_SECONDS),
            )
            .returning(OAuthAuthorizationRequest.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise NotFoundError(MessageCode.OAUTH_REQUEST_INVALID)
    # A credential is about to exist on this person's authority: the audit
    # trail says which client, where its codes go, in which workspace, with
    # what scope -- as an event an operator alerts on, beside the durable
    # provenance row the exchange writes.
    security_events.emit(
        "oauth.consent_approved",
        request=str(row.id),
        client_id=row.client_id,
        client_registration=row.client_registration,
        redirect_uri=row.redirect_uri,
        approver_id=str(approver_id),
        org_id=str(org_id),
        scope=granted,
    )
    params = {"code": code}
    if row.state:
        params["state"] = row.state
    return with_query(row.redirect_uri, params)


async def deny(session: AsyncSession, *, request_id: str, actor_id: uuid.UUID) -> str:
    """Answer no, and return ``redirect_uri?error=access_denied&state=…``,
    so the client is told rather than left waiting for a redirect that
    never comes."""
    row = await _find_open_row(session, request_id=request_id)
    claimed = (
        await session.execute(
            update(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.id == row.id,
                OAuthAuthorizationRequest.approved_at.is_(None),
                OAuthAuthorizationRequest.denied_at.is_(None),
            )
            .values(denied_at=_now(), answered_by_id=actor_id)
            .returning(OAuthAuthorizationRequest.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise NotFoundError(MessageCode.OAUTH_REQUEST_INVALID)
    security_events.emit(
        "oauth.consent_denied",
        request=str(row.id),
        client_id=row.client_id,
        redirect_uri=row.redirect_uri,
        actor_id=str(actor_id),
    )
    params = {"error": "access_denied"}
    if row.state:
        params["state"] = row.state
    return with_query(row.redirect_uri, params)


@dataclass(frozen=True)
class GrantRefusal:
    """An exchange that produced nothing. ``error`` is the RFC 6749 §5.2
    code the token endpoint answers with; ``description`` is for the
    client's developer, never shown to a person."""

    error: str
    description: str


async def redeem(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
    accepted_resources: frozenset[str],
) -> ApprovedConsent | GrantRefusal:
    """Claim a code and check it against what is presented. Returns the
    approval it stands for, or a refusal -- returned rather than raised, and
    that is load-bearing: a refusal after the claim must COMMIT the claim, so
    the code is burned, and raising would roll it back.

    Issues nothing: the approval goes to the credential issuer
    (``oauth.issuance``), in the same transaction, so a failure there rolls
    the claim back and the client may retry within the code's lifetime."""
    if resource is not None and resource not in accepted_resources:
        return GrantRefusal("invalid_target", "resource is not this server's MCP endpoint")
    now = _now()
    row = (
        await session.execute(
            select(OAuthAuthorizationRequest).where(
                OAuthAuthorizationRequest.code_hash == _hash(code)
            )
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.approved_at is None
        or row.redeemed_at is not None
        or row.code_expires_at is None
        or row.code_expires_at <= now
    ):
        return GrantRefusal("invalid_grant", "code unknown, expired or already used")

    claimed = (
        await session.execute(
            update(OAuthAuthorizationRequest)
            .where(
                OAuthAuthorizationRequest.id == row.id,
                OAuthAuthorizationRequest.redeemed_at.is_(None),
            )
            .values(redeemed_at=now)
            .returning(OAuthAuthorizationRequest.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        return GrantRefusal("invalid_grant", "code unknown, expired or already used")

    # From here on the code is spent whatever the outcome.
    if not secrets.compare_digest(row.client_id.encode(), client_id.encode()):
        return GrantRefusal("invalid_grant", "code was not issued to this client")
    if not secrets.compare_digest(row.redirect_uri.encode(), redirect_uri.encode()):
        return GrantRefusal("invalid_grant", "redirect_uri does not match the authorization")
    if not verify_pkce(code_verifier, row.code_challenge):
        return GrantRefusal("invalid_grant", "PKCE verification failed")
    # RFC 8707: the token is for the resource the person authorized. Another
    # resource this server would also accept (the MCP endpoint as named at
    # another host) is still not the one in the authorization.
    if (
        resource is not None
        and row.resource is not None
        and resource.rstrip("/") != row.resource.rstrip("/")
    ):
        return GrantRefusal("invalid_target", "resource differs from the one authorized")
    if row.answered_by_id is None or row.approved_org_id is None or not row.label:
        # Approved with no approver is not a state ``approve`` produces.
        return GrantRefusal("invalid_grant", "code unknown, expired or already used")

    return ApprovedConsent(
        request_id=row.id,
        client_id=row.client_id,
        org_id=row.approved_org_id,
        approver_id=row.answered_by_id,
        label=row.label,
        scope=tuple(row.granted_scope or ()),
        client_name=row.client_name,
        client_registration=row.client_registration,
        redirect_uri=row.redirect_uri,
        approved_at=row.approved_at,
    )


async def sweep_expired(session: AsyncSession, *, older_than_days: int = 7) -> int:
    """Delete requests whose window is long past. Kept a few days after
    expiry rather than deleted at it, so an approval or a refusal can still
    be traced to the assistant it produced while anybody might ask."""
    cutoff = _now() - datetime.timedelta(days=older_than_days)
    doomed = (
        (
            await session.execute(
                select(OAuthAuthorizationRequest.id).where(
                    OAuthAuthorizationRequest.expires_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    if not doomed:
        return 0
    await session.execute(
        sa_delete(OAuthAuthorizationRequest).where(OAuthAuthorizationRequest.id.in_(doomed))
    )
    return len(doomed)


__all__ = [
    "CODE_TTL_SECONDS",
    "MAX_LABEL_LENGTH",
    "MAX_RESOURCE_LENGTH",
    "MAX_SCOPE_PARAM_LENGTH",
    "MAX_STATE_LENGTH",
    "OPEN_GLOBAL_CEILING",
    "OPEN_LIMIT_PER_ORIGIN",
    "OPEN_WINDOW_SECONDS",
    "REQUEST_TTL_SECONDS",
    "GrantRefusal",
    "PendingConsent",
    "approve",
    "check_open_rate",
    "deny",
    "describe",
    "make_room",
    "open_request",
    "redeem",
    "suggested_scope",
    "sweep_expired",
    "valid_s256_challenge",
    "verify_pkce",
    "with_query",
]
