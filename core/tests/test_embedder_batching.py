"""Peak-memory bounding of the local embedder's batching (task 91c36656).

The production worker was OOMKilled in a loop while backfilling embeddings
for long note parts: ``embed_batch`` handed sentence-transformers a fixed
``batch_size=32`` regardless of text length, and 32 sequences at bge-m3's
8192-token window allocate multiple GB of attention activations. The fix
groups by an estimated token budget instead, so the peak is roughly flat
whatever the input shape. These tests pin the grouping arithmetic; the
encode itself needs the optional model extra and is not exercised here.
"""

from __future__ import annotations

from mycelium_core.embedder import estimate_tokens, group_by_token_budget

WINDOW = 2048
BUDGET = 16384


def test_short_texts_ride_in_one_group() -> None:
    texts = ["short title"] * 50
    groups = group_by_token_budget(texts, budget=BUDGET, window=WINDOW)
    assert len(groups) == 1
    assert sum(len(g) for g in groups) == 50


def test_long_texts_are_split_so_the_product_stays_under_budget() -> None:
    # ~2048 estimated tokens each (chars/4), i.e. the full window.
    long_text = "x" * (WINDOW * 4)
    groups = group_by_token_budget([long_text] * 24, budget=BUDGET, window=WINDOW)
    assert len(groups) > 1
    for g in groups:
        longest = max(estimate_tokens(t, window=WINDOW) for t in g)
        assert longest * len(g) <= BUDGET
    # 16384 / 2048 = 8 per group.
    assert max(len(g) for g in groups) == 8


def test_one_long_text_does_not_drag_a_group_of_short_ones() -> None:
    long_text = "x" * (WINDOW * 4)
    texts = ["tiny"] * 20 + [long_text]
    groups = group_by_token_budget(texts, budget=BUDGET, window=WINDOW)
    # The long one must not sit in a group whose padded cost blows the budget.
    for g in groups:
        longest = max(estimate_tokens(t, window=WINDOW) for t in g)
        assert longest * len(g) <= BUDGET


def test_a_single_oversized_text_still_gets_its_own_group() -> None:
    # Bigger than the whole budget on its own: it must be emitted, not dropped.
    huge = "x" * (BUDGET * 8)
    groups = group_by_token_budget([huge], budget=BUDGET, window=0)
    assert groups == [[huge]]


def test_every_text_is_emitted_exactly_once_and_in_order() -> None:
    texts = [f"t{i}" * (i * 100 + 1) for i in range(30)]
    groups = group_by_token_budget(texts, budget=BUDGET, window=WINDOW)
    assert [t for g in groups for t in g] == texts


def test_empty_input_yields_no_groups() -> None:
    assert group_by_token_budget([], budget=BUDGET, window=WINDOW) == []


def test_estimate_is_clamped_to_the_window() -> None:
    assert estimate_tokens("x" * 40_000, window=WINDOW) == WINDOW
    # window=0 means "the model's own default": no clamp.
    assert estimate_tokens("x" * 40_000, window=0) == 10_000
    assert estimate_tokens("", window=WINDOW) == 1


def test_budget_is_floored_at_one_so_grouping_always_terminates() -> None:
    groups = group_by_token_budget(["a", "b", "c"], budget=0, window=WINDOW)
    assert [t for g in groups for t in g] == ["a", "b", "c"]
