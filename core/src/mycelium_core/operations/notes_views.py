"""The note's read and argument models (ADR-0068).

They were REST schemas in ``mycelium_api.schemas``; the class names are
unchanged, so the OpenAPI component names and the web client's generated
types keep theirs. The MCP tools that read a note serve the same models.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.note import NoteKind, NoteStatus
from mycelium_core.models.tag import TagKind


class TagBrief(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None


class NotePartOut(BaseModel):
    """One ordered markdown block of a note (task 71c9d670 Phase 2a).
    ``ui_collapsed`` is the caller's current collapse state for this
    part; missing/no row → ``false`` (default expanded). Populated
    on GET /notes/{id} only; bulk listings omit it to stay light.

    ``body_sha256`` is the digest the conditional-write gate accepts
    (``PATCH .../body/patch``, and the ``X-Body-SHA256`` header on the
    raw download). Served rather than left to the client so the two
    sides cannot hash byte-differently -- a trailing newline or a
    normalisation step is enough to make a correct client look like a
    drifted one."""

    id: uuid.UUID
    note_id: uuid.UUID
    ord: int
    title: str | None = None
    body: str
    body_sha256: str
    lang: str | None = None
    merged_from_note_id: uuid.UUID | None = None
    version: int
    ui_collapsed: bool = False
    # Who wrote the block and when: what lets a reader pick out the blocks
    # that arrived since it last looked, and tell its own from another
    # writer's, without reading a body. The MCP tools served these and the
    # REST route did not; one model now serves both.
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


class _NoteCommon(BaseModel):
    """Fields shared by the list and the single-note projections.

    Not a response model itself. It exists so ``NoteListOut`` is a
    strict subset of ``NoteOut`` by construction, rather than by two
    field lists that drift apart on the next added column.
    """

    id: uuid.UUID
    project_id: uuid.UUID | None
    # Set when the note is a task's "work note" (the SPA detects it to
    # open it from the task and bill its timer to the task).
    task_id: uuid.UUID | None = None
    # Title of the linked task (``task_id``), denormalized so the note's
    # "work note" banner can show *which* task time is billed to without
    # a second round-trip. Resolved regardless of the task's lifecycle
    # state (archived / soft-deleted), so the banner never blanks out for
    # a note linked to a closed task. None when the note has no task.
    task_title: str | None = None
    kind: NoteKind
    status: NoteStatus
    title: str | None
    summary: str | None
    audio_ref: str | None
    audio_seconds: int | None = None
    is_archived: bool = False
    # Fase P (task 561c6aca): finished prose the distiller never compacts.
    protected: bool = False
    deleted_at: datetime.datetime | None = None
    tags: list[TagBrief] = []
    version: int
    # docs/adr/0029 P1: garden lifecycle. ``maturity`` defaults to
    # ``seed`` (the migration backfilled every existing note). When
    # ``promoted_at`` is set the note is read-only at the service
    # layer (transplanted to a task).
    maturity: str = "seed"
    # Typed, unlike ``maturity`` above: the column is a native enum and
    # the SPA gets a union instead of a bare string, matching how
    # ``TaskOut.necessity`` is declared.
    index_scope: IndexScope = IndexScope.org
    promoted_at: datetime.datetime | None = None
    # Humus atom subtype (distillation/pattern/season); None for ordinary
    # notes. Lets the SPA show the "sorgente/ripristina" affordance
    # (Fase P) only on atoms.
    humus_kind: str | None = None
    # docs/adr/0029 P1: every task generated from this note
    # (kind ∈ {derived_from, promoted_from}), in emission order. The
    # SPA renders an "N tasks" chip on the note row from this list;
    # ``task_id`` above stays the SPA-canonical "primary".
    derived_task_ids: list[uuid.UUID] = Field(default_factory=list)
    # Task 1e07437e: total count of typed links into this note across
    # every kind (subject, artifact, derived_from, promoted_from), so
    # the SPA chip reflects all linked tasks -- not just the two
    # "fruit" kinds that ``derived_task_ids`` covers.
    linked_task_count: int = 0
    # Served by the MCP tools before the note operations moved to core:
    # when the note was made and last changed, and whether it is a
    # generated proposal still waiting for review (ADR-0043).
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    review_state: str | None = None


class NoteListOut(_NoteCommon):
    """The list projection: metadata plus a bounded one-line preview.

    Deliberately WITHOUT ``transcript``. A note body is unbounded, so
    serializing it per row makes ``GET /notes`` cost O(total content of
    the org) in bytes instead of O(rows shown) -- in production that
    was a multi-MB response for a screen that renders one line per
    note. Callers that need the body read ``GET /notes/{id}``; callers
    that need to MATCH on it pass ``q``, which filters server-side over
    part bodies (``services.notes.list_notes``) instead of shipping
    every body so the client can filter locally.
    """

    # First non-empty line of the body, capped server-side
    # (``services.notes._previews_by_note``). Enough for a row label or
    # a card subtitle, which is all the list surfaces ever did with the
    # full body. None when the note has no text yet.
    preview: str | None = None
    # The whole body, only when the caller asked for it
    # (``include_transcript``): a list row is unbounded with it, which is
    # why it is off by default and the ``preview`` above is the row label.
    transcript: str | None = None


class NoteOut(_NoteCommon):
    """The single-note projection: everything, body included."""

    transcript: str | None
    # Task 71c9d670 Phase 2a: the note's ordered markdown blocks
    # (note_part). Populated on GET /notes/{id} (single-note path);
    # the other single-note paths leave it empty to stay light. Empty
    # when no parts exist yet (a freshly created note, or one whose
    # transcript was never split).
    parts: list[NotePartOut] = Field(default_factory=list)
    # The parts without their bodies, set instead of ``parts`` when the
    # caller asked for the outline of a long note (``include_part_bodies``
    # false): enough to choose which part to read.
    outline: list[NotePartRow] | None = None


class NotePartRow(BaseModel):
    """One part as a list row: the full block, or its outline when the
    body was not asked for. In an outline ``body`` is null and ``head``
    is the first non-empty line; ``bytes`` (UTF-8 length) and
    ``body_sha256`` are always there, so a reader can tell a part changed
    without reading it."""

    id: uuid.UUID
    note_id: uuid.UUID
    ord: int
    title: str | None = None
    lang: str | None = None
    body: str | None = None
    head: str | None = None
    bytes: int
    body_sha256: str
    merged_from_note_id: uuid.UUID | None = None
    version: int
    ui_collapsed: bool = False
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


class NoteGetIn(BaseModel):
    note_id: uuid.UUID
    include_transcript: bool = Field(
        True,
        description=(
            "Also return the body as one string (the join of the part bodies). "
            "A reader of ``parts`` gets the note twice with it."
        ),
    )
    include_part_bodies: bool = Field(
        True,
        description=(
            "False returns ``outline`` (each part's id, title, size, first line "
            "and digest) instead of ``parts``: read a long note's shape first, "
            "then fetch the parts you need."
        ),
    )


class NotePartsIn(BaseModel):
    note_id: uuid.UUID
    include_body: bool = Field(
        True, description="False returns each part's outline instead of its body."
    )


class NotesListIn(BaseModel):
    """Which notes, newest first, one page at a time. Every filter ANDs."""

    q: str | None = Field(
        None,
        description=(
            "Free text over the whole corpus, matched server-side before paging: "
            "each word must appear in the title, a part, or a tag name."
        ),
    )
    ids: list[uuid.UUID] | None = Field(
        None, max_length=200, description="Only these notes (at most 200)."
    )
    project_id: uuid.UUID | None = None
    tag_id: uuid.UUID | None = None
    any_tag_ids: list[uuid.UUID] | None = Field(
        None,
        max_length=200,
        description=(
            "Only the notes carrying at least one of these tags: a focus on a client "
            "(its projects and the client tag) or on one project."
        ),
    )
    task_id: uuid.UUID | None = Field(None, description="Only the notes linked to this task.")
    link_kinds: list[str] | None = Field(
        None, description="With task_id: only these link kinds (subject, artifact, ...)."
    )
    maturity: str | None = Field(None, description="One lifecycle stage, e.g. dormant.")
    maturities: list[str] | None = None
    include_archived: bool = False
    include_deleted: bool = False
    only: Literal["deleted", "archived"] | None = Field(
        None,
        description=(
            "Only the notes in the bin (deleted) or only the archived ones; "
            "overrides the two include flags."
        ),
    )
    include_transcript: bool = Field(
        False, description="Also return each note's whole body (unbounded)."
    )
    created_after: str | None = Field(
        None, description="ISO instant or bare date (your timezone's day start)."
    )
    created_before: str | None = None
    updated_since: str | None = None
    order_by: Literal["created_at", "updated_at", "title"] | None = Field(
        None,
        description="Default newest first. Another order returns one page, no cursor.",
    )
    order_desc: bool = False
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = Field(
        None, description="The next_cursor of the previous page, for the next one."
    )


class NotesPage(BaseModel):
    """One page of notes. ``next_cursor`` is null on the last page."""

    items: list[NoteListOut]
    next_cursor: str | None = None
    truncated: bool = False


NoteOut.model_rebuild()
