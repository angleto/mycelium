"""The three markdown documents must offer the same verbs.

A note part, a task description and a comment are the same kind of
object wearing different names: an ordered/owned block of markdown with
a body, a version, an author and annotations hanging off it. The
``doc_kind`` handle already says so -- ``annotations`` addresses a
``note_part`` and a ``task_description`` through one column pair.

The surface kept drifting anyway, one verb at a time, and every gap read
as an arbitrary limitation rather than a decision: a comment could be
created and destroyed but not rewritten; the task description could be
appended to but never amended in place; only note parts had a restorable
delete. Each hole was invisible until someone hit it, because nothing
compared the three families.

This test is that comparison. It is deliberately a matrix over TOOL
NAMES rather than behaviour: the behaviour is covered by the per-family
suites, and what keeps rotting here is the surface. Adding a verb to one
family and not its siblings fails on the next run.
"""

from __future__ import annotations

import pytest

from mycelium_mcp.server import mcp as _registry
from mycelium_mcp.tool_scopes import TOOL_SCOPES


def _registry_names() -> set[str]:
    return {t.name for t in _registry._tool_manager.list_tools()}


# verb -> (note part, task description, task comment). ``None`` marks a
# verb that genuinely does not apply to that family; every other cell is
# a tool that must exist.
DOCUMENT_VERBS: dict[str, tuple[str | None, str | None, str | None]] = {
    "create": ("add_note_part", None, "add_comment"),
    "read one": ("get_note_part", "get_task", "get_comment"),
    "edit whole body": ("update_note_part", "update_task", "update_comment"),
    "append": (
        "append_note_part",
        "append_to_task_description",
        "append_to_comment",
    ),
    "prepend": (
        "prepend_note_part",
        "prepend_to_task_description",
        "prepend_to_comment",
    ),
    "anchored replace": (
        "replace_in_note_part",
        "replace_in_task_description",
        "replace_in_comment",
    ),
    "restorable delete": ("trash_note_part", "delete_task", "delete_comment"),
    "restore": ("restore_note_part", "restore_task", "restore_comment"),
    # Discovery of what was deleted: without it a restore is unreachable
    # for anyone who did not perform the delete (they have no id, and no
    # version to pass).
    "list deleted": (
        "list_trashed_note_parts",
        "list_tasks",
        "list_trashed_comments",
    ),
    # A body with no history is a body whose previous wording is simply
    # gone. Note parts ride their note's timeline; tasks and comments
    # have their own.
    "revision timeline": (
        "list_note_revisions",
        "list_task_revisions",
        "list_comment_revisions",
    ),
    "read one revision": (
        "get_note_revision",
        "get_task_revision",
        "get_comment_revision",
    ),
    "restore a revision": (
        "restore_note_revision",
        "restore_task_revision",
        "restore_comment_revision",
    ),
    # The irreversible one. Tasks have no hard entity delete at all (the
    # taxonomy review recorded that as deliberate), so the cell is empty
    # rather than pretending otherwise.
    "purge": ("delete_note_part", None, "purge_comment"),
}


@pytest.mark.parametrize("verb", sorted(DOCUMENT_VERBS))
def test_every_document_family_offers_the_verb(verb: str) -> None:
    registry = _registry_names()
    missing = [tool for tool in DOCUMENT_VERBS[verb] if tool is not None and tool not in registry]
    assert not missing, (
        f"'{verb}' is missing from the surface for: {', '.join(missing)}. "
        "A markdown document is a markdown document -- if one family gets a "
        "verb, its siblings need it too, or the gap reads as an arbitrary "
        "limitation to whoever hits it."
    )


@pytest.mark.parametrize("verb", sorted(DOCUMENT_VERBS))
def test_every_document_verb_is_scoped(verb: str) -> None:
    """Fail-closed: an unmapped tool is denied to every scoped assistant,
    so a missing scope entry is a silently-absent capability."""
    unmapped = [
        tool for tool in DOCUMENT_VERBS[verb] if tool is not None and tool not in TOOL_SCOPES
    ]
    assert not unmapped, f"'{verb}' tools missing a TOOL_SCOPES entry: {unmapped}"


def test_a_family_costs_one_write_key_for_its_whole_lifecycle() -> None:
    """Within a family, create / edit / replace / delete / restore all
    cost the SAME key. A caller allowed to destroy a comment but not to
    fix a typo in it -- which is what the split between ``comments:write``
    and ``annotations:write`` produced -- is an incoherent surface, not a
    tighter one."""
    families = {
        "note part": (
            "add_note_part",
            "update_note_part",
            "replace_in_note_part",
            "append_note_part",
            "prepend_note_part",
            "trash_note_part",
            "restore_note_part",
        ),
        "task description": (
            "update_task",
            "replace_in_task_description",
            "append_to_task_description",
            "prepend_to_task_description",
        ),
        "task comment": (
            "add_comment",
            "update_comment",
            "replace_in_comment",
            "append_to_comment",
            "prepend_to_comment",
            "delete_comment",
            "restore_comment",
        ),
    }
    for family, tools in families.items():
        keys = {TOOL_SCOPES[t] for t in tools}
        assert len(keys) == 1, f"{family} lifecycle is split across scopes: {keys}"


def test_reading_a_family_costs_one_read_key() -> None:
    for family, tools in {
        "note part": ("get_note_part", "list_note_parts", "list_trashed_note_parts"),
        "task comment": ("get_comment", "list_comments", "list_trashed_comments"),
    }.items():
        keys = {TOOL_SCOPES[t] for t in tools}
        assert len(keys) == 1, f"{family} reads are split across scopes: {keys}"


def test_the_irreversible_verb_is_fenced_and_the_reversible_one_is_not() -> None:
    """The rule the destructive-parity review wrote down: only ops that
    cannot be undone sit on a danger key. Its corollary -- that every
    destructive verb SHOULD have an undoable sibling on the write key --
    is what was missing for note parts."""
    assert TOOL_SCOPES["delete_note_part"] == "delete:notes"  # purge
    assert TOOL_SCOPES["purge_comment"] == "delete:comments"  # purge
    assert TOOL_SCOPES["trash_note_part"] == "notes:write"  # restorable
    assert TOOL_SCOPES["delete_note"] == "notes:write"  # soft
    assert TOOL_SCOPES["delete_task"] == "tasks:write"  # soft
    assert TOOL_SCOPES["delete_comment"] == "comments:write"  # soft


def test_every_purge_key_is_danger_and_off_by_default() -> None:
    """A purge destroys the entity AND its recovery history, so there is
    nothing left to restore from -- the exact criterion the
    destructive-parity review put on the danger tier."""
    from mycelium_core.mcp_scopes import DEFAULT_SCOPES, SCOPE_CATALOG

    category = {s.key: s.category for s in SCOPE_CATALOG}
    for key in ("delete:notes", "delete:tasks", "delete:comments"):
        assert category[key] == "danger", key
        assert key not in DEFAULT_SCOPES, key
