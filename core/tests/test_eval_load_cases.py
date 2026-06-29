"""Unit tests for the file-driven gold loader (A7, WS-E1 follow-up).

Pure parsing -- no DB, no embedder. An external bench (a LongMemEval /
LOCOMO subset resolved to stored blob ids) is loaded here and run through
the SAME ``run_eval`` as the synthetic CI gate.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mycelium_core.services.eval_offline import GoldCase, load_cases


def test_load_cases_parses_jsonl(tmp_path: Path) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    p = tmp_path / "gold.jsonl"
    p.write_text(
        f'{{"query": "what is k8s", "expected_blob_ids": ["{a}"]}}\n'
        "\n"
        f'{{"query": "two targets", "expected_blob_ids": ["{a}", "{b}"]}}\n',
        encoding="utf-8",
    )
    assert load_cases(p) == [
        GoldCase(query="what is k8s", expected=frozenset({a})),
        GoldCase(query="two targets", expected=frozenset({a, b})),
    ]


def test_load_cases_skips_blank_lines(tmp_path: Path) -> None:
    a = uuid.uuid4()
    p = tmp_path / "g.jsonl"
    p.write_text(f'\n\n{{"query": "q", "expected_blob_ids": ["{a}"]}}\n\n', encoding="utf-8")
    assert len(load_cases(p)) == 1


@pytest.mark.parametrize(
    "line",
    [
        '{"query": "", "expected_blob_ids": ["x"]}',  # blank query
        '{"query": "q"}',  # missing ids
        '{"query": "q", "expected_blob_ids": []}',  # empty ids
        '{"query": "q", "expected_blob_ids": ["not-a-uuid"]}',  # unparseable id
        "{not valid json}",  # malformed json
    ],
)
def test_load_cases_rejects_bad_rows(tmp_path: Path, line: str) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(p)
