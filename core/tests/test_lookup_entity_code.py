"""The rule that decides "this query is an id, answer it exactly".

Pure and DB-free: it is the gate in front of the whole unified search, so
its two failure modes both matter. Too loose and an ordinary word spelled
in hex ('cafe', 'decade') stops going through the normal pipeline. Too
tight and an id keeps being embedded, which is what returned five
confident, unrelated results for ``5d44d8e5``.
"""

from __future__ import annotations

import pytest

from mycelium_core.errors import DomainError
from mycelium_core.services.lookup import (
    IDENTIFIER_MIN_HEX,
    MAX_PREFIX_LEN,
    looks_like_entity_code,
    normalise_prefix,
)


@pytest.mark.parametrize(
    "raw",
    [
        "f62ff51d",  # the 8-char convention (ADR-0038)
        "5d44d8e5",
        "F62FF51D",  # case is normalised away
        "  f62ff51d  ",  # surrounding whitespace is trimmed
        "f62ff51d-a8dd",  # partial UUID, past the first dash
        "f62ff51d-a8dd-4eff-8a81-30c43434b787",  # full canonical UUID
        "deadbeef",  # a hex word long enough to be a code: exact is fine here
        # A git commit sha: 40 hex, LONGER than a UUID. It cannot resolve to
        # a mycelium id and it is still an identifier, so it must not be
        # embedded. Before 2026-09-13 the gate shared the resolver's 36-char
        # ceiling and let this through to the encoder, which answered a
        # meaningless token with ten arbitrary nearest neighbours.
        "620ac7b73fdf273eab74518365993b90ceecd0ce",
        "a" * 64,  # and a sha-256, for the same reason
    ],
)
def test_recognised_as_an_entity_code(raw: str) -> None:
    assert looks_like_entity_code(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "cafe",  # hex-spelled words shorter than the convention stay fuzzy
        "decade",
        "faced",
        "added",
        "f62ff51",  # 7 hex digits: below the convention
        "f62ff51d note",  # two tokens: a search, not a lookup
        "f62ff51d\tnota",
        "collatz",  # non-hex
        "f62ff51g",  # 'g' is not hex
        # Rejected for its non-hex characters (x, t, r), NOT for its length:
        # length no longer decides recognition.
        "f62ff51d-a8dd-4eff-8a81-30c43434b787-extra",
    ],
)
def test_not_an_entity_code(raw: str) -> None:
    assert looks_like_entity_code(raw) is False


def test_min_hex_is_above_the_resolver_minimum() -> None:
    """The search gate is deliberately stricter than ``normalise_prefix``.

    The resolver accepts 4 chars because a caller there has already SAID it
    is resolving a prefix. A search box has not: at 4 the gate would swallow
    ordinary words, so it starts at the 8-char convention."""
    from mycelium_core.services.lookup import MIN_PREFIX_LEN

    assert IDENTIFIER_MIN_HEX > MIN_PREFIX_LEN


def test_a_code_short_enough_to_be_a_uuid_prefix_resolves() -> None:
    """For everything up to a UUID's length the two agree, and the search
    path relies on it: it calls one straight after the other."""
    for raw in ("f62ff51d", "f62ff51d-a8dd", "a" * MAX_PREFIX_LEN):
        assert looks_like_entity_code(raw)
        assert normalise_prefix(raw) == raw.strip().lower()


def test_a_code_too_long_to_be_a_uuid_is_still_a_code() -> None:
    """This pair used to be asserted as one invariant ("everything
    recognised is resolvable"), and that coupling WAS the defect: it forced
    the recognition question to inherit the resolver's 36-char ceiling, so a
    git sha was handed to the encoder. They are two questions. Recognition
    says "do not embed this"; resolution says "this cannot be one of our
    ids". Both answers are correct for a commit sha, and the caller in
    ``task_search`` already handles the combination by returning no prefix
    match while leaving the verbatim branch to do its work."""
    sha = "620ac7b73fdf273eab74518365993b90ceecd0ce"
    assert looks_like_entity_code(sha)
    with pytest.raises(DomainError):
        normalise_prefix(sha)
