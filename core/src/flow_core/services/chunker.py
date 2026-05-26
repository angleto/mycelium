"""Text chunker for multi-vector indexing of long documents.

A single embedding per long document suffers from averaging: the
vector blends every topic the doc covers and represents none of them
well. Splitting into paragraph-sized chunks (one embedding each)
trades storage for retrieval precision. Short texts (tasks, brief
notes) stay single-vector via the default ``WholeChunker``.

The factory ``pick_chunker(kind, length)`` picks the strategy
automatically per namespace + size; callers wanting a specific
strategy pass it explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Hard floor below which we never chunk: paragraph-split adds latency
# and storage for negligible recall gain on text that already fits one
# embedding (e5-small / bge-m3 truncate quietly past 512 / 8192 tokens).
_CHUNK_THRESHOLD_TOKENS = 800


def get_chunk_threshold_tokens() -> int:
    """Public read of the paragraph-split activation threshold (used by
    ``pick_chunker`` and by the rechunk admin path to identify legacy
    whole-doc notes long enough to benefit from re-indexing)."""
    return _CHUNK_THRESHOLD_TOKENS


def approx_tokens(text: str) -> int:
    """Public re-export of the cheap word-count tokenizer used by
    ``pick_chunker``. Same heuristic as ``_approx_tokens`` so the
    rechunk path and the write path agree on what counts as 'long'."""
    return _approx_tokens(text)


# Approximate token budget per chunk; the actual token count depends on
# the tokenizer (which the chunker doesn't load to stay cheap). Word
# counts approximate tokens within ~10-30% which is good enough for
# the boundary heuristic (the embedder still truncates the chunk at
# its real model context).
_CHUNK_MAX_WORDS = 400
# Words copied from the tail of the previous chunk into the head of
# the next: keeps cross-paragraph references retrievable. Lifted from
# the LangChain default (50 chars ~ 50 words at the granularity we
# care about).
_CHUNK_OVERLAP_WORDS = 50

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_WORD_COUNT = re.compile(r"\w+")


@dataclass(frozen=True)
class Chunk:
    """One fragment of the source text ready to be embedded. ``index``
    is the position in the document (0-based, used as the
    ``blob_sources.chunk_index`` natural key)."""

    text: str
    index: int


@runtime_checkable
class Chunker(Protocol):
    name: str

    def chunks(self, text: str) -> list[Chunk]: ...


@dataclass
class WholeChunker:
    """Single-vector: returns the input verbatim as chunk 0. Default
    for tasks and short notes."""

    name: str = "whole"

    def chunks(self, text: str) -> list[Chunk]:
        return [Chunk(text=text, index=0)]


@dataclass
class ParagraphChunker:
    """Paragraph-aware split with bounded chunk size + overlap.

    Strategy:
    1. Split on blank-line paragraph boundaries.
    2. Greedily concatenate small paragraphs up to ``max_words``.
    3. Split any paragraph longer than ``max_words`` on word boundaries
       into ``max_words``-sized pieces with ``overlap_words`` shared
       between successive pieces.

    Order is preserved (chunk N's index < chunk N+1's index). The
    output is never empty for a non-empty input (a single-paragraph
    short doc becomes one chunk, matching the whole-doc semantics).
    """

    name: str = "paragraph"
    max_words: int = _CHUNK_MAX_WORDS
    overlap_words: int = _CHUNK_OVERLAP_WORDS

    def chunks(self, text: str) -> list[Chunk]:
        if not text or not text.strip():
            return [Chunk(text=text, index=0)]
        paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
        # Pack small paragraphs together; oversize ones get sliding-window split.
        packed: list[str] = []
        buf: list[str] = []
        buf_words = 0
        for para in paragraphs:
            words = para.split()
            if len(words) > self.max_words:
                if buf:
                    packed.append("\n\n".join(buf))
                    buf = []
                    buf_words = 0
                packed.extend(self._window_split(words))
                continue
            if buf_words + len(words) > self.max_words:
                packed.append("\n\n".join(buf))
                buf = [para]
                buf_words = len(words)
            else:
                buf.append(para)
                buf_words += len(words)
        if buf:
            packed.append("\n\n".join(buf))
        if not packed:
            # Degenerate (e.g. text was only whitespace+separators):
            # fall back to single chunk of the raw input.
            return [Chunk(text=text, index=0)]
        return [Chunk(text=c, index=i) for i, c in enumerate(packed)]

    def _window_split(self, words: Sequence[str]) -> list[str]:
        out: list[str] = []
        step = max(1, self.max_words - self.overlap_words)
        i = 0
        while i < len(words):
            piece = list(words[i : i + self.max_words])
            out.append(" ".join(piece))
            if i + self.max_words >= len(words):
                break
            i += step
        return out


def pick_chunker(*, namespace: str, text: str) -> Chunker:
    """Strategy selector. Conservative by design: only ``namespace='note'``
    above the threshold gets paragraph-split. Tasks, channels, consolidated
    memory, agent output etc. stay single-vector. Callers that want a
    different recipe pass a Chunker instance directly."""
    if namespace != "note":
        return WholeChunker()
    tokens = _approx_tokens(text)
    if tokens < _CHUNK_THRESHOLD_TOKENS:
        return WholeChunker()
    return ParagraphChunker()


def _approx_tokens(text: str) -> int:
    """Cheap token approximation via word count. Within ~10-30% of the
    real tokenizer for IT/EN content, fine for the threshold decision."""
    return len(_WORD_COUNT.findall(text or ""))
