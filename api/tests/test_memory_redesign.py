"""Memory redesign (DB-backed): graceful degradation when the optional
embedding model is missing, and memory channels as a tag kind.

The embedder is swapped through the ADR-0012 seam
(``set_embedder_override``). "Missing" is simulated with an override
whose ``embed`` raises (the same failure path as the missing
``sentence-transformers`` extra); ``/memory/status`` is additionally
exercised by clearing the override and stubbing ``find_spec`` so the
cheap probe reports unavailable without any override shortcut. The
override is always restored in teardown so other tests are unaffected
(mirrors test_f6_api.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from _fake_embedder import FakeEmbedder
from httpx import ASGITransport, AsyncClient

import flow_core.embedder as embedder_mod
from flow_api.main import app
from flow_core.embedder import EmbedResult, set_embedder_override


class _BrokenEmbedder:
    """Stands in for the missing optional model: instantiable, but
    ``embed`` raises exactly like LocalEmbedder without the extra."""

    model_id = "broken-embed"

    async def embed(self, text: str) -> EmbedResult:
        raise RuntimeError("LocalEmbedder requires the 'sentence-transformers' extra")


@pytest.fixture
def _fake_embedder() -> Iterator[None]:
    set_embedder_override(FakeEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


@pytest.fixture
def _broken_embedder() -> Iterator[None]:
    set_embedder_override(_BrokenEmbedder)
    try:
        yield
    finally:
        set_embedder_override(None)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _signup(c: AsyncClient) -> dict[str, str]:
    a = (
        await c.post(
            "/auth/signup",
            json={"email": _email(), "password": "pw-strong-123", "workspace_name": "A"},
        )
    ).json()
    return {"Authorization": f"Bearer {a['token']}", "X-Workspace-Id": a["workspace_id"]}


async def _grant_and_rate(c: AsyncClient, h: dict[str, str]) -> None:
    await c.post("/billing/grant", headers=h, json={"amount": "100"})
    await c.post(
        "/billing/rate-cards",
        headers=h,
        json={
            "model_id": FakeEmbedder.model_id,
            "provider": "local",
            "credits_per_input": "0.001",
        },
    )


async def test_embedder_missing_write_and_lexical_recall(_broken_embedder: None) -> None:
    """Embedder unavailable: write still 200 and persists a keyword-only
    blob (model_id == "none"); a search whose query word is in the text
    finds it via the lexical fallback. No 500, no rate card needed (no
    embedding cost is metered)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        # Deliberately NO billing grant / rate card: the keyword-only
        # path must not meter an embedding, so it must not require one.
        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "quarterly budget review for project alpha",
                "operation_id": "w-missing-1",
            },
        )
        assert w.status_code == 200, w.text
        body = w.json()
        assert body["model_id"] == "none"
        assert body["tier"] == "hot"
        blob_id = body["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={"project_id": proj, "query": "budget", "operation_id": "q-missing-1"},
        )
        assert found.status_code == 200, found.text
        hits = found.json()
        assert hits, "lexical fallback must return the blob"
        assert blob_id in {x["blob"]["id"] for x in hits}


async def test_embedder_present_semantic_recall(_fake_embedder: None) -> None:
    """Embedder available (fake): write + semantic recall returns the
    blob, and it is recorded with the real model id."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)
        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "the budget review for project alpha",
                "operation_id": "w-present-1",
            },
        )
        assert w.status_code == 200, w.text
        assert w.json()["model_id"] == FakeEmbedder.model_id
        blob_id = w.json()["id"]

        found = await c.post(
            "/memory/search",
            headers=h,
            json={"project_id": proj, "query": "budget alpha", "operation_id": "q-present-1"},
        )
        assert found.status_code == 200, found.text
        assert blob_id in {x["blob"]["id"] for x in found.json()}


async def test_memory_channel_filtering(_fake_embedder: None) -> None:
    """A memory_channel tag is created via the generic tag endpoint
    (member-level), folded into a blob on write, and used as an AND
    facet on search: the right channel matches, a different one does
    not, and a non-channel tag id is rejected with a 4xx code."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        await _grant_and_rate(c, h)

        ch = await c.post(
            "/tags", headers=h, json={"kind": "memory_channel", "name": "agent-scratch"}
        )
        assert ch.status_code == 200, ch.text
        ch_body = ch.json()
        assert ch_body["kind"] == "memory_channel"
        channel_id = ch_body["id"]

        other_ch = await c.post(
            "/tags", headers=h, json={"kind": "memory_channel", "name": "other-channel"}
        )
        assert other_ch.status_code == 200, other_ch.text
        other_channel_id = other_ch.json()["id"]

        # A generic tag id is NOT a valid channel.
        generic = await c.post("/tags", headers=h, json={"kind": "generic", "name": "g1"})
        assert generic.status_code == 200, generic.text
        generic_id = generic.json()["id"]

        proj = str(uuid.uuid4())
        w = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "channelled memo about onboarding",
                "operation_id": "w-ch-1",
                "channel_tag_id": channel_id,
            },
        )
        assert w.status_code == 200, w.text
        blob_id = w.json()["id"]
        assert channel_id in {t["id"] for t in w.json()["tags"]}

        same = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-1",
                "channel_tag_id": channel_id,
            },
        )
        assert same.status_code == 200, same.text
        assert blob_id in {x["blob"]["id"] for x in same.json()}

        different = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-2",
                "channel_tag_id": other_channel_id,
            },
        )
        assert different.status_code == 200, different.text
        assert different.json() == []

        bad = await c.post(
            "/memory/blobs",
            headers=h,
            json={
                "project_id": proj,
                "text": "x",
                "operation_id": "w-ch-bad",
                "channel_tag_id": generic_id,
            },
        )
        assert bad.status_code == 400, bad.text
        assert bad.json()["code"] == "tag.kind_mismatch"

        bad_search = await c.post(
            "/memory/search",
            headers=h,
            json={
                "project_id": proj,
                "query": "onboarding",
                "operation_id": "q-ch-bad",
                "channel_tag_id": generic_id,
            },
        )
        assert bad_search.status_code == 400, bad_search.text
        assert bad_search.json()["code"] == "tag.kind_mismatch"


async def test_memory_status_true_with_fake_override(_fake_embedder: None) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/memory/status", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"semantic": True}


async def test_memory_status_false_when_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override set + the optional package not importable: the cheap
    probe must report keyword-only. Override is force-cleared and the
    spec lookup stubbed; nothing here persists past the test."""
    set_embedder_override(None)
    monkeypatch.setattr(
        embedder_mod.importlib.util,
        "find_spec",
        lambda name: None if name == "sentence_transformers" else object(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        h = await _signup(c)
        r = await c.get("/memory/status", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {"semantic": False}
