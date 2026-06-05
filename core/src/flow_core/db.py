"""Database access and tenant context for RLS.

Multi-tenant isolation is a primary defense (docs/adr/0002, 0007):
every application transaction sets session GUCs that the RLS policies
read via ``current_setting(...)``. Queries cannot see another
org/project's data even if they forget the predicate.

GUCs used:
- ``app.current_org``     uuid of the current organization
- ``app.current_user``    uuid of the authenticated user
- ``app.current_project`` uuid of the current project (memory), optional
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from flow_core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


# Closed set of actor kinds; mirrored at the DB level by the
# ``ck_activity_log_actor_kind`` CHECK constraint (migration 0083).
# Kept as a literal type rather than an Enum so it remains a plain
# string at the GUC boundary.
ActorKind = str  # human_direct | human_api | human_telegram | agent_run | mcp_token | system


@asynccontextmanager
async def tenant_session(
    org_id: str,
    user_id: str,
    project_id: str | None = None,
    *,
    actor_kind: ActorKind = "human_direct",
    actor_subject_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """A transactional session with the tenant GUCs set for RLS.

    The GUCs are ``set_config(..., is_local => true)``: they apply only
    to the current transaction, no leak across pool connections.

    ``actor_kind`` and ``actor_subject_id`` propagate the **type of
    caller** down to ``audit.log``, which reads them via
    ``current_setting`` and persists them on ``activity_log``. The
    default ``human_direct`` keeps every existing call site
    backward-compatible (the test suite and the SPA's REST paths still
    work without changes).
    """
    # Imported lazily so the services layer (which depends on the models
    # registered at import time) doesn't pull db.py into a circular cycle.
    # The side-effect of the import is what matters: it registers the
    # SQLAlchemy mapper-level event listeners that track task/checklist
    # mutations into ``session.info``.
    from flow_core.services.task_search import flush_task_search_dirty

    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true),"
                    "       set_config('app.current_user', :usr, true),"
                    "       set_config('app.current_project', :prj, true),"
                    "       set_config('app.current_actor_kind', :ak, true),"
                    "       set_config('app.current_actor_subject', :asubj, true)"
                ),
                {
                    "org": org_id,
                    "usr": user_id,
                    "prj": project_id or "",
                    "ak": actor_kind,
                    "asubj": actor_subject_id or "",
                },
            )
            yield session
            # Resync task-search index for any task/checklist row that
            # mutated inside this transaction. Sits inside the outer
            # ``begin()`` so the blob upsert is atomic with the source
            # mutation (FTS is visible the instant the commit lands;
            # the embedding vector is best-effort within a 2 s timeout
            # and the backfill worker fills the rest).
            await flush_task_search_dirty(session)


@asynccontextmanager
async def admin_session(*, actor_kind: ActorKind = "system") -> AsyncIterator[AsyncSession]:
    """A no-tenant session: sets the actor kind but never ``app.current_org``.

    Used by the worker to enumerate workspaces before fanning out into a
    per-org ``tenant_session`` (and by bootstrap/migrations/tests). Under
    RLS this session is fail-closed for org-scoped tables EXCEPT the
    enumeration tables (``organizations``, ``memberships``,
    ``google_calendar_subscriptions``), which carry a ``FOR SELECT`` policy
    that opens to a system session with no current org (migration 0029).
    So a ``system`` caller can list every workspace and resolve its owner,
    yet still sees nothing in tasks/notifications/etc. until it narrows to
    a single org via ``tenant_session``.

    ``actor_kind`` defaults to ``system`` so audit rows from this path are
    attributed to a system actor and the enumeration policies apply;
    callers can override (e.g. a CLI acting for a specific operator), but a
    non-system kind sees no org-scoped rows at all (fail-closed).
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_actor_kind', :ak, true),"
                    "       set_config('app.current_actor_subject', '', true)"
                ),
                {"ak": actor_kind},
            )
            yield session


@asynccontextmanager
async def with_actor(
    session: AsyncSession,
    *,
    actor_kind: ActorKind,
    actor_subject_id: str | None = None,
) -> AsyncIterator[None]:
    """Temporarily shift the GUC ``actor_kind`` + ``actor_subject``
    within an already-open session.

    Use case: ``services/agent_runtime.start_run`` opens nothing of
    its own (the caller hands it a tenant session), yet the audit
    rows it produces should read ``actor_kind='agent_run'`` rather
    than the caller's kind. The contextmanager saves the current
    GUC values, sets the new ones, and restores them on exit so the
    rest of the caller's transaction is unaffected.

    Because ``set_config(..., true)`` is transaction-local, the
    restore-on-exit pattern is safe: we never leak across connections,
    we only narrow a window inside one transaction.
    """
    saved = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true)"
            )
        )
    ).one()
    saved_kind = saved[0] or "human_direct"
    saved_subj = saved[1] or ""
    await session.execute(
        text(
            "SELECT set_config('app.current_actor_kind', :ak, true),"
            "       set_config('app.current_actor_subject', :asubj, true)"
        ),
        {"ak": actor_kind, "asubj": actor_subject_id or ""},
    )
    try:
        yield
    finally:
        await session.execute(
            text(
                "SELECT set_config('app.current_actor_kind', :ak, true),"
                "       set_config('app.current_actor_subject', :asubj, true)"
            ),
            {"ak": saved_kind, "asubj": saved_subj},
        )
