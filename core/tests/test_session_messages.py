"""Messages a person sends to their agent sessions (docs/adr/0079), against
Postgres.

Each test pins a property the design was shaped against: a model, or the
CLI's token, sending into a session; another member reading or writing to
someone's session; two relays delivering one message; a resumed session
delivering, days later, what the page showed as not delivered; a second
outcome rewriting the first.

The CLI that runs a host's hooks and relay is an ``mcp_token`` subject, a
person on the web is ``human_api`` with no subject: that is what the server
sees of each.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import ForbiddenError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.agent_session import SessionAgent
from mycelium_core.models.agent_session_message import AgentSessionMessage, MessageState
from mycelium_core.models.agent_token import AgentToken
from mycelium_core.models.user import User
from mycelium_core.operations import OpCtx
from mycelium_core.operations import sessions as ops
from mycelium_core.services import agent_sessions as sessions_svc
from mycelium_core.services import agent_tokens
from mycelium_core.services import session_messages as svc
from mycelium_core.services.auth import signup
from mycelium_core.services.memberships import add_member

CC = SessionAgent.CLAUDE_CODE


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="MSG")
    return r.org_id, r.user_id


async def _member_of(org: uuid.UUID, owner: uuid.UUID) -> uuid.UUID:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="Other")
        email = (await s.execute(select(User.email).where(User.id == r.user_id))).scalar_one()
    async with tenant_session(str(org), str(owner)) as s:
        await add_member(s, org_id=org, actor_id=owner, email=email, role="member")
    return r.user_id


def _person(org: uuid.UUID, user: uuid.UUID):  # type: ignore[no-untyped-def]  # an async context manager factory, typed by tenant_session
    return tenant_session(str(org), str(user), actor_kind="human_api")


def _token(org: uuid.UUID, user: uuid.UUID, token: str):  # type: ignore[no-untyped-def]  # as above
    return tenant_session(str(org), str(user), actor_kind="mcp_token", actor_subject_id=token)


async def _session(
    org: uuid.UUID, user: uuid.UUID, token: str, *, ref: str | None = None, accepts: bool = True
) -> uuid.UUID:
    async with _token(org, user, token) as s:
        row, _ = await sessions_svc.open_session(
            s,
            org_id=org,
            actor_id=user,
            agent=CC,
            host_ref=ref or uuid.uuid4().hex,
            project="humus",
            machine="lab",
            accepts_messages=accepts,
        )
        return row.id


async def _send(org: uuid.UUID, user: uuid.UUID, sid: uuid.UUID, body: str = "run the tests"):
    async with _person(org, user) as s:
        return await svc.send(s, org_id=org, actor_id=user, session_id=sid, body=body)


async def _claim(org: uuid.UUID, user: uuid.UUID, token: str, sid: uuid.UUID):
    async with _token(org, user, token) as s:
        return await svc.claim(s, org_id=org, actor_id=user, session_id=sid)


async def _outcome(
    org: uuid.UUID, user: uuid.UUID, token: str, mid: uuid.UUID, *, delivered: bool
) -> svc.MessageView:
    async with _token(org, user, token) as s:
        return await svc.record_outcome(
            s, org_id=org, actor_id=user, message_id=mid, delivered=delivered, failure="EPIPE"
        )


async def _state(org: uuid.UUID, user: uuid.UUID, mid: uuid.UUID) -> str:
    async with tenant_session(str(org), str(user)) as s:
        return (
            await s.execute(select(AgentSessionMessage.state).where(AgentSessionMessage.id == mid))
        ).scalar_one()


# --- who may send -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "subject"),
    [
        ("human_api", "token"),  # an agent token over REST, the CLI's included
        ("mcp_token", "token"),  # an agent token over MCP
        ("agent_run", "run"),  # a dispatched run
        ("issuer_api_key", "key"),
        ("human_telegram", None),  # a person's words turned into calls by a model
        ("system", None),
        ("human_direct", None),
    ],
)
async def test_only_a_person_sends(kind: str, subject: str | None) -> None:
    org, user = await _org()
    sid = await _session(org, user, str(uuid.uuid4()))
    subject_id = str(uuid.uuid4()) if subject else None
    async with tenant_session(
        str(org), str(user), actor_kind=kind, actor_subject_id=subject_id
    ) as s:
        with pytest.raises(ForbiddenError) as e:
            await svc.send(s, org_id=org, actor_id=user, session_id=sid, body="push to main")
    assert e.value.code is MessageCode.AGENT_PERSON_ONLY


async def test_a_person_on_the_web_cannot_claim_or_settle() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    async with _person(org, user) as s:
        with pytest.raises(ForbiddenError) as e:
            await svc.claim(s, org_id=org, actor_id=user, session_id=sid)
    assert e.value.code is MessageCode.SESSION_NEEDS_CREDENTIAL
    async with _person(org, user) as s:
        with pytest.raises(ForbiddenError) as e:
            await svc.record_outcome(
                s, org_id=org, actor_id=user, message_id=sent.id, delivered=True
            )
    assert e.value.code is MessageCode.SESSION_NEEDS_CREDENTIAL


async def test_another_members_session_is_absent() -> None:
    org, owner = await _org()
    other = await _member_of(org, owner)
    sid = await _session(org, owner, str(uuid.uuid4()))
    with pytest.raises(NotFoundError):
        await _send(org, other, sid)


async def test_a_closed_session_is_absent_and_one_without_a_relay_says_so() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    closed = await _session(org, user, token)
    async with _token(org, user, token) as s:
        await sessions_svc.close_session(s, org_id=org, actor_id=user, session_id=closed)
    with pytest.raises(NotFoundError):
        await _send(org, user, closed)
    no_relay = await _session(org, user, token, accepts=False)
    with pytest.raises(UnprocessableError) as e:
        await _send(org, user, no_relay)
    assert e.value.code is MessageCode.SESSION_MESSAGES_NOT_ACCEPTED


async def test_a_blank_message_is_refused() -> None:
    org, user = await _org()
    sid = await _session(org, user, str(uuid.uuid4()))
    with pytest.raises(UnprocessableError):
        await _send(org, user, sid, body="  \n ")


# --- delivery ---------------------------------------------------------------


async def test_a_claim_names_the_sender_and_takes_the_oldest() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    first = await _send(org, user, sid, "first")
    await _send(org, user, sid, "second")
    claimed = await _claim(org, user, token, sid)
    assert claimed is not None and claimed.id == first.id
    assert claimed.text.startswith("Message from ")
    assert "Mycelium sessions page" in claimed.text and claimed.text.endswith("first")
    assert await _state(org, user, first.id) == MessageState.CLAIMED.value


async def test_only_the_opening_credential_claims() -> None:
    org, user = await _org()
    sid = await _session(org, user, str(uuid.uuid4()))
    await _send(org, user, sid)
    with pytest.raises(NotFoundError):
        await _claim(org, user, str(uuid.uuid4()), sid)


async def _still_waiting(task: asyncio.Future[object]) -> bool:
    """Whether a statement started in another transaction is still blocked
    on a lock this one holds, after giving it five seconds to finish.

    The time bounds a run that is not blocked, never one that is: on a
    loaded database an unblocked statement may take longer than a quarter
    of a second (measured: a guard with that bound passed against the
    defect while the full suite ran), and then it ran after the commit and
    hid the race. Waiting until it finishes, or five seconds, means an
    unblocked one has finished, inside the open transaction, by the time
    this returns."""
    await asyncio.wait({task}, timeout=5)
    return not task.done()


async def test_a_second_relay_waits_for_the_first_claim_and_finds_nothing() -> None:
    """The interleaving is forced: claim A holds its transaction open while
    claim B starts. Claims that merely ran concurrently caught a lock-free
    claim in one run out of four; this one was observed failing against it
    (B claimed the same message once A committed)."""
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    await _send(org, user, sid)
    async with _token(org, user, token) as s:
        first = await svc.claim(s, org_id=org, actor_id=user, session_id=sid)
        second = asyncio.ensure_future(_claim(org, user, token, sid))
        assert await _still_waiting(second)
    assert first is not None
    assert await second is None


async def test_a_message_sent_while_its_session_closes_is_not_delivered_later() -> None:
    """Observed failing against a send that read the session without a
    lock: the reconcile closed the session under the open send, the message
    committed as queued, and the resumed session's relay claimed it."""
    org, user = await _org()
    token = str(uuid.uuid4())
    ref = uuid.uuid4().hex
    sid = await _session(org, user, token, ref=ref)
    async with _token(org, user, token) as s:
        await s.execute(
            text(
                "UPDATE agent_sessions SET last_seen_at = now() - interval '1 hour' WHERE id = :i"
            ),
            {"i": sid},
        )

    async def reconcile() -> object:
        async with _token(org, user, token) as s:
            return await sessions_svc.reconcile(
                s, org_id=org, actor_id=user, agent=CC, machine="lab", alive=[]
            )

    async with _person(org, user) as s:
        sent = await svc.send(s, org_id=org, actor_id=user, session_id=sid, body="push")
        closing = asyncio.ensure_future(reconcile())
        assert await _still_waiting(closing)
    await closing
    assert await _state(org, user, sent.id) == MessageState.NOT_DELIVERED.value
    assert await _session(org, user, token, ref=ref) == sid
    assert await _claim(org, user, token, sid) is None


async def test_a_send_after_the_close_committed_is_absent() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    async with _token(org, user, token) as s:
        await sessions_svc.close_session(s, org_id=org, actor_id=user, session_id=sid)
    with pytest.raises(NotFoundError):
        await _send(org, user, sid)


async def test_the_first_outcome_counts_and_a_repeat_changes_nothing() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    await _claim(org, user, token, sid)
    first = await _outcome(org, user, token, sent.id, delivered=True)
    again = await _outcome(org, user, token, sent.id, delivered=False)
    assert first.status == again.status == "delivered"
    assert again.failure is None


async def test_an_outcome_for_a_message_never_claimed_changes_nothing() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    view = await _outcome(org, user, token, sent.id, delivered=True)
    assert view.status == "queued"


async def test_an_outcome_from_another_credential_is_absent() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    await _claim(org, user, token, sid)
    with pytest.raises(NotFoundError):
        await _outcome(org, user, str(uuid.uuid4()), sent.id, delivered=True)


async def test_a_claim_left_without_outcome_reads_as_uncertain() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    await _claim(org, user, token, sid)
    later = dt.datetime.now(dt.UTC) + svc.UNCERTAIN_AFTER + dt.timedelta(seconds=5)
    async with _person(org, user) as s:
        views = await svc.mine(s, org_id=org, actor_id=user, now=later)
    assert [v.status for v in views if v.id == sent.id] == ["uncertain"]


# --- a closed session never delivers later ----------------------------------


@pytest.mark.parametrize("how", ["ended", "gone", "idle"])
async def test_a_reopened_session_does_not_deliver_what_was_queued_before_it_closed(
    how: str,
) -> None:
    """Observed failing against a close that left queued messages queued
    (``_abandon_queued`` removed from the three close paths): the resumed
    session's relay claimed the message, days after the page had shown it
    as not delivered."""
    org, user = await _org()
    token = str(uuid.uuid4())
    ref = uuid.uuid4().hex
    sid = await _session(org, user, token, ref=ref)
    queued = await _send(org, user, sid)
    async with _token(org, user, token) as s:
        if how == "ended":
            await sessions_svc.close_session(s, org_id=org, actor_id=user, session_id=sid)
        elif how == "gone":
            await s.execute(
                text(
                    "UPDATE agent_sessions SET last_seen_at = now() - interval '1 hour' "
                    "WHERE id = :i"
                ),
                {"i": sid},
            )
            await sessions_svc.reconcile(
                s, org_id=org, actor_id=user, agent=CC, machine="lab", alive=[]
            )
        else:
            await sessions_svc.close_idle(
                s,
                idle_for=dt.timedelta(seconds=0),
                now=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
            )
    assert await _state(org, user, queued.id) == MessageState.NOT_DELIVERED.value
    assert await _session(org, user, token, ref=ref) == sid  # the resume finds the same row
    assert await _claim(org, user, token, sid) is None


async def test_a_claimed_message_keeps_its_outcome_across_a_close() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    sid = await _session(org, user, token)
    sent = await _send(org, user, sid)
    await _claim(org, user, token, sid)
    async with _token(org, user, token) as s:
        await sessions_svc.close_session(s, org_id=org, actor_id=user, session_id=sid)
    assert (await _outcome(org, user, token, sent.id, delivered=True)).status == "delivered"


# --- who reads --------------------------------------------------------------


async def test_messages_are_read_only_by_their_author_and_five_per_session() -> None:
    org, owner = await _org()
    other = await _member_of(org, owner)
    sid = await _session(org, owner, str(uuid.uuid4()))
    for i in range(7):
        await _send(org, owner, sid, f"m{i}")
    async with _person(org, owner) as s:
        mine = await svc.mine(s, org_id=org, actor_id=owner)
    assert [v.body for v in mine] == ["m6", "m5", "m4", "m3", "m2"]
    async with _person(org, other) as s:
        assert await svc.mine(s, org_id=org, actor_id=other) == []
    async with _token(org, owner, str(uuid.uuid4())) as s:
        with pytest.raises(ForbiddenError):
            await svc.mine(s, org_id=org, actor_id=owner)


async def test_the_list_offers_a_composer_only_where_a_send_succeeds() -> None:
    org, owner = await _org()
    other = await _member_of(org, owner)
    token = str(uuid.uuid4())
    relayed = await _session(org, owner, token)
    unrelayed = await _session(org, owner, token, accepts=False)

    async def can_send(user: uuid.UUID, kind: str, subject: str | None) -> dict[uuid.UUID, bool]:
        async with tenant_session(
            str(org), str(user), actor_kind=kind, actor_subject_id=subject
        ) as s:
            ctx = OpCtx(session=s, org_id=org, actor_id=user)
            page = await ops.list_(ctx, ops.SessionsListIn())
        return {r.session_id: r.can_send for r in page.sessions if r.session_id}

    assert await can_send(owner, "human_api", None) == {relayed: True, unrelayed: False}
    assert await can_send(other, "human_api", None) == {relayed: False, unrelayed: False}
    assert await can_send(owner, "mcp_token", token) == {relayed: False, unrelayed: False}


# --- retention, and the token the relays share -------------------------------


async def test_messages_go_thirty_days_after_their_session_closed() -> None:
    org, user = await _org()
    token = str(uuid.uuid4())
    old = await _session(org, user, token)
    recent = await _session(org, user, token)
    old_msg = await _send(org, user, old)
    recent_msg = await _send(org, user, recent)
    async with _token(org, user, token) as s:
        for sid in (old, recent):
            await sessions_svc.close_session(s, org_id=org, actor_id=user, session_id=sid)
        await s.execute(
            text("UPDATE agent_sessions SET closed_at = now() - interval '31 days' WHERE id = :i"),
            {"i": old},
        )
    async with tenant_session(str(org), str(user), actor_kind="system") as s:
        assert await svc.purge_closed(s) == 1
    async with tenant_session(str(org), str(user)) as s:
        left = set((await s.execute(select(AgentSessionMessage.id))).scalars().all())
    assert recent_msg.id in left and old_msg.id not in left


async def test_a_token_is_touched_at_most_once_a_minute() -> None:
    """Observed failing against 0012's function, which wrote ``last_used_at``
    on every call: ten relays polling every five seconds on one CLI token."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        minted = await agent_tokens.mint(s, org_id=org, actor_id=user, name="cli", ttl_days=None)
    assert await agent_tokens.authenticate(minted.raw) is not None

    async def touched() -> dt.datetime:
        async with tenant_session(str(org), str(user)) as s:
            return (
                await s.execute(
                    select(AgentToken.last_used_at).where(AgentToken.id == minted.token.id)
                )
            ).scalar_one()

    first = await touched()
    assert await agent_tokens.authenticate(minted.raw) is not None
    assert await touched() == first
    async with tenant_session(str(org), str(user)) as s:
        await s.execute(
            text(
                "UPDATE agent_tokens SET last_used_at = now() - interval '2 minutes' WHERE id = :i"
            ),
            {"i": minted.token.id},
        )
    aged = await touched()
    assert await agent_tokens.authenticate(minted.raw) is not None
    assert await touched() > aged
