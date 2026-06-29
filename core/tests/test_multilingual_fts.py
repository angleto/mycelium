"""Per-row multilingual FTS (task b1baaf52, migration 0066).

The stemmed ``fts_lang`` column is generated in each row's OWN language
(``memory_blobs.fts_language``) instead of a hardcoded Italian
dictionary. Covers the task TEST PLAN: per-language stemming, the match
and snippet using the row's config, the unknown-language fallback to
``simple``, and detection at write.
"""

from __future__ import annotations

import uuid

from _fake_embedder import FakeEmbedder
from sqlalchemy import text as sa_text

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.services import memory as mem
from mycelium_core.services.auth import signup
from mycelium_core.services.fts_language import (
    FTS_SIMPLE,
    detect_fts_language,
    normalize_fts_language,
)

_FAKE = FakeEmbedder()


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="FTS")
    return r.org_id, r.user_id


async def _write(s, org, user, text_body, *, language=None, op):  # type: ignore[no-untyped-def]
    return await mem.write_blob(
        s,
        org_id=org,
        actor_id=user,
        project_id=None,
        text_body=text_body,
        operation_id=op,
        language=language,
        embedder=_FAKE,
    )


async def _fts_lang_text(s, blob_id: uuid.UUID) -> str:
    row = (
        await s.execute(
            sa_text("SELECT fts_lang::text AS v FROM memory_blobs WHERE id = :id"),
            {"id": blob_id},
        )
    ).one()
    return str(row.v)


async def _stem_matches(s, blob_id: uuid.UUID, query: str) -> bool:
    """True if the row's stemmed column matches the query in its OWN config."""
    row = (
        await s.execute(
            sa_text(
                "SELECT (fts_lang @@ plainto_tsquery(fts_language::regconfig, :q)) AS m"
                " FROM memory_blobs WHERE id = :id"
            ),
            {"id": blob_id, "q": query},
        )
    ).one()
    return bool(row.m)


# --------------------------------------------------------------- detector unit


def test_detect_picks_the_right_language() -> None:
    assert detect_fts_language("the mice are running on the old system") == "english"
    assert detect_fts_language("i topi corrono nella cucina di casa mia") == "italian"
    assert detect_fts_language("je mange une pomme dans la cuisine ce soir") == "french"
    assert detect_fts_language("el gato come pescado en la cocina de casa") == "spanish"


def test_detect_falls_back_to_simple_when_unsure() -> None:
    assert detect_fts_language("") == FTS_SIMPLE
    assert detect_fts_language(None) == FTS_SIMPLE
    assert detect_fts_language("ciao") == FTS_SIMPLE  # too short
    assert detect_fts_language("kubernetes pgvector hnsw") == FTS_SIMPLE  # no stopwords


def test_normalize_coerces_unknown_to_simple() -> None:
    assert normalize_fts_language("English") == "english"
    assert normalize_fts_language("italian") == "italian"
    assert normalize_fts_language("klingon") == FTS_SIMPLE
    assert normalize_fts_language(None) == FTS_SIMPLE


# ------------------------------------------------------------- generated column


async def test_each_row_stems_with_its_own_dictionary() -> None:
    """TEST PLAN #1: en 'running'->'run', it 'corrono'->'corr',
    fr 'manger'->'mang' -- each by its own dictionary, in one table."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        en = await _write(s, org, user, "the mice are running", language="english", op="en")
        it = await _write(s, org, user, "i gatti corrono veloci", language="italian", op="it")
        fr = await _write(s, org, user, "je mange une pomme", language="french", op="fr")

        assert "run" in await _fts_lang_text(s, en.id)
        assert "corr" in await _fts_lang_text(s, it.id)
        assert "mang" in await _fts_lang_text(s, fr.id)


async def test_match_is_per_language_no_cross_language_false_stem() -> None:
    """TEST PLAN #2: the English row matches an English-stemmed query;
    that same query does NOT spuriously stem-match the Italian row."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        en = await _write(s, org, user, "the cats are running fast", language="english", op="en")
        it = await _write(s, org, user, "i gatti corrono veloci", language="italian", op="it")

        # 'run' is the English stem of 'running' -> matches the English row
        # via its own dictionary even though the word 'run' is not verbatim.
        assert await _stem_matches(s, en.id, "run")
        # The Italian row, stemmed by the Italian dictionary, shares nothing
        # with the English query -- no cross-language false positive.
        assert not await _stem_matches(s, it.id, "run")
        # ...and symmetrically the Italian morphological query hits it.
        assert await _stem_matches(s, it.id, "correre")


async def test_unknown_language_falls_back_to_simple() -> None:
    """TEST PLAN #4: an unsupported language is stored as 'simple' (no
    stemming) and never raises on write or query."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        b = await _write(s, org, user, "the mice are running", language="klingon", op="k")
        assert b.fts_language == FTS_SIMPLE
        # 'simple' does not stem: 'running' is indexed verbatim, 'run' is not.
        fts = await _fts_lang_text(s, b.id)
        assert "running" in fts
        assert await _stem_matches(s, b.id, "running")
        assert not await _stem_matches(s, b.id, "run")


async def test_language_detected_when_not_supplied() -> None:
    """No explicit language -> detected from the text at write."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        b = await _write(
            s, org, user, "the project is on track and we will ship it next week", op="auto"
        )
        assert b.fts_language == "english"


async def test_ts_headline_highlights_in_row_language() -> None:
    """TEST PLAN #3: the snippet highlights a STEMMED hit in the row's
    own language, not just a verbatim one."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        en = await _write(
            s, org, user, "the mice are running across the floor", language="english", op="en"
        )
        snippets = await mem._ts_headlines(s, blob_ids=[en.id], query="run")
        snip = snippets[en.id]
        # 'run' stems to match 'running', which ts_headline wraps in <b>.
        assert "<b>running</b>" in snip
