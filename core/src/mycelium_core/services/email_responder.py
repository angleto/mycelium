"""Autonomous email responder (WS-4).

The LLM half of the responder: claim queued ``email_responder_jobs`` and
draft a reply for each via the per-org metered provider seam
(``resolve_llm``). A draft is a SINGLE completion grounded in the thread
(WS-2) plus the account's tagged email memory (WS-1) -- no tool loop, so it
is cheap and predictable. The draft is parked in state ``drafted`` and
NEVER sent here: sending is the human-gated ``email.approve_draft`` path
(email is outward-facing/irreversible).

Session discipline mirrors ``telegram_assistant``: claiming (mark running)
and drafting run in separate tenant sessions so a slow model call never
holds the queue row lock. The worker (``mycelium_worker.email_responder``)
enumerates workspaces and drives these two steps per org.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.errors import NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.email import EmailMessage, EmailResponderJob
from mycelium_core.services import email as email_svc
from mycelium_core.services import memory as memory_svc
from mycelium_core.services.llm_resolver import resolve_llm

_log = logging.getLogger("mycelium.email_responder")

_SYSTEM_PROMPT = (
    "You draft a reply to an email on the user's behalf. Write a concise, "
    "professional reply in the SAME language as the latest message. Use the "
    "thread and the background notes only as context to be accurate. Output "
    "ONLY the reply body text: no subject line, no placeholder like [Name], "
    "no meta commentary."
)

_THREAD_BODY_MAX = 2000
_CONTEXT_BODY_MAX = 800
_CONTEXT_HITS = 5


def _build_prompt(
    *,
    thread: Sequence[EmailMessage],
    target: EmailMessage,
    context_texts: Sequence[str],
) -> str:
    parts: list[str] = ["# Thread (oldest first)"]
    for m in thread:
        parts.append(
            f"From: {m.from_addr}\nSubject: {m.subject or ''}\n"
            f"{(m.body_text or '').strip()[:_THREAD_BODY_MAX]}\n---"
        )
    if context_texts:
        parts.append("# Background notes (from your email memory)")
        for text in context_texts:
            parts.append(text + "\n---")
    parts.append("# Task")
    parts.append(f"Draft a reply to the latest message from {target.from_addr}.")
    return "\n".join(parts)


async def claim_pending(session: AsyncSession, *, limit: int = 10) -> list[uuid.UUID]:
    """Claim up to ``limit`` pending jobs (FOR UPDATE SKIP LOCKED), mark them
    ``running``, and return their ids. RLS scopes to the session's tenant, so
    the caller runs this once per workspace."""
    rows = (
        (
            await session.execute(
                select(EmailResponderJob)
                .where(EmailResponderJob.status == "pending")
                .order_by(EmailResponderJob.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(tz=dt.UTC)
    for job in rows:
        job.status = "running"
        job.started_at = now
    await session.flush()
    return [job.id for job in rows]


async def generate_draft(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    job: EmailResponderJob,
    provider: LLMProvider | None = None,
) -> tuple[str, str | None]:
    """Produce the draft text (and the model id that wrote it) for a job:
    the latest message + its thread + the account's tagged email memory, in
    one metered completion. ``provider`` is injectable for tests; in prod the
    metered per-org provider is resolved."""
    msg = await email_svc.get_message(session, org_id=org_id, message_id=job.message_id)
    thread = await email_svc.get_thread_for_message(
        session, org_id=org_id, message_id=job.message_id
    )
    # Ground the draft in the account's tagged email memory (best-effort: a
    # retrieval/embed hiccup must not fail the draft).
    context_texts: list[str] = []
    query = (msg.subject or msg.body_text or "").strip()[:500]
    if query:
        try:
            default_tags = list(await email_svc.default_tag_ids(session, msg.account_id))
            hits = await memory_svc.retrieve(
                session,
                org_id=org_id,
                actor_id=actor_id,
                project_id=None,
                query=query,
                operation_id=f"email-draft-ctx:{job.id}",
                channel_key="email",
                tag_ids=default_tags or None,
                limit=_CONTEXT_HITS,
            )
            for h in hits:
                text = (getattr(h.blob, "text", None) or "").strip()[:_CONTEXT_BODY_MAX]
                if text:
                    context_texts.append(text)
        except Exception:  # context is a nicety, not a requirement
            _log.warning("email draft: context retrieval failed for job=%s", job.id)

    prompt = _build_prompt(thread=thread, target=msg, context_texts=context_texts)
    llm = provider or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"email-responder:{job.id}",
        op="email_draft",
    )
    result = await llm.complete(system=_SYSTEM_PROMPT, messages=[("user", prompt)])
    return result.text.strip(), result.model_id


async def draft_job(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    job_id: uuid.UUID,
    provider: LLMProvider | None = None,
) -> str:
    """Draft one claimed (``running``) job: store the reply and flip to
    ``drafted``; on any failure flip to ``failed`` with the error. Returns
    the resulting status. Never raises (per-job isolation for the worker)."""
    job = (
        await session.execute(select(EmailResponderJob).where(EmailResponderJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError(MessageCode.EMAIL_DRAFT_NOT_FOUND)
    try:
        draft, model_id = await generate_draft(
            session, org_id=org_id, actor_id=actor_id, job=job, provider=provider
        )
        job.draft_reply = draft
        job.origin_model_id = model_id
        job.status = "drafted"
        job.error = None
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        _log.warning("email draft failed for job=%s: %s", job_id, exc)
    job.finished_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    return job.status
