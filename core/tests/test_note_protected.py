"""Fase P of the search-informed graph (task 561c6aca): the ``protected``
facet and the "sorgente/ripristina" affordance.

- protected prose is never ``is_inert`` and never surfaces as a distill
  candidate;
- a cluster with a protected member counts only the unprotected archived
  members (mirroring ``extract_cluster_pattern``'s own eligibility, so the
  candidate signature matches what extraction would sign);
- ``distill_note`` refuses a protected source up-front;
- ``restore_source`` round-trip: the source is un-archived and the atom
  retired (soft-deleted, never hard-deleted -- the ``hypha_of`` chain stays
  queryable), for both a proposed and an approved atom; idempotent.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from mycelium_core.db import admin_session, tenant_session  # noqa: E402
from mycelium_core.errors import DomainError  # noqa: E402
from mycelium_core.i18n import MessageCode  # noqa: E402
from mycelium_core.models.garden_graph_snapshot import GardenGraphSnapshot  # noqa: E402
from mycelium_core.models.note import Note, NoteKind  # noqa: E402
from mycelium_core.models.note_link import NoteNoteLink  # noqa: E402
from mycelium_core.services import candidates as cand  # noqa: E402
from mycelium_core.services import decomposition, garden_review, note_inert  # noqa: E402
from mycelium_core.services import notes as nt  # noqa: E402
from mycelium_core.services.auth import signup  # noqa: E402


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="PROT")
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


async def test_protected_note_is_not_inert_and_not_a_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _inert_note(s, org, user, "prosa finita")
        assert await note_inert.is_inert(s, note=n) is True
        await nt.protect_note(
            s, org_id=org, actor_id=user, note_id=n.id, expected_version=n.version
        )
        await s.refresh(n)
        assert n.protected is True
        assert await note_inert.is_inert(s, note=n) is False
        out = await cand.list_distillation_candidates(s, org_id=org, kind="distill")
        assert all(str(n.id) not in c["note_ids"] for c in out["nodes"])
        # Release it: the candidate reappears (the user has the last word).
        # The toggle itself bumps ``updated_at`` (lifecycle.transition), which
        # restarts the quiet window -- recent user attention makes the note
        # non-inert for a while by design -- so age it again before asserting.
        await s.refresh(n)
        await nt.protect_note(
            s,
            org_id=org,
            actor_id=user,
            note_id=n.id,
            expected_version=n.version,
            protected=False,
        )
        await s.execute(
            text("UPDATE notes SET updated_at = :t WHERE id = :id"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "id": str(n.id)},
        )
        out = await cand.list_distillation_candidates(s, org_id=org, kind="distill")
        assert any(str(n.id) in c["note_ids"] for c in out["nodes"])


async def test_pattern_cluster_counts_only_unprotected_members() -> None:
    # A 3-member community with one protected member must surface a pattern
    # candidate over the 2 unprotected archived members ONLY -- the same set
    # (and therefore the same signature) extract_cluster_pattern would sign,
    # so the proposal stays actionable and de-duplicable.
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _inert_note(s, org, user, "cluster A")
        b = await _inert_note(s, org, user, "cluster B")
        p = await _inert_note(s, org, user, "cluster P (protetta)")
        await nt.protect_note(
            s, org_id=org, actor_id=user, note_id=p.id, expected_version=p.version
        )
        s.add(
            GardenGraphSnapshot(
                org_id=org,
                signature="sig-prot",
                centrality={},
                betweenness={},
                clusters={str(a.id): 0, str(b.id): 0, str(p.id): 0},
                modularity=None,
            )
        )
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="pattern")
        patterns = [c for c in out["nodes"] if c["kind"] == "pattern"]
        assert patterns, "expected a pattern candidate over the unprotected pair"
        ids = set(patterns[0]["note_ids"])
        assert ids == {str(a.id), str(b.id)}


async def test_pattern_below_two_without_protected_member_is_not_a_candidate() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await _inert_note(s, org, user, "solo archiviata")
        p = await _inert_note(s, org, user, "protetta")
        await nt.protect_note(
            s, org_id=org, actor_id=user, note_id=p.id, expected_version=p.version
        )
        s.add(
            GardenGraphSnapshot(
                org_id=org,
                signature="sig-prot2",
                centrality={},
                betweenness={},
                clusters={str(a.id): 0, str(p.id): 0},
                modularity=None,
            )
        )
        await s.flush()
        out = await cand.list_distillation_candidates(s, org_id=org, kind="pattern")
        assert not [c for c in out["nodes"] if c["kind"] == "pattern"]


async def test_distill_refuses_protected_source() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _inert_note(s, org, user, "prosa protetta")
        await nt.protect_note(
            s, org_id=org, actor_id=user, note_id=n.id, expected_version=n.version
        )
        with pytest.raises(DomainError) as exc:
            await decomposition.distill_note(
                s,
                org_id=org,
                actor_id=user,
                note_id=n.id,
                distilled_text="un atomo che non deve nascere",
                origin_model_id="test-model",
            )
        assert exc.value.code == MessageCode.NOTE_PROTECTED


async def _external_atom(s: object, org: uuid.UUID, user: uuid.UUID, source: Note) -> uuid.UUID:
    """Distill via the external path (born ``proposed``) and return the atom id."""
    res = await decomposition.distill_note(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        note_id=source.id,
        distilled_text=f"essenza di {source.title}",
        origin_model_id="test-model",
    )
    return res.distilled_note_id


async def test_restore_source_roundtrip_proposed_atom() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        src = await _inert_note(s, org, user, "sorgente conclusa")
        atom_id = await _external_atom(s, org, user, src)
        res = await garden_review.restore_source(s, org_id=org, actor_id=user, atom_note_id=atom_id)
        assert res.atom_retired is True
        assert src.id in res.restored_source_ids
        await s.refresh(src)
        assert src.is_archived is False
        atom = await s.get(Note, atom_id)
        assert atom is not None  # never hard-deleted
        assert atom.deleted_at is not None
        # The provenance chain (the stack) survives the retire.
        link = (
            await s.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.child_note_id == atom_id,
                    NoteNoteLink.kind == "hypha_of",
                )
            )
        ).scalar_one()
        assert link.parent_note_id == src.id
        # Idempotent: a second call retires nothing and revives nothing new.
        res2 = await garden_review.restore_source(
            s, org_id=org, actor_id=user, atom_note_id=atom_id
        )
        assert res2.atom_retired is False
        assert res2.restored_source_ids == []


async def test_restore_source_roundtrip_approved_atom() -> None:
    # An APPROVED (effective) atom has no direct reject verb: restore demotes
    # it back to 'proposed' (audited) and then rejects, landing on the exact
    # rejected-proposal end state (withheld from retrieval + hidden + fully
    # reversible).
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        src = await _inert_note(s, org, user, "sorgente approvata")
        atom_id = await _external_atom(s, org, user, src)
        await garden_review.approve_node(s, org_id=org, actor_id=user, note_id=atom_id)
        res = await garden_review.restore_source(s, org_id=org, actor_id=user, atom_note_id=atom_id)
        assert res.atom_retired is True
        atom = await s.get(Note, atom_id)
        assert atom is not None
        assert atom.deleted_at is not None
        assert atom.review_state == "proposed"  # demoted, then rejected
        await s.refresh(src)
        assert src.is_archived is False


async def test_restore_source_rejects_non_atom() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await _inert_note(s, org, user, "nota qualunque")
        with pytest.raises(DomainError):
            await garden_review.restore_source(s, org_id=org, actor_id=user, atom_note_id=n.id)
