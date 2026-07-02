"""Distillation-candidate surfacing (task 4995a32f).

Asserts the "suggest, don't automate" read surface:
- an inert, not-yet-distilled note is a ``distill`` candidate;
- an already-distilled note is NOT;
- a Leiden community of >=2 inert notes is a ``pattern`` candidate, and
  an existing pattern signature is excluded;
- a quarter with an archived note is a ``season`` candidate, excluded once
  its signature exists;
- two tag-sharing unlinked notes are a ``link_add`` candidate;
- a ``related`` link with no shared tags / co-activity is a ``link_prune``
  candidate;
- the project perimeter is respected.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.models.garden_graph_snapshot import GardenGraphSnapshot  # noqa: E402
from mycelium_core.models.note import Note, NoteKind, NoteMaturity  # noqa: E402
from mycelium_core.models.note_link import NoteNoteLink  # noqa: E402
from mycelium_core.models.note_tag import NoteTag  # noqa: E402
from mycelium_core.models.tag import TagKind  # noqa: E402
from mycelium_core.services import candidates as cand  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services import taxonomy  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="CAND")
    return r.org_id, r.user_id


async def _inert_note(
    s: object, org: uuid.UUID, user: uuid.UUID, title: str, days: int = 30
) -> Note:
    """A note archived and aged past the quiet window -> inert / distillable."""
    n = await nt.create_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        kind=NoteKind.text,
        title=title,
        text=f"body of {title}, long enough to be a real note worth distilling",
    )
    n.is_archived = True
    await s.flush()  # type: ignore[attr-defined]
    await s.execute(  # type: ignore[attr-defined]
        text("UPDATE notes SET updated_at = :t WHERE id = :id"),
        {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=days), "id": str(n.id)},
    )
    await s.refresh(n)  # type: ignore[attr-defined]
    return n


async def test_inert_note_is_a_distill_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _inert_note(s, org, user, "una nota conclusa")
        out = await cand.list_distillation_candidates(s, org_id=org, kind="distill")
        distill = [c for c in out["nodes"] if c["kind"] == "distill"]
        assert any(str(n.id) in c["note_ids"] for c in distill)


async def test_already_distilled_note_is_excluded() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        src = await _inert_note(s, org, user, "già distillata")
        # Simulate an existing distillation without invoking the LLM: a
        # humus_kind='distillation' child joined by a hypha_of link.
        child = Note(
            org_id=org,
            kind=NoteKind.text.value,
            title="atom",
            humus_kind="distillation",
            humus_flag=True,
        )
        s.add(child)
        await s.flush()
        s.add(
            NoteNoteLink(
                org_id=org,
                parent_note_id=src.id,
                child_note_id=child.id,
                kind="hypha_of",
            )
        )
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="distill")
        assert all(str(src.id) not in c["note_ids"] for c in out["nodes"])


async def test_cluster_of_inert_notes_is_a_pattern_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _inert_note(s, org, user, "cluster A")
        b = await _inert_note(s, org, user, "cluster B")
        s.add(
            GardenGraphSnapshot(
                org_id=org,
                signature="sig",
                centrality={},
                betweenness={},
                clusters={str(a.id): 0, str(b.id): 0},
                modularity=None,
            )
        )
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="pattern")
        patterns = [c for c in out["nodes"] if c["kind"] == "pattern"]
        assert patterns, "expected a pattern candidate for the 2-note community"
        ids = set(patterns[0]["note_ids"])
        assert {str(a.id), str(b.id)} <= ids


async def test_pattern_excludes_dormant_non_archived_members() -> None:
    # Regression (candidates review 2026-07-02): extract_cluster_pattern
    # accepts ONLY archived sources and signs the sorted-then-truncated id
    # list. A community whose 2nd member is dormant-but-NOT-archived must not
    # surface as a pattern -- at extraction that member is dropped, shrinking
    # the set below 2 and shifting the signature, so a proposed pattern could
    # never be de-duplicated. Only archived members count toward the >=2 gate.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _inert_note(s, org, user, "archiviata A")  # archived
        d = await nt.create_note(
            s,
            org_id=org,
            actor_id=user,
            kind=NoteKind.text,
            title="dormiente D",
            text="body of the dormant, not-archived note, long enough",
        )
        d.maturity = NoteMaturity.dormant.value
        await s.flush()
        await s.execute(
            text("UPDATE notes SET updated_at = :t WHERE id = :id"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(d.id)},
        )
        await s.refresh(d)
        s.add(
            GardenGraphSnapshot(
                org_id=org,
                signature="sig2",
                centrality={},
                betweenness={},
                clusters={str(a.id): 0, str(d.id): 0},
                modularity=None,
            )
        )
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="pattern")
        patterns = [c for c in out["nodes"] if c["kind"] == "pattern"]
        # only ONE archived member in the community -> below the >=2 gate.
        assert not any(str(d.id) in c["note_ids"] for c in patterns)


async def test_season_candidate_and_signature_exclusion() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _inert_note(s, org, user, "nota di stagione")
        await s.refresh(n)
        year = n.created_at.year
        quarter = (n.created_at.month - 1) // 3 + 1
        out = await cand.list_distillation_candidates(s, org_id=org, kind="season")
        titles = [c["title"] for c in out["nodes"]]
        assert f"Season · {year} Q{quarter}" in titles
        # Now record the season humus for that window -> excluded.
        s.add(
            Note(
                org_id=org,
                kind=NoteKind.text.value,
                title="season atom",
                humus_kind="season",
                humus_signature=f"{year}Q{quarter}",
                humus_flag=True,
            )
        )
        await s.flush()
        out2 = await cand.list_distillation_candidates(s, org_id=org, kind="season")
        titles2 = [c["title"] for c in out2["nodes"]]
        assert f"Season · {year} Q{quarter}" not in titles2


async def test_tag_sharing_unlinked_notes_are_a_link_add_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="A", text="a body"
        )
        b = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="B", text="b body"
        )
        tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="shared-topic"
        )
        s.add(NoteTag(org_id=org, note_id=a.id, tag_id=tag.id))
        s.add(NoteTag(org_id=org, note_id=b.id, tag_id=tag.id))
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="link_add")
        pair = {str(a.id), str(b.id)}
        assert any(
            {e["src_note_id"], e["dst_note_id"]} == pair and e["op"] == "add" for e in out["edges"]
        )


async def test_related_link_with_no_basis_is_a_link_prune_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="A", text="a body"
        )
        b = await nt.create_note(
            s, org_id=org, actor_id=user, kind=NoteKind.text, title="B", text="b body"
        )
        # canonical pair order (parent < child by str) for the related edge
        p, c = sorted([a.id, b.id], key=str)
        s.add(NoteNoteLink(org_id=org, parent_note_id=p, child_note_id=c, kind="related"))
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="link_prune")
        pair = {str(a.id), str(b.id)}
        assert any(
            {e["src_note_id"], e["dst_note_id"]} == pair and e["op"] == "prune"
            for e in out["edges"]
        )


async def test_project_perimeter_is_respected() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        inside = await _inert_note(s, org, user, "dentro il progetto")
        await _inert_note(s, org, user, "fuori dal progetto")
        proj = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.project, name="Proj"
        )
        s.add(NoteTag(org_id=org, note_id=inside.id, tag_id=proj.id))
        await s.flush()
        out = await cand.list_distillation_candidates(
            s, org_id=org, project_id=proj.id, kind="distill"
        )
        ids = {i for c in out["nodes"] for i in c["note_ids"]}
        assert str(inside.id) in ids
        assert len(ids) == 1  # the outside note is excluded by the perimeter
