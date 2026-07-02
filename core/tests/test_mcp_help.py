"""MCP ``help`` tool: the system's self-knowledge for agents.

It answers "how is Mycelium configured / what are its features" from the
maintained docs + a config reference DERIVED from the Settings model (so it
never drifts), with no hand-kept FAQ. No DB / workspace needed.
"""

from __future__ import annotations

from mycelium_mcp.server import help as mcp_help


def test_help_index_config_derived_no_secret_leak() -> None:
    idx = mcp_help()
    assert idx["overview"]
    assert "functional-requirements" in idx["doc_topics"]
    envs = {r["env"]: r for r in idx["configuration"]}
    # A required secret is present but leaks NO default value.
    pepper = envs["MYCELIUM_ISSUER_KEY_PEPPER"]
    assert pepper["required"] is True
    assert pepper["default"] is None
    # A non-secret knob exposes its default (proves the reference is real).
    assert envs["MYCELIUM_ISSUER_KEY_ROTATION_GRACE_SECONDS"]["default"] == 0


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
