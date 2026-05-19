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
    payment_iban: str | None = None
    description: str | None = None
    default_billable: bool = True
    tariffa: Decimal | None = None
    valuta: str = "EUR"
    timezone: str | None = None


# Tag kinds any member may create/rename (free-form facets): the
# generic label and the memory channel. Client/project carry typed
# satellite profiles and stay owner/admin-gated.
_MEMBER_TAG_KINDS: frozenset[TagKind] = frozenset({TagKind.generic, TagKind.memory_channel})


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
    if kind is TagKind.memory_channel:
        # Channels are a controlled, seeded vocabulary managed by the
        # platform admin via /memory/channels, never created ad-hoc from
        # arbitrary user input through the generic tag endpoint: an
        # integration needs a deterministic, well-known target, not a
        # free-form tag (docs/adr/0005, FR-8).
        raise DomainError(MessageCode.CHANNEL_NOT_TAG_CREATABLE)
    minimum = Role.member if kind in _MEMBER_TAG_KINDS else Role.admin
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
            payment_iban=profile.payment_iban,
            description=profile.description,
            default_billable=profile.default_billable,
            tariffa=profile.tariffa,
            valuta=profile.valuta,
            timezone=profile.timezone,
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
_DEFAULT_PROJECT_NAME = "General"


async def _remember_default(
    session: AsyncSession,
    org: Organization | None,
    settings: dict[str, object],
    key: str,
    tag_id: uuid.UUID,
) -> None:
    """Best-effort: cache the default tag id in organizations.settings
    as a fast path. Only a cache — correctness no longer depends on it
    (the natural key does), so an RLS-hidden Organization row (org is
    None) or an already-current pointer is simply skipped."""
    if org is None or settings.get(key) == str(tag_id):
        return
    org.settings = {**settings, key: str(tag_id)}
    await session.flush()


async def _ensure_default_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: TagKind,
    name: str,
    settings_key: str,
) -> tuple[uuid.UUID, bool]:
    """Get-or-create the singleton default tag, idempotent on the
    natural key ``uq_tags(org_id, kind, name)``. The settings pointer
    is only a fast-path cache: a path that creates the default without
    persisting the pointer (or where the Organization row is RLS-hidden,
    so ``org is None`` and the pointer is never written) can no longer
    cause a duplicate-insert ``tag.duplicate``. Returns (tag_id,
    created)."""
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    settings = dict(org.settings) if org and org.settings else {}
    cur = settings.get(settings_key)
    if cur is not None:
        exists = (
            await session.execute(
                select(Tag.id).where(Tag.id == uuid.UUID(str(cur)), Tag.kind == kind)
            )
        ).scalar_one_or_none()
        if exists is not None:
            return uuid.UUID(str(cur)), False
    # Authoritative: (org_id, kind, name) is unique, so an existing
    # default tag — created by any sibling path, in any order — is THE
    # default. Reuse it instead of colliding on insert.
    existing = (
        await session.execute(select(Tag.id).where(Tag.kind == kind, Tag.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        await _remember_default(session, org, settings, settings_key, existing)
        return existing, False
    try:
        tag = await _insert_tag(session, org_id, kind, name, None)
    except DomainError as exc:
        # Lost a concurrent race: re-resolve the now-existing row.
        if exc.code is not MessageCode.TAG_DUPLICATE:
            raise
        raced = (
            await session.execute(select(Tag.id).where(Tag.kind == kind, Tag.name == name))
        ).scalar_one_or_none()
        if raced is None:
            raise
        await _remember_default(session, org, settings, settings_key, raced)
        return raced, False
    await _remember_default(session, org, settings, settings_key, tag.id)
    return tag.id, True


async def ensure_default_client(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """Every project belongs to a client; a workspace always has a
    default ("Personal") for personal projects/tasks. Idempotent on the
    natural key (not a denormalized pointer). System action (no role
    gate) so a member can create a project."""
    tag_id, created = await _ensure_default_tag(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=TagKind.client,
        name=_DEFAULT_CLIENT_NAME,
        settings_key="default_client_tag_id",
    )
    if not created:
        return tag_id
    session.add(ClientProfile(tag_id=tag_id, org_id=org_id, ragione_sociale=_DEFAULT_CLIENT_NAME))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="ensure_default_client",
    )
    return tag_id


async def ensure_default_project(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    """Every task belongs to a project (and thus, transitively, to a
    client). A workspace always has a default ("General") project under
    the default ("Personal") client for otherwise-orphan tasks.
    Idempotent on the natural key. The default client is ensured first
    so a fresh workspace gets the full chain in any call order."""
    client_id = await ensure_default_client(session, org_id=org_id, actor_id=actor_id)
    tag_id, created = await _ensure_default_tag(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=TagKind.project,
        name=_DEFAULT_PROJECT_NAME,
        settings_key="default_project_tag_id",
    )
    if not created:
        return tag_id
    session.add(ProjectProfile(tag_id=tag_id, org_id=org_id, client_tag_id=client_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="ensure_default_project",
    )
    return tag_id


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
    minimum = Role.member if tag.kind in _MEMBER_TAG_KINDS else Role.admin
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


# --- Memory channels (controlled, seeded vocabulary; FR-8) ---------
#
# A memory channel is a ``memory_channel`` tag whose ``system_key`` is a
# stable slug. Integrations (email ingest, Telegram) resolve their
# target channel by this key, never by a user-chosen name. The four
# canonical channels are seeded idempotently per tenant; the platform
# admin may add custom (keyless or keyed) channels via /memory/channels.
# Enable/disable reuses the pre-existing tag ``status`` soft-state
# ('active' vs 'archived'); a disabled channel is not a valid write/
# search target. Seeded channels are renamable but their key is
# immutable and they are not deletable (disable instead).
_CHANNEL_ACTIVE = "active"
_CHANNEL_DISABLED = "archived"

# slug -> human (English) name. Order is the seed/list order.
CANONICAL_MEMORY_CHANNELS: tuple[tuple[str, str], ...] = (
    ("email", "Email"),
    ("telegram", "Telegram"),
    ("manual", "Manual"),
    ("agent", "Agent"),
)
_CANONICAL_KEYS: frozenset[str] = frozenset(k for k, _ in CANONICAL_MEMORY_CHANNELS)


async def ensure_default_memory_channels(session: AsyncSession, *, org_id: uuid.UUID) -> None:
    """Seed the four canonical memory channels for a tenant, idempotent.

    Mirrors the lazy ``ensure_default_*`` bootstrap shape (called from
    the same surfaces that need them, e.g. ``GET /memory/channels`` and
    the memory write/search entry points), so a fresh tenant always has
    them regardless of call order. System action (no role gate) so any
    member listing channels triggers the one-time seed.

    Idempotency is guarded by an existence query on
    ``(org_id, system_key, kind=memory_channel)`` per channel -- NOT via
    ``organizations.settings`` -- so it never collides with the
    client/project default-tag settings keys and is safe to call
    repeatedly and from any order (it is a distinct ``kind`` with its
    own unique surface, the 0042 partial index on
    ``(org_id, system_key)``). Re-seeding is a no-op and never raises
    ``tag.duplicate``.
    """
    for key, display in CANONICAL_MEMORY_CHANNELS:
        existing = (
            await session.execute(
                select(Tag.id).where(
                    Tag.kind == TagKind.memory_channel,
                    Tag.system_key == key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        tag = Tag(
            org_id=org_id,
            kind=TagKind.memory_channel,
            name=display,
            system_key=key,
            status=_CHANNEL_ACTIVE,
        )
        try:
            async with session.begin_nested():
                session.add(tag)
                await session.flush()
        except IntegrityError:
            # A concurrent seed (or a pre-existing same-name channel)
            # already created it: idempotent, swallow and move on.
            continue


async def list_memory_channels(session: AsyncSession, *, org_id: uuid.UUID) -> list[Tag]:
    """Configured channels for the tenant (RLS-scoped). Seeds the
    canonical set first so a fresh tenant always lists the four."""
    await ensure_default_memory_channels(session, org_id=org_id)
    rows = await session.execute(
        select(Tag).where(Tag.kind == TagKind.memory_channel).order_by(Tag.name)
    )
    return list(rows.scalars().all())


async def _get_channel(session: AsyncSession, *, tag_id: uuid.UUID) -> Tag:
    tag = (await session.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None or tag.kind is not TagKind.memory_channel:
        raise NotFoundError(MessageCode.CHANNEL_NOT_FOUND)
    return tag


async def create_memory_channel(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
    system_key: str | None = None,
) -> Tag:
    """Create a custom memory channel (platform-admin only; the gate is
    enforced in the adapter, same as the admin surface). A custom
    channel may be keyless (``system_key`` None) or carry an
    admin-chosen key; the 0042 partial unique index rejects a duplicate
    key as ``tag.duplicate``."""
    tag = Tag(
        org_id=org_id,
        kind=TagKind.memory_channel,
        name=name,
        system_key=system_key,
        status=_CHANNEL_ACTIVE,
    )
    try:
        async with session.begin_nested():
            session.add(tag)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.TAG_DUPLICATE) from exc
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="create_memory_channel",
    )
    return tag


async def update_memory_channel(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
    name: str | None = None,
    enabled: bool | None = None,
    system_key: str | None = None,
) -> Tag:
    """Rename and/or enable/disable a channel (platform-admin only).

    A seeded (canonical-key) channel may be renamed and disabled, but
    its ``system_key`` is IMMUTABLE: any attempt to change it (to a
    different value) is rejected. ``enabled`` maps to the pre-existing
    tag ``status`` soft-state ('active' / 'archived'); a disabled
    channel is not a valid write/search target. Custom channels are
    fully editable.
    """
    tag = await _get_channel(session, tag_id=tag_id)
    is_seeded = tag.system_key in _CANONICAL_KEYS
    if system_key is not None and system_key != tag.system_key:
        if is_seeded:
            raise DomainError(MessageCode.CHANNEL_KEY_IMMUTABLE)
        tag.system_key = system_key
    if name is not None:
        tag.name = name
    if enabled is not None:
        tag.status = _CHANNEL_ACTIVE if enabled else _CHANNEL_DISABLED
    tag.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag.id,
        action="update_memory_channel",
    )
    return tag


async def delete_memory_channel(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Delete a custom channel (platform-admin only). A seeded
    (canonical-key) channel is NOT deletable -- disable it instead --
    so historical blobs keep a stable, well-known facet."""
    tag = await _get_channel(session, tag_id=tag_id)
    if tag.system_key in _CANONICAL_KEYS:
        raise DomainError(MessageCode.CHANNEL_SEEDED_UNDELETABLE)
    await session.execute(delete(Tag).where(Tag.id == tag_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="delete_memory_channel",
    )


async def resolve_channel_by_key(
    session: AsyncSession, *, org_id: uuid.UUID, channel_key: str
) -> Tag:
    """Resolve a tenant's enabled ``memory_channel`` tag by its stable
    ``system_key`` (RLS scopes ``tags`` to the org, so a foreign org's
    channel is invisible -> not found). A disabled channel is treated
    as absent (not a valid write/search target)."""
    tag = (
        await session.execute(
            select(Tag).where(
                Tag.kind == TagKind.memory_channel,
                Tag.system_key == channel_key,
            )
        )
    ).scalar_one_or_none()
    if tag is None or tag.status != _CHANNEL_ACTIVE:
        raise NotFoundError(MessageCode.CHANNEL_NOT_FOUND)
    return tag
