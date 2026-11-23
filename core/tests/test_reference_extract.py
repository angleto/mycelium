"""What counts as a written reference (ADR-0073 decision 2), without a DB.

The corpus is shared with the web suite
(``web/src/lib/markdownSource/bodyReferences.agreement.test.ts``): this side
must produce exactly the listed references, the web side the chips and
mentions among them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mycelium_core.services.reference_extract import extract_references

_CORPUS = Path(__file__).resolve().parents[2] / "web/src/shared/fixtures/body_references.json"


def _cases() -> list[dict[str, Any]]:
    return list(json.loads(_CORPUS.read_text(encoding="utf-8"))["cases"])


def _as_rows(body: str) -> list[list[Any]]:
    return [
        [r.syntax, r.token, r.ordinal] + ([r.mention_kind] if r.syntax == "mention" else [])
        for r in extract_references(body)
    ]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_corpus(case: dict[str, Any]) -> None:
    assert _as_rows(case["body"]) == case["expected"]


def test_corpus_covers_every_syntax() -> None:
    syntaxes = {row[0] for case in _cases() for row in case["expected"]}
    assert syntaxes == {"mention", "chip", "bare"}


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_extraction_is_idempotent(case: dict[str, Any]) -> None:
    assert extract_references(case["body"]) == extract_references(case["body"])


def test_offsets_delimit_the_written_form() -> None:
    uid = "9cf6bc7f-1111-2222-3333-444455556666"
    body = f"A `91cf6aaa` B [label](@note:{uid}) C 91cf6aaa."
    spans = [body[r.start : r.end] for r in extract_references(body)]
    assert spans == ["`91cf6aaa`", f"[label](@note:{uid})", "91cf6aaa"]


def test_anchor_is_the_rendered_sentence() -> None:
    uid = "9cf6bc7f-1111-2222-3333-444455556666"
    body = f"First sentence. We cite [**the duct**](@note:{uid}) here. Last one."
    (ref,) = extract_references(body)
    assert ref.anchor is not None
    assert ref.anchor.exact == "the duct"
    assert ref.anchor.prefix == "We cite "
    assert ref.anchor.suffix == " here."


def test_anchor_stays_inside_its_block() -> None:
    body = "# Heading text\n\n`91cf6aaa` opens this paragraph.\n\n- next block"
    (ref,) = extract_references(body)
    assert ref.anchor is not None
    assert ref.anchor.prefix == ""
    assert ref.anchor.suffix == " opens this paragraph."


def test_editing_around_a_reference_keeps_its_identity() -> None:
    before = extract_references("x `91cf6aaa` y `91cf6aaa`")
    after = extract_references("new text first. x `91cf6aaa` y changed `91cf6aaa` z")
    assert [(r.syntax, r.token, r.ordinal) for r in before] == [
        (r.syntax, r.token, r.ordinal) for r in after
    ]
