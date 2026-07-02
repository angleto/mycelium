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

from mycelium_core.config import get_settings

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
# ``ck_activity_log_actor_kind`` CHECK constraint (baseline 0001, widened in
# migration 0077 to add ``issuer_api_key``). Kept as a literal type rather than
# an Enum so it remains a plain string at the GUC boundary.
ActorKind = str  # human_direct|human_api|human_telegram|agent_run|mcp_token|system|issuer_api_key


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
    # and note-part mutations into ``session.info``.
    from mycelium_core.services.note_search import flush_note_search_dirty
    from mycelium_core.services.task_search import flush_task_search_dirty

    sm = get_sessionmaker()
    async with sm() as session:
        # Explicit begin/commit instead of the ``session.begin()`` context
        # manager: SQLAlchemy forbids further statements after a mid-CM
        # commit, and the two-phase transmit (ADR-0046) must be able to
        # ``tenant_checkpoint`` -- commit the prepared fiscal identifiers and
        # keep working on the same session. Semantics are unchanged for every
        # other caller: begin-on-first-statement (autobegin), commit on clean
        # exit, rollback on exception.
        try:
            # Stashed so ``tenant_rollback`` can re-arm after an ABORTED
            # transaction (an aborted tx cannot be queried for its GUCs).
            session.info["tenant_gucs"] = (
                org_id,
                user_id,
                project_id or "",
                actor_kind,
                actor_subject_id or "",
                "",
            )
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
            # mutated inside this transaction. Sits before the commit so
            # the blob upsert is atomic with the source mutation (FTS is
            # visible the instant the commit lands; the embedding vector
            # is best-effort within a 2 s timeout and the backfill worker
            # fills the rest).
            await flush_task_search_dirty(session)
            # Same chokepoint for the note index: one blob per note PART,
            # resync'd atomically with the part mutation.
            await flush_note_search_dirty(session)
            trans = session.sync_session.get_transaction()
            if trans is not None and not trans.is_active:
                # A flush error contained by a savepoint pattern (e.g. the
                # EXCLUDE-overlap 409s) deactivates the outer transaction
                # while the request still renders a handled error response.
                # The old ``session.begin()`` CM rolled this back silently
                # on exit; keep that contract instead of raising
                # PendingRollbackError from an unconditional commit.
                await session.rollback()
            else:
                await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def _capture_gucs(session: AsyncSession) -> tuple[str, ...]:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('app.current_org', true),"
                "       current_setting('app.current_user', true),"
                "       current_setting('app.current_project', true),"
                "       current_setting('app.current_actor_kind', true),"
                "       current_setting('app.current_actor_subject', true),"
                "       current_setting('app.current_role', true)"
            )
        )
    ).one()
    return tuple(v or "" for v in row)


async def _rearm_gucs(session: AsyncSession, captured: tuple[str, ...]) -> None:
    await session.execute(
        text(
            "SELECT set_config('app.current_org', :org, true),"
            "       set_config('app.current_user', :usr, true),"
            "       set_config('app.current_project', :prj, true),"
            "       set_config('app.current_actor_kind', :ak, true),"
            "       set_config('app.current_actor_subject', :asubj, true),"
            "       set_config('app.current_role', :role, true)"
        ),
        {
            "org": captured[0],
            "usr": captured[1],
            "prj": captured[2],
            "ak": captured[3],
            "asubj": captured[4],
            "role": captured[5],
        },
    )


async def tenant_checkpoint(session: AsyncSession) -> None:
    """Commit the current tenant transaction and re-arm the GUCs in a fresh one.

    The durability primitive of the two-phase transmit (ADR-0046): phase 1
    calls this to make the fiscal identifiers (numero, ProgressivoInvio,
    NomeFile, frozen XML) durable BEFORE the SdI dispatch, releasing the row
    locks; the caller keeps using the same session afterwards.

    The GUCs are ``set_config(..., is_local => true)`` = transaction-local,
    so they die with the commit; this helper captures every tenant GUC
    (including ``app.current_role``, pinned to ``member`` by the public-API
    issuer-key path) and restores it as the FIRST statement of the next
    transaction -- RLS never sees a statement without its tenant context.

    The task/note search-dirty flush hooks run before the commit, exactly as
    at the end of ``tenant_session``, so an FTS blob never lags the source
    row it indexes. CAVEAT: only the ``app.current_*`` GUC set is captured;
    do not checkpoint inside a ``with_actor`` or ``kg_allow_erase`` window
    (any other transaction-local GUC is silently dropped).
    """
    from mycelium_core.services.note_search import flush_note_search_dirty
    from mycelium_core.services.task_search import flush_task_search_dirty

    captured = await _capture_gucs(session)
    await flush_task_search_dirty(session)
    await flush_note_search_dirty(session)
    await session.commit()
    await _rearm_gucs(session, captured)


async def tenant_rollback(session: AsyncSession) -> None:
    """Roll back the current tenant transaction and re-arm the GUCs in a
    fresh one, so the caller can keep using the session (mirror of
    ``tenant_checkpoint`` for the abort path, e.g. the SdI-notification
    redelivery dedupe that rolls back a duplicate ingest).

    The GUCs are re-armed from the values stashed at ``tenant_session`` open
    (an aborted transaction cannot be queried), so a mid-request
    ``app.current_role`` pin or ``with_actor`` override is NOT restored --
    use only on paths that set neither (the system ingest paths)."""
    captured = session.info.get("tenant_gucs")
    await session.rollback()
    if captured is not None:
        await _rearm_gucs(session, captured)


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
