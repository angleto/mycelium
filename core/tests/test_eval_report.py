"""WS-EVAL reporting path (task d7c0693e): the report is the SINGLE
aggregation the article uses, so it is pinned byte-for-byte by a golden
file (deterministic: the bootstrap rng is seeded from the config)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_core.services.eval_report import build_report, load_config, load_records

_DATA = Path(__file__).parent / "data_eval_report"


def test_report_matches_golden() -> None:
    records = load_records(_DATA / "records.jsonl")
    config = load_config(_DATA / "config.json")
    rendered = build_report(records, config) + "\n"
    assert rendered == (_DATA / "golden.txt").read_text(encoding="utf-8")


def test_report_is_deterministic() -> None:
    records = load_records(_DATA / "records.jsonl")
    config = load_config(_DATA / "config.json")
    assert build_report(records, config) == build_report(records, config)


def test_loaders_fail_loudly(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no records"):
        load_records(empty)
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text('{"category": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed record"):
        load_records(malformed)


def test_load_config_requires_keys(tmp_path: Path) -> None:
    bad = tmp_path / "cfg.json"
    bad.write_text('{"k": 10}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required key"):
        load_config(bad)


def test_primary_without_records_raises(tmp_path: Path) -> None:
    cfg = load_config(_DATA / "config.json")
    cfg["primary"] = [
        {"name": "ghost", "category": "does-not-exist", "metric": "recall", "threshold": 0.5}
    ]
    records = load_records(_DATA / "records.jsonl")
    with pytest.raises(ValueError, match="no answerable records"):
        build_report(records, cfg)
