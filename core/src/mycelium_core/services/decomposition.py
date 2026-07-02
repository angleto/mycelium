"""Decomposizione fungina (task 4a718dc4, ADR-0034).

When a note is archived (``is_archived = true``), the decomposition
pipeline reads the note body, asks an LLM to extract the
*lessons / atoms / claims*, and writes the synthesis back as a new
``distillation`` note linked to the source. Both the source and
the synthesis flip ``humus_flag`` so the LLM walk (ADR-0034) can
surface them as fertiliser instead of fresh content.

Phase 1 surface: ``distill_note(note_id)``. The pattern-extraction
job (per-cluster) and the quarterly season synthesis are out of
scope for this commit; the schema and the LLM prompt are designed
so adding them is one helper + one cron entry.

The pipeline is idempotent on ``(source_note_id, kind="distillation")``:
re-running for an already-distilled note is a no-op (no second
distillation note is created). The caller decides when to trigger
(sync on archive, async via a worker queue, on-demand).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import LLMProvider
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.classification_feedback import ClassificationFeedback
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.services import audit, note_inert
from mycelium_core.services import notes as notes_svc
from mycelium_core.services.llm_resolver import resolve_llm
from mycelium_core.services.rbac import require_role

# Version tag for the inert-flip heuristic that decides a source becomes
# humus. Recorded on the feedback row so the learning loop can tell which
# policy produced the decision (the LLM model that distilled lives in the
# signals snapshot, since the flip is decided by ``is_inert``, not the LLM).
_HUMUS_MODEL_VERSION = "auto_humus_v1"
# Hard cap on caller-supplied external distillation text. The external path
# (below) skips the internal LLM and its implicit length bound, so without a
# cap a member token could write an unbounded blob. A distillation is a
# summary; 20k chars is generous headroom.
_MAX_EXTERNAL_DISTILL_CHARS = 20_000


def _review_state_for(autonomous: bool) -> str | None:
    """ADR-0043 review state for a freshly synthesised humus note.

    A summary the garden generates AUTONOMOUSLY (the unsolicited background
    sweep, ``autonomous=True``) is born ``'proposed'`` -- withheld from every
    retrieval surface until a human approves it -- but ONLY when the review
    gate is enabled. A USER-initiated synthesis (MCP / SPA / on-demand, the
    default ``autonomous=False``) is always effective (``None``), byte-
    identical to pre-ADR-0043 behaviour. ``origin_model_id`` is stamped on the
    note regardless of this function (transparency is unconditional). The
    per-model "earned autonomy" auto-approve policy (ADR-0043 D4) is a
    deferred follow-up; until it lands the gate is always approval-required.
    """
    if autonomous and get_settings().garden_review_gate_enabled:
        return "proposed"
    return None


_DISTILL_SYSTEM = (
    "You decompose a piece of finished thinking into reusable atoms. "
    "Read the note and reply with: (1) one-sentence lesson, (2) up to "
    "five concrete claims as bullet points, (3) up to three keywords. "
    # Fidelity grounding (task a44e72a4): the distillation must never add
    # information the source does not contain.
    "Ground every claim strictly in the source text: do not infer, "
    "generalise beyond it, or invent anything not explicitly stated; if a "
    "point is uncertain or unsupported by the source, omit it. "
    "No filler, no apology, no restate of the brief. "
    # Bench 2026-07-02 (task 50501e45): a soft mid-prompt "preserve the input
    # language" was IGNORED by the lean production candidates (mistral-small
    # and gemma-3 wrote English atoms for Italian notes); this closing
    # imperative is the phrasing the bench validated as effective. It is
    # deliberately language-AGNOSTIC (no IT/EN enumeration): a note in German,
    # Spanish, French or any other language gets its atom in that language.
    "Write the distillation in the same language as the note."
)
# Fidelity verify pass (task a44e72a4): a second model reads the SOURCE and
# the DRAFT distillation and returns a corrected distillation that keeps ONLY
# the claims the source supports. Conservative toward the source: dropping an
# invented claim is the goal; the source note itself is never touched, so no
# real information is lost.
_VERIFY_SYSTEM = (
    "You are a strict fact-checker for a distillation. You are given a SOURCE "
    "note and a DRAFT distillation derived from it. Return a corrected "
    "distillation that keeps ONLY the claims explicitly supported by the "
    "SOURCE; drop any claim that is inferred, generalised beyond, or not "
    "stated in the SOURCE. Keep the same structure and the input language. "
    "Output only the corrected distillation, nothing else."
)


@dataclass(frozen=True)
class DistillationResult:
    distilled_note_id: uuid.UUID
    model_id: str
    # False when an existing distillation was returned untouched
    # (idempotent re-run); True when a new distillation was generated.
    created: bool


async def _flip_source_to_humus(
    session: AsyncSession, *, note_id: uuid.UUID, expected_version: int
) -> bool:
    """Flip a source note's ``humus_flag`` to True under optimistic
    concurrency (WS-F3, §12).

    The UPDATE matches only when the version is still ``expected_version``
    AND the flag is still False, and it bumps the version like every other
    note mutation. So a concurrent edit/unarchive (version moved) or a
    double-distill (flag already set) is a no-op. Returns True when the row
    was flipped, False when it was skipped. Never raises: a race here is a
    deliberate skip, not a 409 -- the distillation must stand regardless.
    """
    row = (
        await session.execute(
            update(Note)
            .where(
                Note.id == note_id,
                Note.version == expected_version,
                Note.humus_flag.is_(False),
            )
            .values(humus_flag=True, version=Note.version + 1)
            .returning(Note.version)
        )
    ).first()
    return row is not None


async def _verify_against_source(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    source: str,
    draft: str,
    llm: LLMProvider | None,
) -> str:
    """Fidelity verify pass (task a44e72a4): a second metered LLM call that
    returns a corrected distillation keeping ONLY the claims ``source``
    supports. Falls back to ``draft`` when the verifier returns blank, so the
    distillation is never lost to an over-aggressive/empty check. Metered on a
    distinct deterministic ``operation_id`` so it is charged separately from
    the draft and never double-charges on a retry."""
    verifier = llm or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"distill_verify:{org_id}:{note_id}",
        op="distill_verify",
    )
    res = await verifier.complete(
        system=_VERIFY_SYSTEM,
        messages=[("user", f"SOURCE:\n{source}\n\nDRAFT:\n{draft}")],
    )
    return (res.text or "").strip() or draft


async def distill_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    llm: LLMProvider | None = None,
    autonomous: bool = False,
    distilled_text: str | None = None,
    origin_model_id: str | None = None,
) -> DistillationResult:
    """Read the source note's body, generate a distillation via the
    LLM provider, and persist it as a new note linked to the source.

    Idempotent: if a distillation note already derives from this source
    (a ``hypha_of`` link to a note marked ``humus_kind='distillation'``),
    the existing one is returned untouched.

    ``autonomous`` (ADR-0043): set True only by the unsolicited background
    sweep, so the distillation is born ``review_state='proposed'`` (gated)
    instead of effective. User-initiated callers leave it False (the default,
    byte-identical to pre-gate behaviour).

    ``distilled_text`` (the external path): a caller supplies the distillation
    from its own strong model. This content is UNVERIFIED, so it is hardened:
    length-capped (``_MAX_EXTERNAL_DISTILL_CHARS``), its provenance namespaced
    ``external:<origin_model_id>`` (no masquerading as an internal model), and
    born ``review_state='proposed'`` so it is withheld from retrieval until a
    human approves it -- closing the "any member injects live, falsely-attributed
    humus" hole.
    """
    await require_role(session, org_id, actor_id, Role.member)
    source = await notes_svc.get_note(session, org_id=org_id, note_id=note_id)
    # WS-F3: snapshot the source version BEFORE the slow LLM call, so the
    # humus flip at the end can be guarded with optimistic concurrency -- a
    # concurrent edit/unarchive in that window bumps the version and the
    # flip is skipped instead of mutating a note that turned live.
    source_version = source.version
    # Idempotency: look for an existing distillation derived from this
    # source (a hypha_of edge source -> a humus_kind='distillation' note).
    existing_row = (
        await session.execute(
            select(NoteNoteLink.child_note_id)
            .join(Note, Note.id == NoteNoteLink.child_note_id)
            .where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == note_id,
                NoteNoteLink.kind == "hypha_of",
                Note.humus_kind == "distillation",
            )
            .limit(1)
        )
    ).first()
    if existing_row is not None:
        # The previous distillation is still authoritative. The model
        # id is unknown from this row alone (audit log carries it);
        # callers shouldn't rely on this branch's model_id.
        return DistillationResult(
            distilled_note_id=existing_row[0], model_id="cached", created=False
        )
    body = await notes_svc.get_body(session, note_id=note_id)
    if not body or not body.strip():
        raise DomainError(MessageCode.DOMAIN_ERROR)
    external_text = distilled_text.strip() if distilled_text is not None else ""
    born_external = bool(external_text)
    if born_external:
        # dec45ebc: an external MCP caller (running its OWN strong model)
        # supplied the distillation text directly, SKIPPING the internal metered
        # LLM call and the verify pass. Because this content is unverified and
        # the model id is caller-asserted, it is HARDENED (adversarial audit
        # A-2): (1) length-capped so a member token cannot inject an unbounded
        # blob; (2) the provenance is namespaced ``external:<id>`` so it can
        # never masquerade as an internally-verified model; (3) born
        # ``review_state='proposed'`` below, so it is withheld from every
        # retrieval surface until a human approves it (it is NOT trusted just
        # because a member supplied it). Mycelium never observes the caller's
        # model spend (only the flat mcp_io gateway fee), so no ``distill:``
        # UsageRecord is produced on this path -- by design.
        final_text = external_text
        if len(final_text) > _MAX_EXTERNAL_DISTILL_CHARS:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        model_id = f"external:{(origin_model_id or 'unknown').strip()[:96] or 'unknown'}"
    else:
        # Route through the per-org METERED seam (WS-C3): an org on a hosted
        # provider (anthropic/scaleway/openai) gets ITS model and is charged,
        # instead of silently falling back to the local model for free -- the
        # bug this fixes, where the metering the docstrings/ADRs promise was
        # bypassed by a bare get_llm(). ``llm`` stays an explicit test/override
        # injection. The operation_id is deterministic so a retried distill
        # never double-charges (the idempotency guard above already prevents a
        # second LLM call once the humus atom exists).
        provider = llm or await resolve_llm(
            session,
            org_id,
            actor_id=actor_id,
            operation_id=f"distill:{org_id}:{note_id}",
            op="distill",
        )
        res = await provider.complete(
            system=_DISTILL_SYSTEM,
            messages=[("user", body)],
        )
        # Fidelity verify pass (task a44e72a4): keep only the claims the source
        # supports before persisting. Gated (cost-doubling second LLM call); off
        # by default leaves ``res.text`` as the draft, byte-identical.
        final_text = res.text
        if get_settings().distill_verify_pass_enabled:
            final_text = await _verify_against_source(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=note_id,
                source=body,
                draft=res.text,
                llm=llm,
            )
        model_id = res.model_id
    title = (source.title or "").strip()
    distill_title = f"Distillation · {title or 'untitled'}"[:300]
    # Migration 0016: the source's project lives in the junction;
    # carry it over so the distillation lands in the same project.
    source_project_id = await notes_svc.project_tag_for_note(session, note_id=source.id)
    distilled = await notes_svc.create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        title=distill_title,
        text=final_text,
        project_id=source_project_id,
    )
    distilled.humus_kind = "distillation"
    distilled.humus_flag = True
    # ADR-0043: the generating model on the artifact (transparency,
    # unconditional) + the review gate (proposed only for an autonomous,
    # gate-enabled run; NULL/effective otherwise).
    distilled.origin_model_id = model_id
    # Externally-supplied (unverified) humus is ALWAYS gated: it must pass the
    # ADR-0043 review before it can surface, regardless of ``autonomous`` or the
    # gate flag (adversarial audit A-2 -- it previously went live immediately).
    distilled.review_state = "proposed" if born_external else _review_state_for(autonomous)
    # Anti-mutation invariant (task 8a26c000): the source becomes humus
    # only if it is inert (archived/dormant, no open linked work, past the
    # quiet window). A live source -- one being actively worked -- is left
    # untouched; only the derived distillation node is created.
    #
    # WS-F3 (§12 concurrency): re-check inertia here, then flip under
    # optimistic concurrency. The is_inert re-check catches a linked task
    # reopening (which does not bump the note version); the version guard in
    # _flip_source_to_humus catches a concurrent edit/unarchive (which does).
    # On either race the flip is skipped -- the distillation still stands and
    # a now-live source is never mutated.
    flipped = await note_inert.is_inert(session, note=source) and await _flip_source_to_humus(
        session, note_id=source.id, expected_version=source_version
    )
    if flipped:
        # §12 "every mutation is tracked" (WS-F2): this is the one autonomous
        # note mutation that used to bypass _note_set entirely -- no revision,
        # no audit, no feedback. Trace it explicitly with an audit row
        # (action auto_humus) and an append-only classification_feedback row
        # (action 'auto', the system-initiated kind) so the flip is auditable
        # and replayable by the learning loop, exactly like auto_promote_mature.
        # The guarded UPDATE only fires when humus_flag was still False, so the
        # diff is always old=False; a live source leaves no trace.
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=source.id,
            action="auto_humus",
            diff={
                "humus_flag": {"old": False, "new": True},
                "distilled_note_id": str(distilled.id),
            },
        )
        session.add(
            ClassificationFeedback(
                org_id=org_id,
                user_id=actor_id,
                node_id=source.id,
                suggestion_type="humus",
                suggestion_value={"humus_flag": True},
                action="auto",
                model_version=_HUMUS_MODEL_VERSION,
                signals_snapshot={
                    "trigger": "distill",
                    "distilled_note_id": str(distilled.id),
                    "distill_model_id": model_id,
                },
            )
        )
    await session.flush()
    # Link: the distillation DERIVED FROM the source, so it is an
    # ordinary ``hypha_of`` (parent = source / origin, child = the new
    # distillation). The fact that it is humus lives in the node facet
    # (``humus_kind``), not in the link kind (ADR-0040): a 1:1
    # distillation keeps the thread to its single source so the lesson
    # can be decompressed back to the rich note it came from. We write
    # the row directly (the pipeline already gated on role above) rather
    # than via ``link_notes``.
    # ``created_by`` on note_note_link is an Identity FK, not a
    # raw user id — resolve the actor's Identity row in this org.
    identity_id = (
        await session.execute(
            select(Identity.id).where(
                Identity.org_id == org_id,
                Identity.user_id == actor_id,
            )
        )
    ).scalar_one_or_none()
    session.add(
        NoteNoteLink(
            org_id=org_id,
            parent_note_id=source.id,
            child_note_id=distilled.id,
            kind="hypha_of",
            created_by=identity_id,
        )
    )
    await session.flush()
    return DistillationResult(distilled_note_id=distilled.id, model_id=model_id, created=True)


# ── Phase 2: pattern extraction + season synthesis (e87daff4, ADR-0039) ──
#
# Beyond the 1:1 ``distill_note``, decomposition densifies the corpus with
# two N:1 syntheses, both PROPOSALS (a new humus note + ``hypha_of`` links
# back to every source), never mutations of live notes (§12). Idempotent on
# ``humus_signature``; metered through the same per-org seam as distill so an
# org on a strong summariser gets its quality and is charged. The (gated)
# autonomous scheduler that computes the clusters/quarters and calls these is
# a follow-up; on-demand callers (MCP, a future cron) drive them today.

_PATTERN_SYSTEM = (
    "You are a forester writing a retrospective across several finished, "
    "archived notes. Identify the recurring patterns, tensions and "
    "through-lines ACROSS them (not a summary of each). Reply with: (1) a "
    "one-line theme, (2) three to five patterns as bullets, each naming what "
    "recurs and where, (3) one open question the set leaves unresolved. No "
    "filler, no restating the brief. "
    "Write in the same language as the source notes."
)
_SEASON_SYSTEM = (
    "You are a forester writing a seasonal retrospective -- 'what I cultivated "
    "this season'. Given the season's archived notes, synthesise: (1) the "
    "season's headline, (2) three to five themes that grew, (3) what went "
    "dormant or was abandoned, (4) one seed to plant next season. No filler. "
    "Write in the same language as the source notes."
)

# Bounds so a synthesis stays one cheap LLM call regardless of corpus size.
_MAX_PATTERN_SOURCES = 20
_MAX_SEASON_SOURCES = 50
_PER_SOURCE_CHARS = 1500


@dataclass(frozen=True)
class HumusResult:
    note_id: uuid.UUID
    model_id: str
    # False when an existing synthesis was returned untouched (idempotent
    # re-run); True when a new humus note was generated.
    created: bool


async def _existing_humus(
    session: AsyncSession, *, org_id: uuid.UUID, kind: str, signature: str
) -> uuid.UUID | None:
    return (
        await session.execute(
            select(Note.id).where(
                Note.org_id == org_id,
                Note.humus_kind == kind,
                Note.humus_signature == signature,
                Note.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _assemble_bodies(session: AsyncSession, notes: list[Note]) -> str:
    """Title + head-of-body for each source, bounded per source, joined with
    separators -- the LLM input for a synthesis."""
    parts: list[str] = []
    for n in notes:
        body = await notes_svc.get_body(session, note_id=n.id)
        head = (body or "").strip()[:_PER_SOURCE_CHARS]
        title = (n.title or "").strip()
        parts.append(f"## {title or 'untitled'}\n{head}".strip())
    return "\n\n---\n\n".join(parts)


async def _synthesise_humus(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: str,
    op: str,
    signature: str,
    title: str,
    system_prompt: str,
    sources: list[Note],
    project_id: uuid.UUID | None,
    llm: LLMProvider | None,
    autonomous: bool,
) -> HumusResult:
    """Shared core for pattern/season: idempotency check, metered LLM call,
    create the humus note (``humus_kind``/``humus_flag``/``humus_signature``)
    and a ``hypha_of`` link from every source. Sources are read-only; no live
    note is mutated. ``autonomous`` (ADR-0043) decides the review gate (see
    ``_review_state_for``)."""
    existing = await _existing_humus(session, org_id=org_id, kind=kind, signature=signature)
    if existing is not None:
        return HumusResult(note_id=existing, model_id="cached", created=False)
    body = await _assemble_bodies(session, sources)
    if not body.strip():
        raise DomainError(MessageCode.DOMAIN_ERROR)
    provider = llm or await resolve_llm(
        session,
        org_id,
        actor_id=actor_id,
        operation_id=f"{op}:{org_id}:{signature}",
        op=op,
    )
    res = await provider.complete(system=system_prompt, messages=[("user", body)])
    note = await notes_svc.create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        title=title[:300],
        text=res.text,
        project_id=project_id,
    )
    note.humus_kind = kind
    note.humus_flag = True
    note.humus_signature = signature
    # ADR-0043: model provenance on the artifact + the review gate.
    note.origin_model_id = res.model_id
    note.review_state = _review_state_for(autonomous)
    await session.flush()
    identity_id = (
        await session.execute(
            select(Identity.id).where(Identity.org_id == org_id, Identity.user_id == actor_id)
        )
    ).scalar_one_or_none()
    # N:1 derivation: every source is a hypha_of parent of the synthesis, so
    # the walk can decompress the pattern/season back to the notes it grew
    # from (ADR-0040; the humus facet lives on the node, not the link kind).
    for src in sources:
        session.add(
            NoteNoteLink(
                org_id=org_id,
                parent_note_id=src.id,
                child_note_id=note.id,
                kind="hypha_of",
                created_by=identity_id,
            )
        )
    await session.flush()
    return HumusResult(note_id=note.id, model_id=res.model_id, created=True)


async def extract_cluster_pattern(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_note_ids: list[uuid.UUID],
    llm: LLMProvider | None = None,
    autonomous: bool = False,
) -> HumusResult:
    """Synthesise a ``pattern`` humus note over a set of ARCHIVED source notes
    (a Leiden cluster, a cross-cluster pick, a project window -- the caller
    chooses the grouping). Reads the sources, asks the per-org metered LLM for
    the through-lines, writes a new note linked back to each source. Idempotent
    on the source set; never mutates a live note (only archived sources count).

    ``autonomous`` (ADR-0043): True only for the unsolicited background sweep
    (the note is born ``review_state='proposed'``); user-initiated callers
    leave it False (effective immediately, as today).
    """
    await require_role(session, org_id, actor_id, Role.member)
    sources = list(
        (
            await session.execute(
                select(Note).where(
                    Note.id.in_(source_note_ids),
                    Note.org_id == org_id,
                    Note.deleted_at.is_(None),
                    Note.is_archived.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    sources.sort(key=lambda n: str(n.id))
    if len(sources) < 2:
        # A pattern is a cross-note retrospective; one (or zero) archived
        # source has no pattern to extract.
        raise DomainError(MessageCode.DOMAIN_ERROR)
    sources = sources[:_MAX_PATTERN_SOURCES]
    signature = hashlib.sha256(",".join(str(n.id) for n in sources).encode("utf-8")).hexdigest()[
        :32
    ]
    return await _synthesise_humus(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind="pattern",
        op="pattern",
        signature=signature,
        title=f"Pattern · {len(sources)} notes",
        system_prompt=_PATTERN_SYSTEM,
        sources=sources,
        project_id=None,  # a pattern may span projects
        llm=llm,
        autonomous=autonomous,
    )


async def synthesize_season(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    year: int,
    quarter: int,
    llm: LLMProvider | None = None,
    autonomous: bool = False,
) -> HumusResult:
    """Synthesise a ``season`` humus note for one quarter -- "what I cultivated
    this season" -- over the notes archived (created) in it. Idempotent per
    (org, year, quarter); a proposal, never a mutation of live notes.

    ``autonomous`` (ADR-0043): True only for the unsolicited background sweep
    (born ``review_state='proposed'``); user-initiated callers leave it False.
    """
    if quarter not in (1, 2, 3, 4):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    start = dt.datetime(year, 3 * (quarter - 1) + 1, 1, tzinfo=dt.UTC)
    end = (
        dt.datetime(year + 1, 1, 1, tzinfo=dt.UTC)
        if quarter == 4
        else dt.datetime(year, 3 * quarter + 1, 1, tzinfo=dt.UTC)
    )
    sources = list(
        (
            await session.execute(
                select(Note)
                .where(
                    Note.org_id == org_id,
                    Note.deleted_at.is_(None),
                    Note.is_archived.is_(True),
                    Note.created_at >= start,
                    Note.created_at < end,
                )
                .order_by(Note.created_at)
                .limit(_MAX_SEASON_SOURCES)
            )
        )
        .scalars()
        .all()
    )
    if not sources:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    return await _synthesise_humus(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind="season",
        op="season",
        signature=f"{year}Q{quarter}",
        title=f"Season · {year} Q{quarter}",
        system_prompt=_SEASON_SYSTEM,
        sources=sources,
        project_id=None,
        llm=llm,
        autonomous=autonomous,
    )


__all__ = [
    "DistillationResult",
    "HumusResult",
    "distill_note",
    "extract_cluster_pattern",
    "synthesize_season",
]
