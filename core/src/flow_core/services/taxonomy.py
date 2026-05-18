"""Taxonomy service: unified tags + typed client/project profiles
(docs/adr/0003). RBAC, optimistic concurrency, i18n, audit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.client_profile import ClientProfile
from flow_core.models.membership import Role
from flow_core.models.organization import Organization
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.tag_scope import TagScope
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
    description: str | None = None
    default_billable: bool = True
    tariffa: Decimal | None = None
    valuta: str = "EUR"


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
            description=profile.description,
            default_billable=profile.default_billable,
            tariffa=profile.tariffa,
            valuta=profile.valuta,
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


_DEFAULT_CLIENT_NAME = "Personal"


async def ensure_default_client(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """Every project belongs to a client; a workspace always has a
    default ("Personal") for personal projects/tasks. Idempotent: the
    id is remembered in organizations.settings.default_client_tag_id.
    System action (no role gate) so a member can create a project."""
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    settings = dict(org.settings) if org and org.settings else {}
    cur = settings.get("default_client_tag_id")
    if cur is not None:
        exists = (
            await session.execute(
                select(Tag.id).where(Tag.id == uuid.UUID(str(cur)), Tag.kind == TagKind.client)
            )
        ).scalar_one_or_none()
        if exists is not None:
            return uuid.UUID(str(cur))
    tag = await _insert_tag(session, org_id, TagKind.client, _DEFAULT_CLIENT_NAME, None)
    session.add(ClientProfile(tag_id=tag.id, org_id=org_id, ragione_sociale=_DEFAULT_CLIENT_NAME))
    await session.flush()
    if org is not None:
        org.settings = {**settings, "default_client_tag_id": str(tag.id)}
        await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="ensure_default_client",
    )
    return tag.id


_DEFAULT_PROJECT_NAME = "General"


async def ensure_default_project(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """Every task belongs to a project (and thus, transitively, to a
    client). A workspace always has a default ("General") project under
    the default ("Personal") client for otherwise-orphan tasks.
    Idempotent: id remembered in settings.default_project_tag_id."""
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    settings = dict(org.settings) if org and org.settings else {}
    cur = settings.get("default_project_tag_id")
    if cur is not None:
        exists = (
            await session.execute(
                select(Tag.id).where(
                    Tag.id == uuid.UUID(str(cur)), Tag.kind == TagKind.project
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            return uuid.UUID(str(cur))
    client_id = await ensure_default_client(
        session, org_id=org_id, actor_id=actor_id
    )
    tag = await _insert_tag(
        session, org_id, TagKind.project, _DEFAULT_PROJECT_NAME, None
    )
    session.add(
        ProjectProfile(tag_id=tag.id, org_id=org_id, client_tag_id=client_id)
    )
    await session.flush()
    if org is not None:
        org.settings = {**settings, "default_project_tag_id": str(tag.id)}
        await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="ensure_default_project",
    )
    return tag.id


async def create_project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    client_tag_id: uuid.UUID | None = None,
    budget: Decimal | None = None,
    color: str | None = None,
    description: str | None = None,
) -> Tag:
    await require_role(session, org_id, actor_id, Role.admin)
    if client_tag_id is None:
        # Every project belongs to a client; default to "Personal".
        client_tag_id = await ensure_default_client(session, org_id=org_id, actor_id=actor_id)
    else:
        client = await session.execute(
            select(Tag.id).where(Tag.id == client_tag_id, Tag.kind == TagKind.client)
        )
        if client.scalar_one_or_none() is None:
            raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    # The project's colour is the project tag's colour (single source).
    tag = await _insert_tag(session, org_id, TagKind.project, name, color)
    session.add(
        ProjectProfile(
            tag_id=tag.id,
            org_id=org_id,
            client_tag_id=client_tag_id,
            budget=budget,
            description=description,
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
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    kind: TagKind | None = None,
    for_project: uuid.UUID | None = None,
) -> list[Tag]:
    stmt = select(Tag).order_by(Tag.kind, Tag.name)
    if kind is not None:
        stmt = stmt.where(Tag.kind == kind)
    if for_project is not None:
        # Visible for a project = global (no scope rows) OR scoped to
        # that project or its client.
        targets: list[uuid.UUID] = [for_project]
        prof = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == for_project)
            )
        ).scalar_one_or_none()
        if prof is not None:
            targets.append(prof)
        scoped = select(TagScope.tag_id).distinct()
        in_scope = select(TagScope.tag_id).where(TagScope.target_tag_id.in_(targets))
        stmt = stmt.where(or_(Tag.id.not_in(scoped), Tag.id.in_(in_scope)))
    return list((await session.execute(stmt)).scalars().all())


async def scopes_by_tag(
    session: AsyncSession, *, tag_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Batched tag -> its scope target ids (empty list = global)."""
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    if not tag_ids:
        return out
    rows = await session.execute(
        select(TagScope.tag_id, TagScope.target_tag_id).where(TagScope.tag_id.in_(tag_ids))
    )
    for tid, target in rows.all():
        out.setdefault(tid, []).append(target)
    return out


async def set_tag_scope(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
    target_ids: Sequence[uuid.UUID],
) -> None:
    """Replace a tag's scope with ``target_ids`` (each a project or
    client tag). Empty => global."""
    await require_role(session, org_id, actor_id, Role.admin)
    await get_tag(session, org_id=org_id, tag_id=tag_id)
    valid = {
        r
        for (r,) in (
            await session.execute(
                select(Tag.id).where(
                    Tag.id.in_(list(target_ids)),
                    Tag.kind.in_([TagKind.project, TagKind.client]),
                )
            )
        ).all()
    }
    bad = set(target_ids) - valid
    if bad:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    await session.execute(delete(TagScope).where(TagScope.tag_id == tag_id))
    for target in valid:
        session.add(TagScope(org_id=org_id, tag_id=tag_id, target_tag_id=target))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="set_scope",
        diff={"targets": str(len(valid))},
    )


async def get_tag(session: AsyncSession, *, org_id: uuid.UUID, tag_id: uuid.UUID) -> Tag:
    tag = (await session.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return tag


async def list_clients(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[tuple[Tag, ClientProfile]]:
    rows = await session.execute(
        select(Tag, ClientProfile)
        .join(ClientProfile, ClientProfile.tag_id == Tag.id)
        .where(Tag.kind == TagKind.client)
        .order_by(Tag.name)
    )
    return [(t, p) for t, p in rows.all()]


async def list_projects(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[tuple[Tag, ProjectProfile]]:
    rows = await session.execute(
        select(Tag, ProjectProfile)
        .join(ProjectProfile, ProjectProfile.tag_id == Tag.id)
        .where(Tag.kind == TagKind.project)
        .order_by(Tag.name)
    )
    return [(t, p) for t, p in rows.all()]


async def update_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
    name: str | None = None,
    fields: dict[str, str | None] | None = None,
) -> None:
    """Edit a client's name and its invoicing card (ClientProfile)."""
    await require_role(session, org_id, actor_id, Role.admin)
    tag = await get_tag(session, org_id=org_id, tag_id=tag_id)
    if tag.kind is not TagKind.client:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    prof = (
        await session.execute(select(ClientProfile).where(ClientProfile.tag_id == tag_id))
    ).scalar_one_or_none()
    if prof is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if name is not None:
        tag.name = name
        tag.version += 1
    for k, v in (fields or {}).items():
        setattr(prof, k, v)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="update_client",
    )


async def update_project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
    name: str | None = None,
    fields: dict[str, object] | None = None,
) -> None:
    """Edit a project's name and its profile (rate/currency/budget/
    color/description/client link). Billable is a client default now."""
    await require_role(session, org_id, actor_id, Role.admin)
    tag = await get_tag(session, org_id=org_id, tag_id=tag_id)
    if tag.kind is not TagKind.project:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    prof = (
        await session.execute(select(ProjectProfile).where(ProjectProfile.tag_id == tag_id))
    ).scalar_one_or_none()
    if prof is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    flds = fields or {}
    ctid = flds.get("client_tag_id")
    if ctid is not None:
        ok = await session.execute(select(Tag.id).where(Tag.id == ctid, Tag.kind == TagKind.client))
        if ok.scalar_one_or_none() is None:
            raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    tag_dirty = False
    if name is not None:
        tag.name = name
        tag_dirty = True
    # Colour is a tag attribute (single source), not a profile column.
    if "color" in flds:
        tag.color = flds.pop("color")  # type: ignore[assignment]
        tag_dirty = True
    if tag_dirty:
        tag.version += 1
    for k, v in flds.items():
        setattr(prof, k, v)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="update_project",
    )


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
