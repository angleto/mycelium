"""Accesso al database e contesto tenant per la RLS.

L'isolamento multi-tenant e una difesa primaria (docs/adr/0002, 0007):
ogni transazione applicativa imposta GUC di sessione che le policy RLS
leggono via ``current_setting(...)``. Le query non possono vedere dati
di un'altra org/progetto anche se dimenticano il predicato.

GUC usati:
- ``app.current_org``     uuid dell'organizzazione corrente
- ``app.current_user``    uuid dell'utente autenticato
- ``app.current_project`` uuid del progetto corrente (memoria), opzionale
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
    """Sessione in transazione con i GUC di tenant impostati per la RLS.

    I GUC sono ``set_config(..., is_local => true)``: valgono solo per la
    transazione corrente, niente leak tra connessioni del pool.
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
    """Sessione senza contesto tenant. Solo per bootstrap/migrazioni/test.

    Non usare nei path applicativi: la RLS resta attiva e senza GUC non
    vede righe org-scoped (fail-closed).
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            yield session
