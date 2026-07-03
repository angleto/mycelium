"""Fase 1 evidence (task 561c6aca): bounded best-first vs full-org PPR.

Builds synthetic orgs of 100 / 1k / 10k notes with realistic structure
(preferential-attachment links, zipf-ish generic tags, co-activity pairs;
seeded RNG, deterministic), then measures per size:

- wall-clock of ``graph_local.bounded_neighborhood`` (the Fase 1 primitive,
  DB work O(budget)) vs ``graph.compute_personalized_pagerank`` (the focused
  ``graph_walk`` path, which loads the whole org);
- focus-set overlap (Jaccard of the top-N note sets) on the 100/1k orgs,
  bounded-vs-PPR quality parity.

Run against a DISPOSABLE database (creates orgs and notes; never prod):

    MYCELIUM_DATABASE_URL_SYNC=... MYCELIUM_DATABASE_URL=... \
        uv run python scripts/bench_bounded_walk.py [--sizes 100 1000 10000]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.note import Note, NoteKind
from mycelium_core.models.note_coactivity import NoteCoactivity
from mycelium_core.models.note_link import NoteNoteLink
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.services import graph, graph_local
from mycelium_core.services.auth import signup
from mycelium_core.services.graph import _pair_key

_KINDS = ["related", "related", "related", "hypha_of", "supersedes"]
_BUDGET = 24
_REPEATS = 5


async def _build_org(n_notes: int, rng: random.Random) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"bench-{uuid.uuid4().hex[:10]}@example.test",
            password=uuid.uuid4().hex,
            org_name=f"BW-{n_notes}",
        )
    org, user = r.org_id, r.user_id
    async with tenant_session(str(org), str(user)) as s:
        note_ids = [uuid.uuid4() for _ in range(n_notes)]
        for i, nid in enumerate(note_ids):
            s.add(Note(id=nid, org_id=org, kind=NoteKind.text, title=f"n{i}"))
        await s.flush()
        # Preferential-ish links: each note links to ~3 earlier notes, biased
        # to low indices so hubs emerge (what a real garden looks like).
        seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for i in range(1, n_notes):
            for _ in range(3):
                j = min(int(rng.expovariate(1 / (max(i, 2) / 4))), i - 1)
                pk = _pair_key(note_ids[i], note_ids[j])
                if note_ids[i] == note_ids[j] or pk in seen_pairs:
                    continue
                seen_pairs.add(pk)
                s.add(
                    NoteNoteLink(
                        org_id=org,
                        parent_note_id=note_ids[j],
                        child_note_id=note_ids[i],
                        kind=rng.choice(_KINDS),
                    )
                )
        # Zipf-ish generic tags, vocabulary scaling with the corpus (a real
        # garden grows tags with notes; a FIXED vocab makes every tag a hub
        # whose degree grows with N and turns the co-tag expansion into an
        # org-size scan -- exactly the artefact this bench must not fake).
        n_tags = max(30, n_notes // 25)
        tag_ids = []
        for t in range(n_tags):
            tid = uuid.uuid4()
            s.add(Tag(id=tid, org_id=org, kind=TagKind.generic, name=f"t{t}"))
            tag_ids.append(tid)
        await s.flush()
        for nid in note_ids:
            for tid in rng.sample(tag_ids, rng.randint(1, 3)):
                s.add(NoteTag(org_id=org, note_id=nid, tag_id=tid))
        # Co-activity on ~n random pairs.
        coact_seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for _ in range(n_notes):
            a, b = rng.sample(note_ids, 2)
            ka, kb = _pair_key(a, b)
            if (ka, kb) in coact_seen:
                continue
            coact_seen.add((ka, kb))
            s.add(
                NoteCoactivity(
                    org_id=org, note_a_id=ka, note_b_id=kb, session_count=rng.randint(1, 5)
                )
            )
        await s.flush()
    return org, user


async def _bench_size(n_notes: int, rng: random.Random) -> None:
    org, user = await _build_org(n_notes, rng)
    async with tenant_session(str(org), str(user)) as s:
        # Hub-ish seeds: the first notes accumulate links by construction.
        from sqlalchemy import select

        note_ids = [
            r[0]
            for r in (
                await s.execute(
                    select(Note.id).where(Note.org_id == org).order_by(Note.title).limit(20)
                )
            ).all()
        ]
        seeds = [note_ids[i] for i in (0, 3, 7, 11, 15)]

        t_bounded: list[float] = []
        t_ppr: list[float] = []
        jaccards: list[float] = []
        for seed in seeds[:_REPEATS]:
            t0 = time.perf_counter()
            hood = await graph_local.bounded_neighborhood(
                s, org_id=org, actor_id=user, seed_note_id=seed, node_budget=_BUDGET, tau=0.01
            )
            t_bounded.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            ranks = await graph.compute_personalized_pagerank(s, org_id=org, seed_ids=[seed])
            t_ppr.append(time.perf_counter() - t0)

            if n_notes <= 1000:
                ppr_top = {
                    nid
                    for nid, _ in sorted(
                        ((nid, m) for nid, m in ranks.items() if nid != seed and m > 0),
                        key=lambda kv: (-kv[1], str(kv[0])),
                    )[:_BUDGET]
                }
                bset = {n.note_id for n in hood.nodes}
                union = bset | ppr_top
                jaccards.append(len(bset & ppr_top) / len(union) if union else 1.0)

        def _med(xs: list[float]) -> float:
            return sorted(xs)[len(xs) // 2]

        overlap = f"{_med(jaccards):.2f}" if jaccards else "-"
        print(
            f"{n_notes:>6}  bounded {1000 * _med(t_bounded):8.1f} ms"
            f"  ppr {1000 * _med(t_ppr):8.1f} ms"
            f"  jaccard@{_BUDGET} {overlap}"
        )


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    args = ap.parse_args()
    rng = random.Random(42)  # noqa: S311 (deterministic synthetic data, not crypto)
    print(f"{'notes':>6}  {'bounded (median)':>20}  {'full PPR (median)':>16}  overlap")
    for n in args.sizes:
        await _bench_size(n, rng)


if __name__ == "__main__":
    asyncio.run(main())
