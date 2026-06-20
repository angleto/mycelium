"""Taxonomy service: unified tags + typed client/project profiles
(docs/adr/0003). RBAC, optimistic concurrency, i18n, audit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.attachment_store import get_attachment_store
from flow_core.concurrency import optimistic_update
from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.attachment import Attachment
from flow_core.models.client_profile import ClientProfile
from flow_core.models.invoice import Invoice
from flow_core.models.membership import Role
from flow_core.models.memory_blob import MemoryBlob, MemoryBlobTag
from flow_core.models.note import Note
from flow_core.models.note_tag import NoteTag
from flow_core.models.organization import Organization
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.tag_scope import TagScope
from flow_core.models.task import Task
from flow_core.models.task_tag import TaskTag
from flow_core.services import audit
from flow_core.services.rbac import require_role
from flow_core.vat import is_valid_vat_code, normalize_vat


@dataclass(frozen=True, slots=True)
class ClientInput:
    legal_name: str
    first_name: str | None = None
    last_name: str | None = None
    country_code: str | None = None
    vat_number: str | None = None
    tax_code: str | None = None
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    sdi_code: str | None = None
    pec: str | None = None
    invoice_series: str | None = None
    payment_iban: str | None = None
    description: str | None = None
    default_billable: bool = True
    hourly_rate: Decimal | None = None
    currency: str = "EUR"
    timezone: str | None = None
    default_payment_conditions_code: str | None = None
    default_payment_method_code: str | None = None
    default_payment_terms_days: int | None = None
    invoice_language: str | None = None


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
    # Normalize a VIES-form VAT id ("IT09876543210") into IdPaese + bare
    # IdCodice (FatturaPA IdCodice carries no country prefix).
    country_code, vat_number = normalize_vat(profile.vat_number, profile.country_code)
    if not is_valid_vat_code(vat_number, country_code):
        raise DomainError(MessageCode.INVOICE_INVALID, detail=f"client vat_number '{vat_number}'")
    session.add(
        ClientProfile(
            tag_id=tag.id,
            org_id=org_id,
            legal_name=profile.legal_name,
            first_name=profile.first_name,
            last_name=profile.last_name,
            country_code=country_code,
            vat_number=vat_number,
            tax_code=profile.tax_code,
            address=profile.address,
            postal_code=profile.postal_code,
            city=profile.city,
            province=profile.province,
            country=profile.country,
            sdi_code=profile.sdi_code,
            pec=profile.pec,
            invoice_series=profile.invoice_series,
            payment_iban=profile.payment_iban,
            description=profile.description,
            default_billable=profile.default_billable,
            hourly_rate=profile.hourly_rate,
            currency=profile.currency,
            timezone=profile.timezone,
            default_payment_conditions_code=profile.default_payment_conditions_code,
            default_payment_method_code=profile.default_payment_method_code,
            default_payment_terms_days=profile.default_payment_terms_days,
            invoice_language=profile.invoice_language,
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
        kind=TagKind.client,
        name=_DEFAULT_CLIENT_NAME,
        settings_key="default_client_tag_id",
    )
    if not created:
        return tag_id
    session.add(ClientProfile(tag_id=tag_id, org_id=org_id, legal_name=_DEFAULT_CLIENT_NAME))
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


_TAG_ACTIVE = "active"
_TAG_ARCHIVED = "archived"


async def list_tags(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    kind: TagKind | None = None,
    for_project: uuid.UUID | None = None,
    for_client: uuid.UUID | None = None,
    include_archived: bool = False,
    manage: bool = False,
) -> list[Tag]:
    """List tags (RLS-scoped to the tenant).

    ``include_archived`` is False by default: an archived tag must not
    appear on ANY selection/filter surface (task/note pickers, the
    graph, memory) -- excluding it here is the single root fix, every
    caller inherits it. The Tag manager passes ``include_archived=True``
    so an archived tag can still be un-archived. (An already-attached
    archived tag still renders via the owning entity's own serializer,
    so it remains removable -- that path does not go through here.)

    ``for_project`` / ``for_client`` are the focus scope: when the SPA
    focus is a project (resp. a client) only tags that are GLOBAL (no
    ``tag_scope`` rows) OR scoped to that project/client (or, for a
    project, to its client; for a client, to any of its projects) are
    returned, so /graph and /tags stop offering tags of other clients
    and of projects not under the focused client. They are mutually
    compatible (a tag visible under either is included); the SPA sends
    at most one. With neither passed the behaviour is unchanged except
    the archived exclusion above.

    ``manage`` selects the Tag-manager surface (as opposed to the
    TagPicker / graph "Filter by tags" chips). On filter surfaces a
    GLOBAL generic tag (no ``tag_scope`` rows) is hidden under a focus —
    the reported "leak". The manager is the opposite case: it is where a
    global tag gets a "Restrict to..." added, so it must show global
    generics even under a focus, otherwise an unrestricted tag is
    unreachable while a focus is active (the reported bug). The
    structural client/project ownership constraint is unaffected: other
    clients' structural tags stay hidden under a focus on both surfaces."""
    # Explicit org filter on top of RLS (defense-in-depth): a read must
    # not leak across tenants even on a path where the RLS GUC is unset
    # (the SECURITY DEFINER + FORCE RLS gotcha seen in #48/#125).
    stmt = select(Tag).where(Tag.org_id == org_id).order_by(Tag.kind, Tag.name)
    if kind is not None:
        stmt = stmt.where(Tag.kind == kind)
    if not include_archived:
        stmt = stmt.where(Tag.status == _TAG_ACTIVE)
    # The set of scope targets a tag may be scoped to for it to be
    # "visible under the current focus": the project/client itself plus
    # the transitively related client/projects. Empty when no focus is
    # passed (the scope filter is then skipped entirely).
    targets: set[uuid.UUID] = set()
    if for_project is not None:
        targets.add(for_project)
        prof = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == for_project)
            )
        ).scalar_one_or_none()
        if prof is not None:
            targets.add(prof)
    if for_client is not None:
        targets.add(for_client)
        # Resolve client -> its projects (ProjectProfile.client_tag_id),
        # mirroring how ``for_project`` resolves project -> its client:
        # a tag scoped to any project of this client is visible too.
        client_projects = (
            (
                await session.execute(
                    select(ProjectProfile.tag_id).where(ProjectProfile.client_tag_id == for_client)
                )
            )
            .scalars()
            .all()
        )
        targets.update(client_projects)
    if targets:
        tlist = list(targets)
        in_scope = select(TagScope.tag_id).where(TagScope.target_tag_id.in_(tlist))
        # Generic tags ("Filter by tags" chips) are global by nature
        # (no TagScope rows), so without this they showed under ANY
        # focus — the 4x-reported leak. Under a focus a generic tag is
        # relevant only if it is explicitly scoped to the focus OR is
        # actually applied to an entity (task/note/memory blob) that
        # itself belongs to the focused client/project (i.e. carries a
        # focus structural tag in `tlist`). No focus -> this whole
        # block is skipped and everything shows (unchanged).
        focus_tasks = select(TaskTag.task_id).where(TaskTag.tag_id.in_(tlist))
        focus_notes = select(NoteTag.note_id).where(NoteTag.tag_id.in_(tlist))
        focus_blobs = select(MemoryBlobTag.blob_id).where(MemoryBlobTag.tag_id.in_(tlist))
        used_in_focus = (
            select(TaskTag.tag_id)
            .where(TaskTag.task_id.in_(focus_tasks))
            .union(
                select(NoteTag.tag_id).where(NoteTag.note_id.in_(focus_notes)),
                select(MemoryBlobTag.tag_id).where(MemoryBlobTag.blob_id.in_(focus_blobs)),
            )
        )
        generic_ok = or_(Tag.id.in_(in_scope), Tag.id.in_(used_in_focus))
        if manage:
            # Manager surface: a GLOBAL generic (no tag_scope rows at all)
            # must stay reachable under a focus, since the manager is
            # where its "Restrict to..." gets added. Filter surfaces keep
            # the stricter rule above (the leak fix).
            scoped_generics = select(TagScope.tag_id)
            generic_ok = or_(generic_ok, Tag.id.not_in(scoped_generics))
        # Client/project tags are INTRINSICALLY owned (a client tag, a
        # project tag belonging to a client) and almost never carry
        # TagScope rows, so the rule above would always show them. Under
        # a focus they must be constrained by ownership: only the
        # focused client itself and the project tags whose
        # ProjectProfile.client_tag_id is in the resolved targets (plus
        # any target id directly). Other clients' client/project tags
        # are hidden — this is the reported bug.
        owned_projects = select(ProjectProfile.tag_id).where(
            ProjectProfile.client_tag_id.in_(tlist)
        )
        structural = Tag.kind.in_([TagKind.client, TagKind.project])
        structural_ok = or_(Tag.id.in_(tlist), Tag.id.in_(owned_projects))
        stmt = stmt.where(
            or_(
                and_(structural, structural_ok),
                and_(structural.is_(False), generic_ok),
            )
        )
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
    # Explicit org filter on top of RLS (defense-in-depth, see list_tags).
    tag = (
        await session.execute(select(Tag).where(Tag.id == tag_id, Tag.org_id == org_id))
    ).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return tag


async def list_clients(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[tuple[Tag, ClientProfile]]:
    rows = await session.execute(
        select(Tag, ClientProfile)
        .join(ClientProfile, ClientProfile.tag_id == Tag.id)
        .where(Tag.kind == TagKind.client, Tag.org_id == org_id)
        .order_by(Tag.name)
    )
    return [(t, p) for t, p in rows.all()]


async def list_projects(
    session: AsyncSession, *, org_id: uuid.UUID
) -> list[tuple[Tag, ProjectProfile]]:
    rows = await session.execute(
        select(Tag, ProjectProfile)
        .join(ProjectProfile, ProjectProfile.tag_id == Tag.id)
        .where(Tag.kind == TagKind.project, Tag.org_id == org_id)
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
    # Widened from ``str | None`` (the historical shape: client text
    # fields) to accept the typed payment defaults (int for net-days,
    # bool for default_billable) introduced in migration 0080.
    fields: dict[str, object] | None = None,
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
    flds = fields or {}
    # Payment-method enums (FatturaPA TPxx/MPxx) and net-days range are
    # validated before they touch the row; the XML build never sees a
    # value outside the SdI table.
    if "default_payment_conditions_code" in flds:
        from flow_core.services.payment_methods import validate_condizioni as _vc

        flds["default_payment_conditions_code"] = _vc(flds["default_payment_conditions_code"])  # type: ignore[arg-type]
    if "default_payment_method_code" in flds:
        from flow_core.services.payment_methods import validate_modalita as _vm

        flds["default_payment_method_code"] = _vm(flds["default_payment_method_code"])  # type: ignore[arg-type]
    if "default_payment_terms_days" in flds:
        from flow_core.services.payment_methods import validate_terms_days as _vt

        flds["default_payment_terms_days"] = _vt(flds["default_payment_terms_days"])  # type: ignore[arg-type]
    for k, v in flds.items():
        setattr(prof, k, v)
    if "vat_number" in flds or "country_code" in flds:
        prof.country_code, prof.vat_number = normalize_vat(prof.vat_number, prof.country_code)
        if not is_valid_vat_code(prof.vat_number, prof.country_code):
            raise DomainError(
                MessageCode.INVOICE_INVALID, detail=f"client vat_number '{prof.vat_number}'"
            )
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
        (
            await session.execute(
                select(Tag).where(Tag.kind == kind, Tag.name == name, Tag.org_id == org_id)
            )
        )
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
# target channel by this key, never by a user-chosen name. The
# canonical channels are seeded idempotently per tenant; the platform
# admin may add custom (keyless or keyed) channels via /memory/channels.
# Enable/disable reuses the pre-existing tag ``status`` soft-state
# ('active' vs 'archived', the same one ``list_tags`` filters on); a
# disabled channel is not a valid write/search target. Seeded channels
# are renamable but their key is immutable and they are not deletable
# (disable instead).
_CHANNEL_ACTIVE = _TAG_ACTIVE
_CHANNEL_DISABLED = _TAG_ARCHIVED

# slug -> human (English) name. Order is the seed/list order. ``email``
# and ``telegram`` are seeded for determinism (a future ingest needs a
# stable, well-known target) but are NOT yet usable channels: their
# ingestion is not implemented, so ``_CONFIGURED_KEYS`` filters them out
# of the list/select responses until the integration exists. ``note``
# is the channel note-derived memory lands on (notes.transcribe writes
# with channel_key="note").
CANONICAL_MEMORY_CHANNELS: tuple[tuple[str, str], ...] = (
    ("email", "Email"),
    ("telegram", "Telegram"),
    ("manual", "Manual"),
    ("agent", "Agent"),
    ("note", "Note"),
    ("task", "Tasks"),
)
_CANONICAL_KEYS: frozenset[str] = frozenset(k for k, _ in CANONICAL_MEMORY_CHANNELS)

# Channels actually usable today. ``manual``/``agent``/``note``/``task``
# are intrinsically usable; ``email`` is now wired (task 2a901dee: synced
# messages ingest into this channel per-account). ``telegram`` stays
# seeded-but-hidden until its integration ships. A custom channel added by
# a platform admin is not in this set but IS configured (it was
# deliberately created), so the membership test is
# "system_key in _CONFIGURED_KEYS OR not a canonical (seeded) key".
# ``task`` is written by the task-search resync listener (one blob per
# task, rendered from title + description + checklist).
_CONFIGURED_KEYS: frozenset[str] = frozenset({"manual", "agent", "note", "task", "email"})

# Short English description per seeded channel (rendered read-only in
# the channel picker). Custom channels have no description (None).
_CHANNEL_DESCRIPTIONS: dict[str, str] = {
    "manual": "Written by you in the app",
    "agent": "Written by the assistant",
    "note": "Captured from your notes",
    "task": "Indexed from your tasks",
    "email": "Ingested from your synced email",
}


def channel_description(system_key: str | None) -> str | None:
    """Read-only description for a channel, keyed by ``system_key``;
    None for a keyless/custom channel (no canned copy)."""
    if system_key is None:
        return None
    return _CHANNEL_DESCRIPTIONS.get(system_key)


def _channel_configured(system_key: str | None) -> bool:
    """A channel is exposed in the list/select surface when it is one
    of the intrinsically-usable seeded keys, OR it is not a canonical
    (seeded) key at all -- i.e. a custom channel a platform admin
    deliberately created. The seeded-but-not-yet-implemented ``telegram``
    channel is filtered out."""
    if system_key in _CONFIGURED_KEYS:
        return True
    return system_key not in _CANONICAL_KEYS


async def ensure_default_memory_channels(session: AsyncSession, *, org_id: uuid.UUID) -> None:
    """Seed the canonical memory channels for a tenant, idempotent.

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
    canonical set first (so a fresh tenant is deterministic) then
    returns only the channels that are usable today: the intrinsically
    usable seeded keys (manual/agent/note) plus any custom channel a
    platform admin created. Seeded-but-not-yet-implemented channels
    (email/telegram) stay in the DB for a future ingest but are filtered
    out of this list/select response (``_channel_configured``)."""
    await ensure_default_memory_channels(session, org_id=org_id)
    rows = await session.execute(
        select(Tag).where(Tag.kind == TagKind.memory_channel).order_by(Tag.name)
    )
    return [t for t in rows.scalars().all() if _channel_configured(t.system_key)]


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


async def _is_default_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    tag_id: uuid.UUID,
    settings_key: str,
) -> bool:
    """The workspace default ``Personal``/``General`` tag is identified
    by ``organizations.settings[settings_key]`` (idempotent get-or-
    create cache). If the cache is missing — possible when the row was
    seeded by a prior path that skipped the pointer write — fall back to
    the natural-key match used by ``_ensure_default_tag``."""
    org = (
        await session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is not None and org.settings:
        cached = org.settings.get(settings_key)
        if cached is not None and str(cached) == str(tag_id):
            return True
    tag = (await session.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        return False
    fallback_name = {
        "default_client_tag_id": _DEFAULT_CLIENT_NAME,
        "default_project_tag_id": _DEFAULT_PROJECT_NAME,
    }.get(settings_key)
    return fallback_name is not None and tag.name == fallback_name


async def _purge_attachment_blobs_for(
    session: AsyncSession,
    *,
    task_ids: Sequence[uuid.UUID],
    note_ids: Sequence[uuid.UUID],
) -> None:
    """Drop the off-DB bytes of every attachment under the given task
    and note subgraphs BEFORE the DB rows get cascade-deleted, so an S3
    backend never orphans objects in the bucket. ``pg``-backend rows
    (bytes inline in ``attachments.data``) have ``storage_key IS NULL``
    and are skipped here (the bytes go with the row). Mirrors the order
    of ``services/attachments.delete_attachment``: object first, then
    row, so a store failure aborts the unit of work."""
    if not task_ids and not note_ids:
        return
    conds = []
    if task_ids:
        conds.append(Attachment.task_id.in_(task_ids))
    if note_ids:
        conds.append(Attachment.note_id.in_(note_ids))
    rows = await session.execute(
        select(Attachment.storage_key).where(
            or_(*conds),
            Attachment.storage_key.is_not(None),
        )
    )
    keys = [k for k in rows.scalars().all() if k is not None]
    if not keys:
        return
    store = get_attachment_store(get_settings())
    for key in keys:
        await store.delete(key)


async def _purge_project_subgraph(
    session: AsyncSession,
    *,
    project_tag_id: uuid.UUID,
) -> None:
    """Wipe everything semantically owned by a project (tag of kind
    ``project``): tasks reachable via ``task_tags`` + their CASCADE
    descendants (time entries, comments, attachments, schedules,
    dependencies, handoffs, dispatch requests, agent runs, reminders,
    recurrences, assignees), notes scoped via the project tag in
    ``note_tags`` (migration 0016) + their descendants (turns,
    note-tags, attachments — ``time_entries.note_id``
    is SET NULL by FK so any task-time loses only the back-link), memory
    blobs scoped via ``memory_blobs.project_id`` + their composite-FK
    descendants (blob_sources, memory_blob_tags), and events scoped via
    ``events.project_tag_id`` + their event participants.

    Off-DB attachment blobs are deleted from the store first. The
    project tag row itself is NOT deleted here (the caller does, after
    this returns, so the ProjectProfile CASCADE fires once)."""
    task_ids = list(
        (
            await session.execute(
                select(TaskTag.task_id)
                .join(Tag, Tag.id == TaskTag.tag_id)
                .where(TaskTag.tag_id == project_tag_id)
            )
        )
        .scalars()
        .all()
    )
    note_ids = list(
        (await session.execute(select(NoteTag.note_id).where(NoteTag.tag_id == project_tag_id)))
        .scalars()
        .all()
    )
    await _purge_attachment_blobs_for(session, task_ids=task_ids, note_ids=note_ids)
    if task_ids:
        await session.execute(delete(Task).where(Task.id.in_(task_ids)))
    if note_ids:
        await session.execute(delete(Note).where(Note.id.in_(note_ids)))
    await session.execute(delete(MemoryBlob).where(MemoryBlob.project_id == project_tag_id))
    # Migration 0097: legacy ``events`` table is gone; appointment-tasks
    # are tasks tagged with the project (cascaded through Task above).


async def purge_project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Hard-delete an archived project (kind=``project``) and its full
    subgraph. The project must be archived first (status='archived') so
    a destructive op is always a deliberate two-step (archive → delete);
    the workspace's default ``General`` project is never deletable (a
    workspace must always resolve a default project for orphan-task
    fallback, ``ensure_default_project``). Role: owner — this is
    irreversible and crosses many domains.

    Cascade is split into an explicit subgraph wipe (see
    ``_purge_project_subgraph``) followed by ``DELETE FROM tags WHERE
    id``, which lets the existing FK CASCADEs handle the satellites:
    ``project_profile.tag_id``, every ``task_tags``/``note_tags``/
    ``memory_blob_tags``/``tag_scopes`` row referencing this tag."""
    await require_role(session, org_id, actor_id, Role.owner)
    tag = await get_tag(session, org_id=org_id, tag_id=tag_id)
    if tag.kind is not TagKind.project:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    if tag.status != _TAG_ARCHIVED:
        raise DomainError(MessageCode.TAG_NOT_ARCHIVED, kind="project")
    if await _is_default_tag(
        session, org_id=org_id, tag_id=tag_id, settings_key="default_project_tag_id"
    ):
        raise DomainError(MessageCode.TAG_DEFAULT_PROTECTED, kind="project")
    await _purge_project_subgraph(session, project_tag_id=tag_id)
    await session.execute(delete(Tag).where(Tag.id == tag_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="purge_project",
    )


async def purge_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Hard-delete an archived client (kind=``client``) and everything
    transitively under it: every project linked via
    ``project_profile.client_tag_id`` is purged (subgraph + tag), every
    event scoped directly to the client (``events.client_tag_id``) is
    deleted, then the client tag itself is deleted (cascading the
    ``client_profile`` row).

    **Invoices are NOT cascade-deleted.** Invoices are fiscal records:
    if any row references this client we refuse with
    ``CLIENT_HAS_INVOICES`` instead of silently destroying them. The
    operator must reassign or delete the invoices first. Pre-conditions
    mirror ``purge_project``: status=archived, not the workspace default.
    Role: owner."""
    await require_role(session, org_id, actor_id, Role.owner)
    tag = await get_tag(session, org_id=org_id, tag_id=tag_id)
    if tag.kind is not TagKind.client:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    if tag.status != _TAG_ARCHIVED:
        raise DomainError(MessageCode.TAG_NOT_ARCHIVED, kind="client")
    if await _is_default_tag(
        session, org_id=org_id, tag_id=tag_id, settings_key="default_client_tag_id"
    ):
        raise DomainError(MessageCode.TAG_DEFAULT_PROTECTED, kind="client")
    invoice_count = (
        (await session.execute(select(Invoice.id).where(Invoice.client_tag_id == tag_id).limit(50)))
        .scalars()
        .all()
    )
    if invoice_count:
        raise DomainError(MessageCode.CLIENT_HAS_INVOICES, count=len(invoice_count))
    project_ids = list(
        (
            await session.execute(
                select(ProjectProfile.tag_id).where(ProjectProfile.client_tag_id == tag_id)
            )
        )
        .scalars()
        .all()
    )
    for project_tag_id in project_ids:
        await _purge_project_subgraph(session, project_tag_id=project_tag_id)
    if project_ids:
        await session.execute(delete(Tag).where(Tag.id.in_(project_ids)))
    # Migration 0097: legacy ``events.client_tag_id`` is gone; tasks
    # tagged with this client are already removed by the per-project
    # cascade above (task_tags points at the client tag too).
    await session.execute(delete(Tag).where(Tag.id == tag_id))
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="tag",
        entity_id=tag_id,
        action="purge_client",
        diff={"purged_projects": [str(p) for p in project_ids]},
    )


async def resolve_channel_by_key(
    session: AsyncSession, *, org_id: uuid.UUID, channel_key: str
) -> Tag:
    """Resolve a tenant's enabled ``memory_channel`` tag by its stable
    ``system_key`` (RLS scopes ``tags`` to the org, so a foreign org's
    channel is invisible -> not found). A disabled channel is treated
    as absent (not a valid write/search target).

    This is the deterministic resolution entry point integrations and
    note-derived memory write into, so it seeds the canonical channels
    first (idempotent, system action): a fresh tenant that never opened
    the channel list still resolves a canonical ``channel_key`` (e.g.
    ``note``) instead of a spurious CHANNEL_NOT_FOUND. Seeding here does
    NOT make email/telegram appear in the list -- that surface filters
    via ``_channel_configured`` -- it only guarantees the row exists for
    a key lookup."""
    if channel_key in _CANONICAL_KEYS:
        await ensure_default_memory_channels(session, org_id=org_id)
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
