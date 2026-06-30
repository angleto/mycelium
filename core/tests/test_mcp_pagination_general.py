"""b7dde607 — the pagination contract generalized beyond list_tasks.

The {items, next_cursor, truncated} keyset envelope now backs list_notes /
list_dependencies / list_task_relations / list_annotations / list_turns, and
search() / memory_search() gain an ``offset`` (ranked retrieval has no stable
keyset). One shared cursor codec + envelope helper backs them all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from _fake_embedder import FakeEmbedder

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import billing
from mycelium_core.services.auth import signup
from mycelium_mcp.server import (
    add_comment,
    add_dependency,
    add_task_relation,
    create_note,
    create_task,
    list_annotations,
    list_dependencies,
    list_notes,
    list_task_relations,
    memory_search,
    memory_write,
    search,
)


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    from mycelium_core.embedder import set_embedder_override

    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


async def _signup(name: str) -> tuple[str, str, uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(
            s,
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            password="pw-strong-123",
            org_name=name,
        )
    assert r.token is not None
    return r.token, str(r.org_id), r.org_id, r.user_id


async def test_list_notes_keyset_pagination() -> None:
    token, org, _oid, _uid = await _signup("pg-notes")
    for i in range(5):
        await create_note(token=token, org_id=org, kind="text", text="x", title=f"note-{i}")

    full = await list_notes(token=token, org_id=org, limit=100)
    assert set(full) == {"items", "next_cursor", "truncated"}
    full_ids = [n["id"] for n in full["items"]]
    assert len(full_ids) == 5 and not full["truncated"] and full["next_cursor"] is None

    paged: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await list_notes(token=token, org_id=org, limit=2, cursor=cursor)
        paged.extend(n["id"] for n in page["items"])
        pages += 1
        if not page["truncated"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert cursor
    assert pages == 3
    assert paged == full_ids  # disjoint pages, no dupes/gaps, same order
    assert len(set(paged)) == 5


async def test_enveloped_lists_return_the_envelope_shape() -> None:
    token, org, _oid, _uid = await _signup("pg-shape")
    a = await create_task(token=token, org_id=org, title="ea")
    b = await create_task(token=token, org_id=org, title="eb")
    await add_dependency(
        token=token, org_id=org, predecessor_id=a["id"], successor_id=b["id"], type="FS"
    )
    await add_task_relation(token=token, org_id=org, task_id=a["id"], other_id=b["id"])
    await add_comment(token=token, org_id=org, task_id=a["id"], body="first")
    await add_comment(token=token, org_id=org, task_id=a["id"], body="second")

    _KEYS = {"items", "next_cursor", "truncated"}
    deps = await list_dependencies(token=token, org_id=org, limit=1)
    rels = await list_task_relations(token=token, org_id=org, limit=1)
    anns = await list_annotations(
        token=token, org_id=org, doc_kind="task_description", doc_id=a["id"], limit=1
    )
    assert set(deps) == _KEYS and set(rels) == _KEYS and set(anns) == _KEYS
    # The comment thread keyset-pages oldest-first across two pages.
    assert anns["truncated"] is True and anns["next_cursor"]
    page2 = await list_annotations(
        token=token,
        org_id=org,
        doc_kind="task_description",
        doc_id=a["id"],
        limit=1,
        cursor=anns["next_cursor"],
    )
    assert len(page2["items"]) == 1
    assert page2["items"][0]["id"] != anns["items"][0]["id"]


async def test_search_and_memory_offset(_fake_embedder: None) -> None:
    token, org, org_id, user_id = await _signup("pg-offset")
    async with tenant_session(org, str(user_id)) as s:
        await billing.grant_credits(s, org_id=org_id, actor_id=user_id, amount=Decimal(100))
        await billing.upsert_rate_card(
            s,
            org_id=org_id,
            actor_id=user_id,
            model_id=FakeEmbedder.model_id,
            provider="local",
            values={"credits_per_input": Decimal("0.001")},
        )
    # Distinct relevance to the query so the ranking is STRICT (each blob has
    # a different number of filler tokens -> a different cosine to "zeta"), no
    # ties: offset over ranked results is best-effort and only well-defined
    # when the ranking is stable. Pathologically-tied scores would let the
    # (non-deterministic) kNN tie order shuffle pages between calls.
    proj = str(uuid.uuid4())
    filler = ["", "aa", "aa bb", "aa bb cc", "aa bb cc dd"]
    for i, f in enumerate(filler):
        await memory_write(
            token=token, org_id=org, text=f"zeta {f}".strip(), operation_id=f"w{i}", project_id=proj
        )

    async def _mids(**kw: object) -> list[str]:
        r = await memory_search(token=token, org_id=org, query="zeta", project_id=proj, **kw)  # type: ignore[arg-type]
        return [h["blob"]["id"] for h in r["hits"]]

    full = await _mids(operation_id="qf", limit=5)
    assert len(full) == 5 and len(set(full)) == 5
    # offset pages are exact disjoint slices of the one ranked list.
    assert await _mids(operation_id="q0", limit=2) == full[:2]
    assert await _mids(operation_id="q1", limit=2, offset=2) == full[2:4]
    assert await _mids(operation_id="q2", limit=2, offset=4) == full[4:]

    # search() offset likewise slices its ranked hits disjointly.
    async def _sids(**kw: object) -> list[str]:
        r = await search(token=token, org_id=org, q="zeta", kinds=["blob"], project_id=proj, **kw)  # type: ignore[arg-type]
        return [h["blob_id"] for h in r["hits"]]

    sfull = await _sids(limit=5)
    assert sfull[:2] == await _sids(limit=2)
    assert sfull[2:4] == await _sids(limit=2, offset=2)
