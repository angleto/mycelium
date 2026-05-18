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


@asynccontextmanager
async def tenant_session(
    org_id: str,
    user_id: str,
    project_id: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """A transactional session with the tenant GUCs set for RLS.

    The GUCs are ``set_config(..., is_local => true)``: they apply only
    to the current transaction, no leak across pool connections.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.current_org', :org, true),"
                    "       set_config('app.current_user', :usr, true),"
                    "       set_config('app.current_project', :prj, true)"
                ),
                {"org": org_id, "usr": user_id, "prj": project_id or ""},
            )
            yield session


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """A session with no tenant context. Bootstrap/migrations/tests only.

    Do not use in application paths: RLS stays active and, without
    GUCs, sees no org-scoped rows (fail-closed).
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            yield session
