"""The built-in link proposers (ADR-0073 decision 7), run by the garden loop.

Each one only calls :func:`link_proposals.submit`, the same entry an outside
proposer uses through the API, so the built-in ones get no privilege and are
measured on the same decisions.

``builtin:link_add``
    The existing ``link_add`` candidates (strong tag, co-activity, usage or
    written-reference evidence between two notes with no typed link), moved
    onto the seam: what used to be a list in a panel becomes a proposal a
    person decides, whose decisions now count.
``builtin:unlinked_mention``
    A note's title written in another note's text, as words, where no
    reference and no link joins the two. The gate is the title's document
    frequency in the workspace, not its length: a title that occurs in more
    than ``MENTION_MAX_DF_SHARE`` of the notes (and more than
    ``MENTION_MIN_DF_CAP``) is too common to be evidence of anything. Text is
    read in the rendered domain (code, links and URLs contribute their text
    only as the reader sees it). Sources at ``index_scope='none'`` are not
    read: a proposer is an automatic reader (ADR-0056).

Cost of the mention proposer: one pass over the rendered text of at most
``MENTION_SCAN_CAP`` notes, building the word n-grams of the lengths titles
actually have (at most ``MENTION_MAX_TITLE_WORDS``); memory is linear in the
words read. It is a batch job in the garden loop, never on a request.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.index_scope import IndexScope
from mycelium_core.models.note import Note
from mycelium_core.models.note_part import NotePart
from mycelium_core.services import candidates as candidates_svc
from mycelium_core.services import link_proposals as proposals
from mycelium_core.services.md_anchor import render_text
from mycelium_core.services.note_effective import effective_note_clause

LINK_ADD = "builtin:link_add"
UNLINKED_MENTION = "builtin:unlinked_mention"

LINK_ADD_PER_RUN = 20
MENTION_PER_RUN = 20
MENTION_SCAN_CAP = 5000
MENTION_MAX_TITLE_WORDS = 8
MENTION_MAX_DF_SHARE = 0.05
# Below about a hundred notes a 5% share is under five notes, and a concept
# cited in three notes of a small workspace is exactly what is worth
# proposing; so the cap never drops below this. The decisions measure the
# rest (precision per proposer).
MENTION_MIN_DF_CAP = 5
_QUOTE_CONTEXT = 80

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class RunReport:
    proposer: str
    submitted: int
    skipped: dict[str, int]


async def run_link_add(session: AsyncSession, *, org_id: uuid.UUID) -> RunReport:
    found = await candidates_svc.list_distillation_candidates(
        session, org_id=org_id, kind="link_add", limit=LINK_ADD_PER_RUN
    )
    submitted = 0
    skipped: dict[str, int] = defaultdict(int)
    for e in found["edges"]:
        res = await proposals.submit(
            session,
            org_id=org_id,
            proposer=LINK_ADD,
            source_note_id=uuid.UUID(str(e["src_note_id"])),
            target_note_id=uuid.UUID(str(e["dst_note_id"])),
            evidence={"weight": e["score"], "basis": e["reason"]},
            score=float(str(e["score"])),
        )
        if res.skipped:
            skipped[res.skipped] += 1
        else:
            submitted += 1
    return RunReport(LINK_ADD, submitted, dict(skipped))


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD_RE.findall(text)]


def _quote(rendered: str, title: str) -> str:
    idx = rendered.casefold().find(title.casefold())
    if idx < 0:
        return ""
    lo = max(0, idx - _QUOTE_CONTEXT)
    hi = min(len(rendered), idx + len(title) + _QUOTE_CONTEXT)
    return rendered[lo:hi].strip()


async def run_unlinked_mention(session: AsyncSession, *, org_id: uuid.UUID) -> RunReport:
    notes = (
        await session.execute(
            select(Note.id, Note.title, Note.index_scope)
            .where(Note.org_id == org_id, effective_note_clause())
            .order_by(Note.updated_at.desc())
            .limit(MENTION_SCAN_CAP)
        )
    ).all()
    titles: dict[uuid.UUID, tuple[str, ...]] = {}
    for nid, title, _scope in notes:
        words = tuple(_words(title or ""))
        if 0 < len(words) <= MENTION_MAX_TITLE_WORDS:
            titles[nid] = words
    readable = [nid for nid, _t, scope in notes if scope != IndexScope.none]
    if not titles or not readable:
        return RunReport(UNLINKED_MENTION, 0, {})
    lengths = {len(t) for t in titles.values()}
    part_rows = (
        await session.execute(
            select(NotePart.note_id, NotePart.id, NotePart.body)
            .where(NotePart.org_id == org_id, NotePart.note_id.in_(readable))
            .order_by(NotePart.note_id, NotePart.ord)
        )
    ).all()
    rendered_by_part: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    grams_by_note: dict[uuid.UUID, set[tuple[str, ...]]] = defaultdict(set)
    for note_id, part_id, body in part_rows:
        rendered = render_text(body or "")
        rendered_by_part[part_id] = (note_id, rendered)
        body_words = _words(rendered)
        for n in lengths:
            for i in range(len(body_words) - n + 1):
                grams_by_note[note_id].add(tuple(body_words[i : i + n]))
    holders: dict[tuple[str, ...], set[uuid.UUID]] = defaultdict(set)
    wanted = set(titles.values())
    for note_id, grams in grams_by_note.items():
        for g in grams & wanted:
            holders[g].add(note_id)
    cap = max(MENTION_MIN_DF_CAP, MENTION_MAX_DF_SHARE * len(notes))
    written = await _written_pairs(session, org_id=org_id)
    candidates: list[tuple[float, uuid.UUID, uuid.UUID, tuple[str, ...]]] = []
    skipped: dict[str, int] = defaultdict(int)
    for target, words in titles.items():
        citing = holders.get(words, set()) - {target}
        if not citing:
            continue
        if len(citing) > cap:
            skipped["too_common"] += 1
            continue
        for src in citing:
            if (src, target) in written:
                skipped["written"] += 1
                continue
            candidates.append((len(words) / len(citing), src, target, words))
    candidates.sort(key=lambda c: (-c[0], str(c[1]), str(c[2])))
    submitted = 0
    title_text = {nid: (t or "") for nid, t, _s in notes}
    for score, src, target, _words_t in candidates[:MENTION_PER_RUN]:
        part_id, quote = _first_quote(rendered_by_part, src, title_text[target])
        res = await proposals.submit(
            session,
            org_id=org_id,
            proposer=UNLINKED_MENTION,
            source_note_id=src,
            target_note_id=target,
            evidence={"title": title_text[target], "quote": quote},
            evidence_ref=f"note_part:{part_id}" if part_id else None,
            score=score,
        )
        if res.skipped:
            skipped[res.skipped] += 1
        else:
            submitted += 1
    return RunReport(UNLINKED_MENTION, submitted, dict(skipped))


def _first_quote(
    rendered_by_part: dict[uuid.UUID, tuple[uuid.UUID, str]], note_id: uuid.UUID, title: str
) -> tuple[uuid.UUID | None, str]:
    for part_id, (nid, rendered) in rendered_by_part.items():
        if nid == note_id:
            q = _quote(rendered, title)
            if q:
                return part_id, q
    return None, ""


async def _written_pairs(
    session: AsyncSession, *, org_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """(source note, target note) already joined by a written reference: a
    mention there is linked already, in the text."""
    rows = (
        await session.execute(
            select(NotePart.note_id, BodyReference.target_id)
            .select_from(BodyReference)
            .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
            .join(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
            .where(
                BodyReference.org_id == org_id,
                BodyReference.status == "resolved",
                BodyReference.target_kind == "note",
            )
        )
    ).all()
    return {(src, dst) for src, dst in rows if dst is not None}
