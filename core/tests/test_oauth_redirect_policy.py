"""The redirect-URI policy for OAuth clients, as a component (ADR-0069).

A code goes to whatever URI the policy accepts for the client asking, so
this one component decides where a credential can end up, for every client
strategy. Pure, so the hostile shapes are listed exhaustively here.

Observed failing before the component existed (2026-11-13): the module
``mycelium_core.oauth.redirects`` did not import.
"""

from __future__ import annotations

import pytest

from mycelium_core.oauth.redirects import (
    HARD_REFUSED_SCHEMES,
    RedirectPolicy,
    RedirectUriError,
    redirect_target,
    validate_scheme_allowlist,
)

DEFAULT = RedirectPolicy()
NATIVE = RedirectPolicy(private_use_schemes=frozenset({"cursor", "com.example.app"}))


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://host.example/cb?tenant=1",
        "http://127.0.0.1:33418/callback",
        "http://localhost:6274/oauth/callback",
        "http://[::1]:8080/cb",
        "http://127.0.0.1/cb",
    ],
)
def test_accepted_by_default(uri: str) -> None:
    assert DEFAULT.validate(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "http://host.example/cb",  # cleartext off loopback
        "http://localhost.evil.example/cb",  # looks like loopback, is not
        "http://127.0.0.1.evil.example/cb",
        "https://host.example/cb#x",
        "https://claude.ai@evil.example/cb",
        "https://user:pw@host.example/cb",
        "javascript:alert(1)",
        "data:text/html,hi",
        "cursor://anysphere.cursor-mcp/oauth/callback",  # private-use, not allowlisted
        "https:///cb",
        "https://host.example:99999/cb",
        "https://host.example/c b",
        "https://host.example/cb\n",
        "https://host.example/" + "a" * 2001,
        None,
        42,
    ],
)
def test_refused_by_default(uri: object) -> None:
    with pytest.raises(RedirectUriError):
        DEFAULT.validate(uri)


def test_private_use_schemes_only_through_the_allowlist() -> None:
    assert NATIVE.validate("cursor://anysphere.cursor-mcp/oauth/callback")
    assert NATIVE.validate("com.example.app:/oauth2redirect")
    for refused in (
        "vscode://vscode.github-authentication/did-authenticate",  # not listed
        "cursor://anysphere.cursor-mcp/cb#frag",
        "cursor://user@anysphere.cursor-mcp/cb",
        "javascript:alert(1)",
        "http://host.example/cb",
    ):
        with pytest.raises(RedirectUriError):
            NATIVE.validate(refused)


@pytest.mark.parametrize("scheme", sorted(HARD_REFUSED_SCHEMES | {"http", "https"}))
def test_the_hard_refusals_cannot_be_allowlisted(scheme: str) -> None:
    with pytest.raises(ValueError):
        validate_scheme_allowlist([scheme])


@pytest.mark.parametrize("scheme", ["", "1abc", "has space", "a/b", "x:"])
def test_allowlist_entries_must_be_schemes(scheme: str) -> None:
    with pytest.raises(ValueError):
        validate_scheme_allowlist([scheme])


def test_allowlist_is_normalised_to_lower_case() -> None:
    assert validate_scheme_allowlist(["Cursor", "com.example.App"]) == frozenset(
        {"cursor", "com.example.app"}
    )


def test_list_bounds() -> None:
    with pytest.raises(RedirectUriError):
        DEFAULT.validate_list([])
    with pytest.raises(RedirectUriError):
        DEFAULT.validate_list([f"https://h.example/{i}" for i in range(11)])
    with pytest.raises(RedirectUriError):
        DEFAULT.validate_list("https://h.example/cb")
    assert DEFAULT.validate_list(["https://h.example/a", "https://h.example/a"]) == [
        "https://h.example/a"
    ]


@pytest.mark.parametrize(
    ("registered", "presented", "ok"),
    [
        ("https://h.example/cb", "https://h.example/cb", True),
        ("https://h.example/cb", "https://h.example/cb/", False),
        ("https://h.example/cb", "https://H.example/cb", False),  # exact, no normalising
        ("https://h.example:443/cb", "https://h.example/cb", False),
        ("https://h.example/cb", "https://h.example:8443/cb", False),
        ("http://127.0.0.1:33418/cb", "http://127.0.0.1:50000/cb", True),
        ("http://127.0.0.1/cb", "http://127.0.0.1:50000/cb", True),
        ("http://localhost:1/cb", "http://localhost:2/cb", True),
        ("http://[::1]:1/cb", "http://[::1]:2/cb", True),
        ("http://127.0.0.1:1/cb", "http://localhost:1/cb", False),  # host still exact
        ("http://127.0.0.1:1/cb", "http://127.0.0.1:1/other", False),
        ("http://127.0.0.1:1/cb?a=1", "http://127.0.0.1:2/cb?a=2", False),
        ("http://127.0.0.1:1/cb", "https://127.0.0.1:1/cb", False),
        ("http://127.0.0.1:1/cb", "http://127.0.0.1:2/cb#f", False),
        ("http://127.0.0.1:1/cb", "http://u@127.0.0.1:2/cb", False),
        ("http://127.0.0.1:1/cb", "http://127.0.0.1:x/cb", False),
        ("cursor://a.b/cb", "cursor://a.b/cb", True),
        ("cursor://a.b/cb", "cursor://a.b:1/cb", False),  # port freedom is loopback http only
    ],
)
def test_matching(registered: str, presented: str, ok: bool) -> None:
    assert NATIVE.matches(registered, presented) is ok


def test_accepts_against_a_registration_or_any_valid_uri() -> None:
    assert DEFAULT.accepts(("https://h.example/cb",), "https://h.example/cb")
    assert not DEFAULT.accepts(("https://h.example/cb",), "https://h.example/other")
    # No registered list: any URI the policy accepts (the assistant-secret
    # clients, whose secret is what binds the code).
    assert DEFAULT.accepts(None, "https://anything.example/cb")
    assert not DEFAULT.accepts(None, "http://anything.example/cb")
    # A registered URI the policy no longer accepts is not honoured: removing
    # a scheme from the allowlist takes effect on clients already registered.
    assert not DEFAULT.accepts(("cursor://a.b/cb",), "cursor://a.b/cb")


def test_redirect_target_is_scheme_and_host() -> None:
    assert redirect_target("https://claude.ai/api/mcp/auth_callback") == "https://claude.ai"
    assert redirect_target("http://127.0.0.1:33418/cb") == "http://127.0.0.1:33418"
    assert redirect_target("http://[::1]:8/cb") == "http://[::1]:8"
    assert redirect_target("cursor://anysphere.cursor-mcp/cb") == "cursor://anysphere.cursor-mcp"
    assert redirect_target("com.example.app:/oauth2redirect") == "com.example.app:"
