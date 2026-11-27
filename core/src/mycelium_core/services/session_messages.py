"""Send, read, claim and settle the messages a person sends to their agent
sessions (docs/adr/0079).

Who may do what is the point of this module, and each answer is decided by
what a message reaches: a session with local tools, possibly in a mode that
acts without asking.

- :func:`send` and :func:`mine` are a person's, and only for sessions they
  opened. Every other caller is refused, the CLI's token included: an agent
  can run the CLI as easily as a person, and an agent that could send could
  drive the person's sessions in every other project from what it read in
  one. Another person's session is answered as absent.
- :func:`claim` and :func:`record_outcome` are the relay's, made with the credential
  that opened the session, the same predicate as a session's reports.

Delivery is at most once: a claim moves the oldest queued message to
``claimed`` under the session's row lock, so two relays of one session
cannot both take it, and nothing is ever queued again on its own.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import ForbiddenError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_session import AgentSession
from mycelium_core.models.agent_session_message import (
    MAX_BODY_CHARS,
    AgentSessionMessage,
    MessageState,
)
from mycelium_core.models.membership import Role
from mycelium_core.models.user import User
from mycelium_core.services import agent_sessions as sessions_svc
from mycelium_core.services import audit
from mycelium_core.services import caller as caller_svc
from mycelium_core.services.rbac import require_role

#: A message claimed this long ago with no outcome is shown as uncertain:
#: its relay took it and has not said what happened, which a relay that
#: died between the write and the report looks like. A write into a local
#: socket takes milliseconds, so two minutes is never the normal case.
UNCERTAIN_AFTER = dt.timedelta(minutes=2)

#: How long a message outlives its session (docs/adr/0079 §7, Angelo's).
RETAIN_AFTER_CLOSE = dt.timedelta(days=30)

#: The messages the page shows per session.
PER_SESSION = 5

#: What a person reads. ``uncertain`` is the one status not stored.
Status = Literal["queued", "claimed", "uncertain", "delivered", "failed", "not_delivered"]


@dataclass(frozen=True, slots=True)
class MessageView:
    id: uuid.UUID
    session_id: uuid.UUID
    body: str
    status: Status
    status_since: dt.datetime
    created_at: dt.datetime
    failure: str | None


@dataclass(frozen=True, slots=True)
class Claimed:
    id: uuid.UUID
    #: What the relay writes into the session, whole: composed here so every
    #: relay, of every host, says where the text came from the same way.
    text: str


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def status_of(m: AgentSessionMessage, at: dt.datetime) -> Status:
    if m.state == MessageState.CLAIMED.value and at - m.state_since > UNCERTAIN_AFTER:
        return "uncertain"
    return cast(Status, m.state)


def _view(m: AgentSessionMessage, at: dt.datetime) -> MessageView:
    return MessageView(
        id=m.id,
        session_id=m.session_id,
        body=m.body,
        status=status_of(m, at),
        status_since=m.state_since,
        created_at=m.created_at,
        failure=m.failure,
    )


async def _require_person(session: AsyncSession, org_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    if not (await caller_svc.current(session)).is_person:
        raise ForbiddenError(MessageCode.AGENT_PERSON_ONLY)
    await require_role(session, org_id, actor_id, Role.member)


async def send(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: uuid.UUID,
    body: str,
) -> MessageView:
    """Queue a message for an open session the caller opened."""
    await _require_person(session, org_id, actor_id)
    text = body.strip()
    if not text or len(text) > MAX_BODY_CHARS:
        raise UnprocessableError(MessageCode.SESSION_MESSAGE_BODY, max=MAX_BODY_CHARS)
    # FOR SHARE, held to the commit: a reconcile or the idle sweep closes a
    # session with a plain UPDATE, which this waits out or makes wait. Read
    # without it, a close could commit under an open send, the message would
    # commit as queued on a closed session, and a resume would deliver it
    # (measured: the guard in test_session_messages failed that way). If the
    # close commits first, the row no longer matches and the send is absent.
    target = (
        await session.execute(
            select(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.org_id == org_id,
                AgentSession.opened_by_user_id == actor_id,
                AgentSession.closed_at.is_(None),
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(MessageCode.SESSION_NOT_FOUND)
    if not target.accepts_messages:
        raise UnprocessableError(MessageCode.SESSION_MESSAGES_NOT_ACCEPTED)
    at = _now()
    msg = AgentSessionMessage(
        org_id=org_id,
        session_id=session_id,
        author_user_id=actor_id,
        body=text,
        state=MessageState.QUEUED.value,
        state_since=at,
    )
    session.add(msg)
    await session.flush()
    # The text stays out of the log: it may hold anything the person typed.
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="agent_session_message",
        entity_id=msg.id,
        action="send",
        diff={"session_id": str(session_id)},
    )
    return _view(msg, at)


async def mine(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> list[MessageView]:
    """The caller's latest messages to each open session they opened,
    newest first, :data:`PER_SESSION` per session."""
    await _require_person(session, org_id, actor_id)
    at = now or _now()
    rank = (
        func.row_number()
        .over(
            partition_by=AgentSessionMessage.session_id,
            order_by=AgentSessionMessage.created_at.desc(),
        )
        .label("rank")
    )
    ranked = (
        select(AgentSessionMessage.id, rank)
        .join(AgentSession, AgentSession.id == AgentSessionMessage.session_id)
        .where(
            AgentSession.org_id == org_id,
            AgentSession.opened_by_user_id == actor_id,
            AgentSession.closed_at.is_(None),
            AgentSessionMessage.author_user_id == actor_id,
        )
        .subquery()
    )
    rows: Sequence[AgentSessionMessage] = (
        (
            await session.execute(
                select(AgentSessionMessage)
                .join(ranked, ranked.c.id == AgentSessionMessage.id)
                .where(ranked.c.rank <= PER_SESSION)
                .order_by(AgentSessionMessage.session_id, AgentSessionMessage.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_view(m, at) for m in rows]


def _compose(author: str, message_id: uuid.UUID, body: str) -> str:
    return (
        f"Message from {author}, sent from the Mycelium sessions page "
        f"(message {str(message_id)[:8]}):\n\n{body}"
    )


async def claim(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: uuid.UUID,
) -> Claimed | None:
    """Take the oldest queued message of a session this credential opened,
    or None. The session's row is locked for the claim, so two relays of one
    session take different messages or none."""
    await sessions_svc.own_open(session, org_id=org_id, actor_id=actor_id, session_id=session_id)
    msg = (
        await session.execute(
            select(AgentSessionMessage)
            .where(
                AgentSessionMessage.session_id == session_id,
                AgentSessionMessage.state == MessageState.QUEUED.value,
            )
            .order_by(AgentSessionMessage.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if msg is None:
        return None
    msg.state = MessageState.CLAIMED.value
    msg.state_since = _now()
    await session.flush()
    author = (
        await session.execute(
            select(func.coalesce(User.display_name, User.email)).where(
                User.id == msg.author_user_id
            )
        )
    ).scalar_one()
    return Claimed(id=msg.id, text=_compose(author, msg.id, msg.body))


async def record_outcome(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    message_id: uuid.UUID,
    delivered: bool,
    failure: str | None = None,
) -> MessageView:
    """Record what the relay did with a message it claimed. Only from
    ``claimed``, and only the first time: a repeat answers what is stored.

    The session need not be open: one that closed between the write and the
    report was still written to, and saying so is the truth."""
    token_id = (await caller_svc.current(session)).token_id
    if token_id is None:
        raise ForbiddenError(MessageCode.SESSION_NEEDS_CREDENTIAL)
    msg = (
        await session.execute(
            select(AgentSessionMessage)
            .join(AgentSession, AgentSession.id == AgentSessionMessage.session_id)
            .where(
                AgentSessionMessage.id == message_id,
                AgentSessionMessage.org_id == org_id,
                AgentSession.opened_by_user_id == actor_id,
                AgentSession.opened_by_token_id == token_id,
            )
            .with_for_update(of=AgentSessionMessage)
        )
    ).scalar_one_or_none()
    if msg is None:
        raise NotFoundError(MessageCode.SESSION_MESSAGE_NOT_FOUND)
    at = _now()
    if msg.state == MessageState.CLAIMED.value:
        msg.state = (MessageState.DELIVERED if delivered else MessageState.FAILED).value
        msg.failure = None if delivered else (failure or "").strip()[:300] or None
        msg.state_since = at
        await session.flush()
    return _view(msg, at)


async def purge_closed(session: AsyncSession, *, now: dt.datetime | None = None) -> int:
    """Delete the messages of sessions closed more than
    :data:`RETAIN_AFTER_CLOSE` ago. Run by the sweep, per workspace."""
    cutoff = (now or _now()) - RETAIN_AFTER_CLOSE
    old_sessions = select(AgentSession.id).where(AgentSession.closed_at < cutoff)
    deleted = await session.execute(
        delete(AgentSessionMessage)
        .where(AgentSessionMessage.session_id.in_(old_sessions))
        .returning(AgentSessionMessage.id)
        .execution_options(synchronize_session=False)
    )
    return len(deleted.all())
