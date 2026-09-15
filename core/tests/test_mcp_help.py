"""MCP ``help`` tool: the system's self-knowledge for agents.

It answers "how is Mycelium configured / what are its features" from the
maintained docs + a config reference DERIVED from the Settings model (so it
never drifts), with no hand-kept FAQ. No DB / workspace needed.
"""

from __future__ import annotations

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
    assert "help('configuration')" in idx["pointers"]["configuration"]


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
    said "docs are listed under 'doc_topics' -- call help('<topic>')". A
    pointer at an empty list is worse than none: it spends a call to learn
    the thing does not exist.

    Here the docs ARE present, so the promise must be made; the mirror case
    is asserted below against an empty index."""
    idx = mcp_help()
    assert idx["doc_topics"]
    assert "help('<topic>')" in idx["overview"]


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
    assert "help('<topic>')" not in idx["overview"]


def test_help_topic_by_filename() -> None:
    res = mcp_help("functional-requirements")
    assert res["topic"] == "functional-requirements"
    assert "FR-9" in res["content"]


def test_help_topic_by_keyword_content_search() -> None:
    # 'invoicing' is not a docs/ filename; the content fallback still finds the
    # most relevant document (natural: an agent can ask by concept).
    res = mcp_help("invoicing")
    assert "content" in res
    assert "invoic" in res["content"].lower()


def test_help_configuration_alias() -> None:
    res = mcp_help("configuration")
    assert res["topic"] == "configuration"
    assert any(r["env"] == "MYCELIUM_ISSUER_KEY_PEPPER" for r in res["config"])


def test_help_unknown_topic_lists_topics() -> None:
    res = mcp_help("zzz-not-a-real-topic-qqq")
    assert "error" in res
    assert res["doc_topics"]
