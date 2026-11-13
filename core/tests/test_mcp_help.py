"""MCP ``help`` tool: the system's self-knowledge for agents.

It answers "how is Mycelium configured / what are its features" from the
maintained docs + a config reference DERIVED from the Settings model (so it
never drifts), with no hand-kept FAQ. No DB / workspace needed.
"""

from __future__ import annotations

import json

from mycelium_mcp.server import help as mcp_help


def test_the_index_is_an_index_and_not_the_manual() -> None:
    """The default answer used to carry the whole derived environment
    reference: ~160 entries, most with a null description, on the one call
    the server's own instructions tell every client to make at bootstrap.
    That was the single largest fixed cost of a session, paid per agent per
    machine per session, for a reader that cannot set an environment
    variable anyway."""
    idx = mcp_help()
    assert idx["overview"]
    assert "functional-requirements" in idx["doc_topics"]
    assert "configuration" not in idx
    # And it SAYS where the reference went, so the economy costs nobody a
    # guess: a field that vanishes without a pointer is not cheaper, it is
    # just harder to find.
    # Pinned on the CONTENT of the pointer, not on its phrasing: the tool and
    # the topic that carries the reference. The phrasing moved on 2026-09-17,
    # when call syntax was dropped from every bootstrap payload -- over the
    # gateway 'help' is not a tool and a client read "help('configuration')"
    # as one.
    cfg = idx["pointers"]["configuration"]
    assert "help" in cfg and "configuration" in cfg


def test_the_config_reference_is_derived_and_leaks_no_secret() -> None:
    """Moved behind ``help('configuration')`` and unchanged in substance:
    derived from the Settings model, so it cannot drift from the code."""
    envs = {r["env"]: r for r in mcp_help("configuration")["config"]}
    # A required secret is present but leaks NO default value.
    pepper = envs["MYCELIUM_ISSUER_KEY_PEPPER"]
    assert pepper["required"] is True
    assert pepper["default"] is None
    # A non-secret knob exposes its default (proves the reference is real).
    assert envs["MYCELIUM_ISSUER_KEY_ROTATION_GRACE_SECONDS"]["default"] == 0


def test_the_overview_promises_a_drill_down_only_when_there_is_one() -> None:
    """In production ``doc_topics`` was EMPTY -- the runtime image never
    carried ``docs/`` forward from its builder stage -- while the overview
    said "docs are listed under 'doc_topics' -- call the 'help' tool". A
    pointer at an empty list is worse than none: it spends a call to learn
    the thing does not exist.

    Here the docs ARE present, so the promise must be made; the mirror case
    is asserted below against an empty index."""
    idx = mcp_help()
    assert idx["doc_topics"]
    assert "'help'" in idx["overview"] and "<topic>" in idx["overview"]


def test_without_docs_the_overview_says_so_instead_of_pointing(
    monkeypatch: object,
) -> None:
    """The half that could not be caught by reading: with no document set
    the payload must STATE the absence rather than leave an empty field for
    the reader to interpret."""
    from mycelium_mcp import server as srv

    monkeypatch.setattr(srv, "_docs_dir", lambda: None)  # type: ignore[attr-defined]
    idx = mcp_help()
    assert idx["doc_topics"] == []
    assert "not installed on this deployment" in idx["overview"]
    assert "<topic>" not in idx["overview"]


def test_a_short_document_comes_back_whole() -> None:
    res = mcp_help("architecture")
    assert res["topic"] == "architecture"
    assert "outline" not in res
    assert len(res["content"]) <= 6000


def test_a_long_document_comes_back_as_an_outline() -> None:
    """``functional-requirements`` is ~24k characters. Answering a topic with
    the whole of it cost ~6k tokens per lookup for a question one section
    answers; the outline lets the reader pick that section."""
    res = mcp_help("functional-requirements")
    assert res["topic"] == "functional-requirements"
    assert any("FR-9" in h for h in res["outline"])
    assert len(json.dumps(res)) < 8000


def test_one_section_by_heading_words() -> None:
    res = mcp_help("functional-requirements", section="FR-9")
    assert res["section"].startswith("FR-9")
    assert "invoic" in res["content"].lower()
    # The section, not the document: it ends before the next FR.
    assert "## FR-10" not in res["content"]


def test_an_adr_is_reachable_by_number() -> None:
    """The ADRs live under docs/adr/ and used to be unreachable: the index
    globbed the top level only, so the garden's own design records could not
    be read by the agents meant to operate the garden."""
    res = mcp_help("43")
    assert res["topic"].startswith("0043-")


def test_help_topic_by_keyword_returns_sections_not_documents() -> None:
    """A keyword that names no file used to return the whole document that
    repeated it most: help('memory') answered with all of mcp-coverage.md,
    ~32k characters. It now answers with the few sections it lands in."""
    for kw in ("invoicing", "memory", "humus"):
        res = mcp_help(kw)
        assert res["sections"], kw
        assert all(kw[:5] in s["content"].lower() for s in res["sections"]), kw
        assert len(json.dumps(res)) < 12000, kw


def test_a_hash_inside_a_code_fence_is_not_a_heading() -> None:
    from mycelium_mcp.docs_index import split_sections

    _, secs = split_sections("# Title\n\n```sh\n# a shell comment\n```\n\n## Real\nbody\n")
    assert [s.title for s in secs] == ["Title", "Real"]
    # A parent section carries its children.
    assert "## Real" in secs[0].text


def test_help_configuration_alias() -> None:
    res = mcp_help("configuration")
    assert res["topic"] == "configuration"
    assert any(r["env"] == "MYCELIUM_ISSUER_KEY_PEPPER" for r in res["config"])


def test_help_unknown_topic_lists_topics() -> None:
    res = mcp_help("zzz-not-a-real-topic-qqq")
    assert "error" in res
    assert res["doc_topics"]


def test_a_keyword_ranks_the_section_it_heads_first() -> None:
    """Raw counts favoured long sections that merely repeat a word: FR-9,
    the invoicing requirement, ranked third for 'invoicing'."""
    assert mcp_help("invoicing")["sections"][0]["section"].startswith("FR-9")
