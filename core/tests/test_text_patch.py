"""Strict unified-diff applier: pure, no DB.

The oracle for the happy path is ``difflib.unified_diff`` (round-trip:
``apply(unified_diff(a, b)) == b``). difflib does NOT emit the
``\\ No newline at end of file`` marker, so the no-newline / CRLF cases use
hand-written diffs matching GNU/git output, and difflib-generated inputs
are kept newline-terminated (difflib's only well-formed regime).
"""

from __future__ import annotations

import hashlib
import random

import pytest

from mycelium_core.services.text_patch import (
    PatchBaseMismatch,
    PatchDoesNotApply,
    PatchMalformed,
    PatchTooLarge,
    apply_unified_diff,
    assert_base,
    body_sha256,
)


def _udiff(a: str, b: str) -> str:
    """A well-formed unified diff via difflib (inputs must be empty or
    newline-terminated, difflib's clean regime)."""
    import difflib

    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            lineterm="\n",
        )
    )


# --- round-trip happy path ----------------------------------------------

_ROUNDTRIP_CASES = [
    pytest.param("", "line1\nline2\nline3\n", id="empty_to_content"),
    pytest.param("line1\nline2\nline3\n", "", id="content_to_empty"),
    pytest.param("a\nb\nc\n", "a\nB\nc\n", id="single_line_change"),
    pytest.param("a\nb\nc\n", "a\nb\nc\nd\ne\n", id="additions_only"),
    pytest.param("a\nb\nc\nd\ne\n", "a\nc\ne\n", id="deletions_only"),
    pytest.param("a\nb\nc\n", "x\ny\nz\n", id="full_replace"),
    pytest.param(
        "h1\n\nbody paragraph\n\nh2\ntail\n",
        "h1\n\nbody paragraph edited\n\nh2 renamed\ntail\n",
        id="multi_paragraph_blank_lines",
    ),
    pytest.param("café\nnaïve\n", "café\nrésumé\n", id="unicode_latin"),
    pytest.param("行一\n行二\n", "行一\n行三\n🌲\n", id="unicode_cjk_emoji"),
    pytest.param(
        "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n",
        "1\nTWO\n3\n4\n5\n6\n7\n8\nNINE\n10\n",
        id="multi_hunk_offsets",
    ),
]


@pytest.mark.parametrize("a, b", _ROUNDTRIP_CASES)
def test_roundtrip_difflib(a: str, b: str) -> None:
    patch = _udiff(a, b)
    assert apply_unified_diff(a, patch) == b


def test_roundtrip_random_property() -> None:
    rng = random.Random(0xF107)  # noqa: S311 - test data, not cryptographic
    vocab = ["alpha", "beta", "gamma", "", "delta epsilon", "  indented", "ünïcode 行"]
    checked = 0
    for _ in range(500):
        a_lines = [rng.choice(vocab) for _ in range(rng.randint(0, 12))]
        b_lines = list(a_lines)
        for _ in range(rng.randint(1, 5)):
            op = rng.randint(0, 2)
            if op == 0 and b_lines:  # delete
                del b_lines[rng.randrange(len(b_lines))]
            elif op == 1:  # insert
                b_lines.insert(rng.randint(0, len(b_lines)), rng.choice(vocab))
            elif b_lines:  # mutate
                b_lines[rng.randrange(len(b_lines))] = rng.choice(vocab) + "!"
        a = "".join(line + "\n" for line in a_lines)
        b = "".join(line + "\n" for line in b_lines)
        patch = _udiff(a, b)
        if patch == "":  # a == b, difflib emits nothing; nothing to apply
            continue
        assert apply_unified_diff(a, patch) == b, (a, b, patch)
        checked += 1
    assert checked > 100  # the loop really exercised the applier


# --- strict matcher / atomicity -----------------------------------------


def test_tampered_context_raises() -> None:
    a, b = "a\nb\nc\n", "a\nB\nc\n"
    patch = _udiff(a, b).replace(" a\n", " X\n", 1)
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff(a, patch)


def test_tampered_removed_line_raises() -> None:
    a, b = "a\nb\nc\n", "a\nB\nc\n"
    patch = _udiff(a, b).replace("-b\n", "-DIFFERENT\n", 1)
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff(a, patch)


def test_two_hunk_partial_is_atomic() -> None:
    a = "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"
    b = "1\nTWO\n3\n4\n5\n6\n7\n8\nNINE\n10\n"
    patch = _udiff(a, b)
    # Break only the SECOND hunk's context; the first hunk would apply.
    patch = patch.replace(" 8\n", " EIGHT_BROKEN\n")
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff(a, patch)


def test_out_of_order_hunks_rejected() -> None:
    a = "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"
    # Two hunks; swapping them makes the second start before the first cursor.
    h1 = "@@ -1,3 +1,3 @@\n 1\n-2\n+TWO\n 3\n"
    h2 = "@@ -8,3 +8,3 @@\n 8\n-9\n+NINE\n 10\n"
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff(a, h2 + h1)


def test_hunk_past_eof_rejected() -> None:
    a = "a\nb\n"
    patch = "@@ -5,1 +5,1 @@\n-zzz\n+yyy\n"
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff(a, patch)


# --- header / malformed --------------------------------------------------


def test_git_diff_headers_stripped() -> None:
    a, b = "alpha\nbeta\n", "alpha\nBETA\n"
    patch = (
        "diff --git a/notes.md b/notes.md\n"
        "index 0123abc..4567def 100644\n"
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -1,2 +1,2 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
    )
    assert apply_unified_diff(a, patch) == b


def test_binary_marker_rejected() -> None:
    with pytest.raises(PatchMalformed):
        apply_unified_diff("x\n", "Binary files a/x and b/x differ\n")


def test_garbage_input_rejected() -> None:
    with pytest.raises(PatchMalformed):
        apply_unified_diff("x\n", "this is not a diff at all\n")


def test_bad_hunk_header_rejected() -> None:
    with pytest.raises(PatchMalformed):
        apply_unified_diff("x\n", "@@ -x,y +z @@\n-x\n+y\n")


def test_count_mismatch_rejected() -> None:
    # Header promises +3 new lines but only 2 are present.
    with pytest.raises(PatchMalformed):
        apply_unified_diff("", "@@ -0,0 +1,3 @@\n+a\n+b\n")


def test_truncated_hunk_rejected() -> None:
    # Header promises 2 old / 2 new but the body is cut short.
    with pytest.raises(PatchMalformed):
        apply_unified_diff("a\nb\n", "@@ -1,2 +1,2 @@\n a\n")


def test_empty_patch_rejected() -> None:
    with pytest.raises(PatchMalformed):
        apply_unified_diff("a\n", "")


def test_multi_file_patch_rejected() -> None:
    patch = (
        "--- a/one.md\n+++ b/one.md\n@@ -1 +1 @@\n-a\n+A\n"
        "--- a/two.md\n+++ b/two.md\n@@ -1 +1 @@\n-b\n+B\n"
    )
    with pytest.raises(PatchMalformed):
        apply_unified_diff("a\n", patch)


# --- newline / CRLF ------------------------------------------------------


def test_add_trailing_newline() -> None:
    patch = "@@ -1 +1 @@\n-last\n\\ No newline at end of file\n+last\n"
    assert apply_unified_diff("last", patch) == "last\n"


def test_remove_trailing_newline() -> None:
    patch = "@@ -1 +1 @@\n-last\n+last\n\\ No newline at end of file\n"
    assert apply_unified_diff("last\n", patch) == "last"


def test_no_newline_both_sides_preserved() -> None:
    patch = (
        "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n"
    )
    assert apply_unified_diff("a\nb", patch) == "a\nc"


def test_no_newline_marker_disagrees_with_base() -> None:
    # Marker claims old had no trailing newline, but the base does.
    patch = "@@ -1 +1 @@\n-last\n\\ No newline at end of file\n+last\n"
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff("last\n", patch)


def test_crlf_body_matches_crlf_diff() -> None:
    patch = "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+c\r\n"
    assert apply_unified_diff("a\r\nb\r\n", patch) == "a\r\nc\r\n"


def test_crlf_body_lf_diff_does_not_apply() -> None:
    patch = "@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    with pytest.raises(PatchDoesNotApply):
        apply_unified_diff("a\r\nb\r\n", patch)


# --- cap -----------------------------------------------------------------


def test_cap_enforced() -> None:
    a = ""
    b = "x" * 100 + "\n"
    patch = _udiff(a, b)
    with pytest.raises(PatchTooLarge):
        apply_unified_diff(a, patch, max_result_bytes=10)


def test_cap_allows_within_limit() -> None:
    a, b = "a\n", "ab\n"
    patch = _udiff(a, b)
    assert apply_unified_diff(a, patch, max_result_bytes=1024) == b


# --- base gate -----------------------------------------------------------


def test_body_sha256_matches_hashlib() -> None:
    s = "héllo 行\n"
    assert body_sha256(s) == hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_assert_base_ok() -> None:
    s = "content\n"
    assert_base(s, expected_sha256=body_sha256(s))  # no raise


def test_assert_base_mismatch() -> None:
    with pytest.raises(PatchBaseMismatch):
        assert_base("content\n", expected_sha256=body_sha256("other\n"))


def test_assert_base_case_insensitive_hex() -> None:
    s = "content\n"
    assert_base(s, expected_sha256=body_sha256(s).upper())  # no raise
