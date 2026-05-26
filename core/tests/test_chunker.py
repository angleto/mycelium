"""Pure-function unit tests for chunker strategies + factory.

DB-bound write/retrieve paths (multi-chunk INSERT, dedupe at retrieve)
are exercised by the wider integration suite; these tests cover the
sync helpers that don't need a session.
"""

from __future__ import annotations

from flow_core.services.chunker import (
    Chunk,
    ParagraphChunker,
    WholeChunker,
    pick_chunker,
)


def test_whole_chunker_returns_single_chunk_with_input_text() -> None:
    text = "a short task title"
    out = WholeChunker().chunks(text)
    assert out == [Chunk(text=text, index=0)]


def test_whole_chunker_empty_input() -> None:
    out = WholeChunker().chunks("")
    assert len(out) == 1
    assert out[0].text == ""
    assert out[0].index == 0


def test_paragraph_chunker_splits_on_blank_lines() -> None:
    text = "Para one with some words.\n\nPara two has more content.\n\nPara three is here."
    out = ParagraphChunker(max_words=10, overlap_words=0).chunks(text)
    # Each paragraph is well under max_words individually but together
    # they exceed 10 -> packed into >1 chunk.
    assert len(out) >= 2
    # All chunks have monotonically increasing index starting at 0.
    assert [c.index for c in out] == list(range(len(out)))


def test_paragraph_chunker_packs_small_paragraphs() -> None:
    # 4 small paragraphs (each well under max_words) should pack
    # together up to the limit.
    text = "alpha bravo.\n\ncharlie delta.\n\necho foxtrot.\n\ngolf hotel."
    out = ParagraphChunker(max_words=20, overlap_words=0).chunks(text)
    # All 8 words fit in one chunk -> single packed chunk.
    assert len(out) == 1


def test_paragraph_chunker_window_split_oversize_paragraph() -> None:
    # Single paragraph with 30 words, max_words=10 with 2 overlap.
    words = [f"w{i}" for i in range(30)]
    text = " ".join(words)
    out = ParagraphChunker(max_words=10, overlap_words=2).chunks(text)
    # Step = 10 - 2 = 8. Pieces: [0..10), [8..18), [16..26), [24..30] -> 4 chunks.
    assert len(out) == 4
    # First chunk has 10 words.
    assert len(out[0].text.split()) == 10
    # Last chunk has remaining 6 words.
    assert len(out[-1].text.split()) == 6


def test_paragraph_chunker_preserves_paragraph_separator_in_packed() -> None:
    text = "first para.\n\nsecond para."
    out = ParagraphChunker(max_words=20, overlap_words=0).chunks(text)
    assert "\n\n" in out[0].text


def test_paragraph_chunker_whitespace_only_input_falls_back_to_single() -> None:
    out = ParagraphChunker(max_words=100, overlap_words=0).chunks("   \n\n  \n  ")
    assert len(out) == 1


def test_pick_chunker_short_text_returns_whole() -> None:
    out = pick_chunker(namespace="note", text="short note content")
    assert isinstance(out, WholeChunker)


def test_pick_chunker_long_note_returns_paragraph() -> None:
    long_text = " ".join(["word"] * 900)
    out = pick_chunker(namespace="note", text=long_text)
    assert isinstance(out, ParagraphChunker)


def test_pick_chunker_non_note_namespace_always_whole() -> None:
    long_text = " ".join(["word"] * 5000)
    # task / consolidated / agent / channel all stay single-vector
    # regardless of length (their content type / use case prefers it).
    for ns in ("task", "consolidated", "agent", "manual", "email"):
        out = pick_chunker(namespace=ns, text=long_text)
        assert isinstance(out, WholeChunker), f"namespace={ns} got non-Whole"
