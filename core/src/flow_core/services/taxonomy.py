"""Taxonomy service: unified tags + typed client/project profiles
(docs/adr/0003). RBAC, optimistic concurrency, i18n, audit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.client_profile import ClientProfile
from flow_core.models.membership import Role
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.services import audit
from flow_core.services.rbac import require_role


@dataclass(frozen=True, slots=True)
class ClientInput:
    ragione_sociale: str
    id_paese: str | None = None
    id_codice: str | None = None
    codice_fiscale: str | None = None
    indirizzo: str | None = None
    cap: str | None = None
    comune: str | None = None
    provincia: str | None = None
    nazione: str | None = None
    codice_destinatario: str | None = None
    pec: str | None = None


async def _insert_tag(
    session: AsyncSession,
    org_id: uuid.UUID,
    kind: TagKind,
    name: str,
    color: str | None,
) -> Tag:
    tag = Tag(org_id=org_id, kind=kind, name=name, color=color)
    try:
        async with session.begin_nested():
            session.add(tag)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.TAG_DUPLICATE) from exc
    return tag


async def create_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: TagKind,
    name: str,
    color: str | None = None,
) -> Tag:
    minimum = Role.admin if kind is not TagKind.generic else Role.member
    await require_role(session, org_id, actor_id, minimum)
    tag = await _insert_tag(session, org_id, kind, name, color)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="create",
    )
    return tag


async def create_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    profile: ClientInput,
) -> Tag:
    await require_role(session, org_id, actor_id, Role.admin)
    tag = await _insert_tag(session, org_id, TagKind.client, name, None)
    session.add(
        ClientProfile(
            tag_id=tag.id,
            org_id=org_id,
            ragione_sociale=profile.ragione_sociale,
            id_paese=profile.id_paese,
            id_codice=profile.id_codice,
            codice_fiscale=profile.codice_fiscale,
            indirizzo=profile.indirizzo,
            cap=profile.cap,
            comune=profile.comune,
            provincia=profile.provincia,
            nazione=profile.nazione,
            codice_destinatario=profile.codice_destinatario,
            pec=profile.pec,
        )
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="create_client",
    )
    return tag


async def create_project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    client_tag_id: uuid.UUID | None = None,
    tariffa: Decimal | None = None,
    valuta: str = "EUR",
    budget: Decimal | None = None,
) -> Tag:
    await require_role(session, org_id, actor_id, Role.admin)
    if client_tag_id is not None:
        client = await session.execute(
            select(Tag.id).where(Tag.id == client_tag_id, Tag.kind == TagKind.client)
        )
        if client.scalar_one_or_none() is None:
            raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    tag = await _insert_tag(session, org_id, TagKind.project, name, None)
    session.add(
        ProjectProfile(
            tag_id=tag.id,
            org_id=org_id,
            client_tag_id=client_tag_id,
            tariffa=tariffa,
            valuta=valuta,
            budget=budget,
        )
    )
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="create_project",
    )
    return tag


async def list_tags(
    session: AsyncSession, *, org_id: uuid.UUID, kind: TagKind | None = None
) -> list[Tag]:
    stmt = select(Tag).order_by(Tag.kind, Tag.name)
    if kind is not None:
        stmt = stmt.where(Tag.kind == kind)
    return list((await session.execute(stmt)).scalars().all())


async def get_tag(session: AsyncSession, *, org_id: uuid.UUID, tag_id: uuid.UUID) -> Tag:
    tag = (await session.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return tag


async def find_tag_by_name(
    session: AsyncSession, *, org_id: uuid.UUID, kind: TagKind, name: str
) -> Tag:
    """Resolve a tag by exact name within a kind. Raises NotFound if
    none, TAG_AMBIGUOUS if more than one (docs/adr/0021: confirm,
    never guess)."""
    rows = list(
        (await session.execute(select(Tag).where(Tag.kind == kind, Tag.name == name)))
        .scalars()
        .all()
    )
    if not rows:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if len(rows) > 1:
        raise DomainError(MessageCode.TAG_AMBIGUOUS, name=name)
    return rows[0]


async def update_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
    expected_version: int,
    name: str | None = None,
    color: str | None = None,
    status: str | None = None,
) -> int:
    tag = await get_tag(session, org_id=org_id, tag_id=tag_id)
    minimum = Role.admin if tag.kind is not TagKind.generic else Role.member
    await require_role(session, org_id, actor_id, minimum)
    values: dict[str, str] = {}
    if name is not None:
        values["name"] = name
    if color is not None:
        values["color"] = color
    if status is not None:
        values["status"] = status
    new_version = await optimistic_update(
        session,
        Tag,
        pk=tag_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="update",
        diff=values,
    )
    return new_version
