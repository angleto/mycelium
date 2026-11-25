"""The kinds of note link the store knows are the kinds the surface knows,
and the direction a person reads is the one the store means.

This test used to compare two hand-kept lists: the kinds the service
canonicalises (``models.note_link``) and the kinds the web panels rendered
as one neighbour list (``web/src/shared/noteLinks.ts``). The defect it was
written after was an undirected ``related`` edge visible from one of its
two notes only. Since ADR-0077 the web no longer decides direction: the
connections service returns each connection with its direction seen from
the item asked about, and the web shows it. So the agreement that matters
now is:

- the web's ``NoteLinkKind`` type names exactly the store's kinds, since
  the link proposals panel still offers them by name;
- the connections adapter reads exactly the undirected kinds as having no
  direction, and every other kind as a direction, from both ends.
"""

from __future__ import annotations

import pathlib
import re

from mycelium_core.models.note_link import (
    NOTE_NOTE_LINK_KINDS,
    NOTE_NOTE_LINK_UNDIRECTED_KINDS,
)
from mycelium_core.services.connections import Direction, note_notes

_SHARED = pathlib.Path(__file__).resolve().parents[2] / "web" / "src" / "shared" / "noteLinks.ts"


def test_the_web_type_names_the_same_four_kinds() -> None:
    source = _SHARED.read_text(encoding="utf-8")
    match = re.search(r"export type NoteLinkKind = ([^\n]*)", source)
    assert match is not None, f"NoteLinkKind not found in {_SHARED}"
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(NOTE_NOTE_LINK_KINDS)


def test_every_stored_kind_has_a_relation_and_only_the_undirected_lack_direction() -> None:
    """Every stored kind maps to a relation, and the adapter's rule for
    direction agrees with the store's rule for canonicalisation. Read from
    the adapter's own table, so a kind added to the store without a
    relation, or made undirected in one place only, fails here."""
    assert set(note_notes._RELATION) == set(NOTE_NOTE_LINK_KINDS)
    undirected = {k for k, r in note_notes._RELATION.items() if r.value == "related"}
    assert undirected == set(NOTE_NOTE_LINK_UNDIRECTED_KINDS)


def test_the_undirected_kinds_are_kinds() -> None:
    """A typo in the store would make the check above pass on a name no
    verb answers to, which reads as "nothing is undirected"."""
    assert NOTE_NOTE_LINK_UNDIRECTED_KINDS <= NOTE_NOTE_LINK_KINDS


def test_direction_is_not_left_to_the_reader() -> None:
    """The three values a connection can carry; ``none`` is the one the
    undirected kinds get (asserted end to end in ``test_connections``)."""
    assert {d.value for d in Direction} == {"out", "in", "none"}
