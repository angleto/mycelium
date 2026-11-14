"""The base URL and the proxy list refuse values that would disable the
protections the open OAuth strategies are gated on (``oauth.deployment``).

Each refused value used to start the process with DCR / metadata documents ON
and the per-origin bounds silently OFF (garbage proxy entry, catch-all CIDR)
or a forged issuer published (non-origin base URL)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mycelium_core.config import Settings
from mycelium_core.oauth.deployment import validate_public_base_url, validate_trusted_proxies
from mycelium_core.oauth.production import open_endpoints_allowed

_SECRETS = {"jwt_secret": "x" * 64, "secret_key": "y" * 64}


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **_SECRETS, **kw)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("MYCELIUM_PUBLIC_BASE_URL", "MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "value",
    [
        "not a url",
        'https://m.example"x',
        "http://evil.example",
        "http://mycelium.example:8080",
        "ftp://m.example",
        "https://",
        "https:///path",
        "m.example",
        "https://m.example/mcp",
        "https://m.example/?x=1",
        "https://m.example#frag",
        "https://m.example?",
        "https://user:pw@m.example",
        "https://m.example:99999",
        "https://m.example/\nx",
        "https://mÿ.example",
    ],
)
def test_a_base_url_that_is_not_an_https_origin_refuses_to_start(value: str) -> None:
    with pytest.raises(ValidationError, match="MYCELIUM_PUBLIC_BASE_URL"):
        _settings(public_base_url=value)


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("", ""),
        ("   ", ""),
        ("https://m.example\n", "https://m.example"),
        ("https://mycelium.xeno.garden", "https://mycelium.xeno.garden"),
        ("https://Mycelium.Example/", "https://mycelium.example"),
        ("https://m.example:8443", "https://m.example:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://app.localhost", "http://app.localhost"),
    ],
)
def test_an_origin_is_accepted_and_stored_normalised(value: str, stored: str) -> None:
    assert _settings(public_base_url=value).public_base_url == stored


@pytest.mark.parametrize(
    "proxies",
    [
        ["garbage"],
        [""],
        ["10.0.0.0/8", "garbage"],
        ["10.0.0.0/33"],
        ["0.0.0.0/0"],
        ["::/0"],
        ["10.0.0.0/8", "::/0"],
        ["0.0.0.0/1", "128.0.0.0/1"],
    ],
)
def test_a_proxy_list_that_does_not_parse_or_trusts_everyone_refuses_to_start(
    proxies: list[str],
) -> None:
    with pytest.raises(ValidationError, match="MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES"):
        _settings(issuer_key_trusted_proxies=proxies)


def test_the_refusal_also_holds_when_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES", '["0.0.0.0/0"]')
    with pytest.raises(ValidationError, match="trusts every address"):
        _settings()


def test_a_valid_proxy_list_is_stored_canonical() -> None:
    s = _settings(issuer_key_trusted_proxies=[" 10.1.2.3/8", "100.64.0.1", "fd00::/8"])
    assert s.issuer_key_trusted_proxies == ["10.0.0.0/8", "100.64.0.1/32", "fd00::/8"]


def test_both_valid_settings_still_open_the_strategies() -> None:
    s = _settings(public_base_url="https://m.example", issuer_key_trusted_proxies=["10.0.0.0/8"])
    assert open_endpoints_allowed(s)


def test_the_validators_are_the_ones_settings_use() -> None:
    assert validate_public_base_url("") == ""
    assert validate_trusted_proxies([]) == []
