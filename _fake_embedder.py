"""Deterministic in-memory embedder for tests (ADR-0012 seam).

A stable hashed bag-of-words projected into the production dimension
and L2-normalized, so cosine similarity tracks token overlap without a
real model. Process-stable (hashlib, not the salted builtin hash).
"""

from __future__ import annotations

import hashlib
import math

from flow_core.config import get_settings
from flow_core.embedder import EmbedResult


def _hash_idx(token: str, dim: int) -> int:
    h = hashlib.md5(token.encode()).hexdigest()  # noqa: S324 (non-crypto use)
    return int(h, 16) % dim


class FakeEmbedder:
    model_id = "fake-embed"

    async def embed(self, text: str) -> EmbedResult:
        dim = get_settings().embed_dim
        vec = [0.0] * dim
        tokens = text.lower().split()
        for tok in tokens:
            vec[_hash_idx(tok, dim)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        else:
            vec[0] = 1.0
        return EmbedResult(vector=vec, model_id=self.model_id, tokens=max(1, len(tokens)))
