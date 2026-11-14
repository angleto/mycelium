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
from mycelium_core.oauth.production import open_endpoints_allowed, startup_problems

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
        "https://m.example.",
        "https://m.example.:443",
        "https://m.example:0",
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
        ["::ffff:0:0/96"],
        ["::ffff:0.0.0.0/97", "128.0.0.0/1"],
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


def test_a_mapped_proxy_inside_one_ipv4_network_is_accepted() -> None:
    assert _settings(issuer_key_trusted_proxies=["::ffff:10.0.0.0/104"]).issuer_key_trusted_proxies


# ----- the opt-out applies only where the environment says "not production" ---


@pytest.mark.parametrize("env", ["dev", "test", "local", " Dev ", "LOCAL"])
def test_the_opt_out_is_honoured_in_a_development_environment(env: str) -> None:
    s = _settings(env=env, oauth_unattributed_origins_ok=True)
    assert open_endpoints_allowed(s)
    assert startup_problems(s) == []


@pytest.mark.parametrize("env", ["production", "prod", "prd", "staging", "", "  ", "development"])
def test_the_opt_out_is_ignored_anywhere_else(env: str) -> None:
    s = _settings(env=env, oauth_unattributed_origins_ok=True)
    assert not open_endpoints_allowed(s)
    problems = startup_problems(s)
    assert any("MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK is ignored" in p for p in problems)
    assert any("DISABLED" in p for p in problems)


def test_an_ignored_opt_out_does_not_block_a_configured_deployment() -> None:
    s = _settings(
        env="staging",
        oauth_unattributed_origins_ok=True,
        public_base_url="https://m.example",
        issuer_key_trusted_proxies=["10.0.0.0/8"],
    )
    assert open_endpoints_allowed(s)


def test_the_example_environment_file_ships_the_opt_out_off() -> None:
    import pathlib

    example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
    lines = [ln.strip() for ln in example.read_text().splitlines()]
    assert "MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK=false" in lines
