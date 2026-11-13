"""Findings of the second independent security review of ADR-0069, as unit tests.

The reviewer's probes (2026-11-13) turned around: each asserts the safe
behaviour. Most were observed failing against the tree the review read; the
content-encoding cases already held and are kept as regression guards. See
the report for which is which.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
from tests_helpers import streamed_response

from mycelium_core import public_origin
from mycelium_core.config import Settings
from mycelium_core.oauth import cimd
from mycelium_core.oauth.origin import origin_bucket
from mycelium_core.oauth.redirects import RedirectPolicy
from mycelium_core.oauth.strategies import default_registry

CID = "https://client.example/meta.json"
DOC = json.dumps(
    {"client_id": CID, "client_name": "C", "redirect_uris": ["https://client.example/cb"]}
).encode()
POLICY = RedirectPolicy()


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _fetcher(handler: object, **kw: object) -> cimd.MetadataFetcher:
    return cimd.MetadataFetcher(
        resolve=kw.pop("resolve", _public),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# ----- content encodings (already refused; regression guards) -----------------


@pytest.mark.parametrize(
    "enc", ["GZIP", " gzip ", "identity, gzip", "x-gzip", "deflate", "br", "identity;q=1"]
)
async def test_every_content_encoding_but_identity_is_refused(enc: str) -> None:
    body = gzip.compress(DOC)

    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(
            200,
            content=body,
            headers={"content-type": "application/json", "content-encoding": enc},
        )

    with pytest.raises(cimd.CimdError):
        await _fetcher(handler).get(CID, POLICY)


async def test_a_gzip_body_without_the_header_is_not_inflated() -> None:
    body = gzip.compress(b"{" + b" " * 10_000_000 + b"}")

    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(200, content=body, headers={"content-type": "application/json"})

    with pytest.raises(cimd.CimdError):
        await _fetcher(handler).get(CID, POLICY)


# ----- L3: nesting bomb ---------------------------------------------------------------


async def test_a_deeply_nested_document_is_an_invalid_document_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(
            200, content=b"[" * 16000, headers={"content-type": "application/json"}
        )

    fetcher = _fetcher(handler)
    with pytest.raises(cimd.CimdError):
        await fetcher.get(CID, POLICY)
    # Attributable to the remote document, so it IS negative-cached.
    assert fetcher.cached_entries() == 1


def test_nesting_is_bounded_before_parsing_and_strings_do_not_count() -> None:
    deep = b'{"a":' * (cimd.MAX_JSON_DEPTH + 1) + b"1" + b"}" * (cimd.MAX_JSON_DEPTH + 1)
    with pytest.raises(cimd.CimdError):
        cimd._parse(CID, deep, POLICY)
    # Brackets inside strings (escaped quotes included) are not structure.
    doc = json.loads(DOC)
    doc["client_name"] = 'x\\"[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[['
    assert cimd._parse(CID, json.dumps(doc).encode(), POLICY).client_id == CID


# ----- M2: a hung resolver must not starve the fetches -------------------------------


@pytest.fixture
def hung_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """``socket.getaddrinfo`` hangs for ``slow*`` names until released, and
    answers a public address for anything else."""
    release = threading.Event()
    real = socket.getaddrinfo

    def fake(host: str, port: int, *a: object, **k: object) -> object:
        if host.startswith("slow"):
            release.wait(5)
            raise socket.gaierror("timeout")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    try:
        yield release
    finally:
        release.set()
        monkeypatch.setattr(socket, "getaddrinfo", real)


async def test_hung_lookups_hold_capacity_until_their_threads_end(
    hung_dns: threading.Event,
) -> None:
    """Reviewer: four hung lookups timed out, released their slots, and the
    next legitimate fetch then waited for a DNS thread until it timed out
    too -- and was negative-cached for a minute. A thread still blocked now
    still counts: the legitimate fetch is refused at once as a LOCAL
    capacity problem, which is never cached, and goes through once the
    threads are free."""
    fetcher = _fetcher(lambda r: streamed_response(404), resolve=cimd.system_resolve, timeout=0.3)
    results = await asyncio.gather(
        *[
            fetcher.get(f"https://slow-{i}.example/m", POLICY, origin=f"o{i}")
            for i in range(cimd.MAX_CONCURRENT_FETCHES)
        ],
        return_exceptions=True,
    )
    assert all(isinstance(r, cimd.CimdError) for r in results)

    t0 = time.monotonic()
    with pytest.raises(cimd.CimdError) as busy:
        await fetcher.get("https://fast.example/m", POLICY, origin="legit")
    assert busy.value.reason == cimd.FETCH_BUSY
    assert time.monotonic() - t0 < 0.2
    assert "https://fast.example/m" not in fetcher._cache

    hung_dns.set()
    for _ in range(100):
        if cimd.dns_threads_busy() == 0:
            break
        await asyncio.sleep(0.02)
    with pytest.raises(cimd.CimdError) as reached:
        await fetcher.get("https://fast.example/m", POLICY, origin="legit")
    # It got past the meter and to the (404) document this time.
    assert reached.value.reason == cimd.FETCH_FAILED


# ----- INFO: SIIT and site-local ----------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["::ffff:0:a9fe:a9fe", "::ffff:0:7f00:1", "::ffff:0:808:808", "fec0::1", "fec0:1::5"],
)
def test_translated_and_site_local_addresses_are_refused(address: str) -> None:
    assert cimd._is_global(address) is False


async def test_a_siit_answer_is_never_connected_to() -> None:
    seen: list[str] = []

    async def siit(host: str, port: int) -> list[str]:
        return ["::ffff:0:a9fe:a9fe"]  # IPv4-translated 169.254.169.254

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return streamed_response(404)

    with pytest.raises(cimd.CimdError):
        await _fetcher(handler, resolve=siit).get(CID, POLICY)
    assert seen == []


# ----- metadata-document ids parsed as strictly as redirect hosts ----------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://host./x",
        "https://" + chr(0xFF45) + "vil.com/x",  # fullwidth e
        "https://a" + chr(0x3002) + "b.com/x",  # ideographic full stop
        "https://2130706433/x",
        "https://h/%2e%2e/x",
        "https://h/%2E/x",
    ],
)
def test_metadata_document_ids_get_the_strict_authority(value: str) -> None:
    assert cimd.is_client_id_url(value) is False


# ----- INFO: host header grammar anchored --------------------------------------------------


@pytest.mark.parametrize("value", ["evil.com\n", "evil.com\r\n", "evil.com\nX: y"])
def test_a_host_with_a_trailing_line_break_is_refused(value: str) -> None:
    assert public_origin._host(value) is None


# ----- M1d: IPv6 counted per /48 -------------------------------------------------------------


def test_ipv6_origins_are_counted_per_48() -> None:
    assert origin_bucket("2001:db8:1:2::1") == origin_bucket("2001:db8:1:ffff::9")
    assert origin_bucket("2001:db8:1:2::1") == "2001:db8:1::/48"
    assert origin_bucket("2001:db8:1:2::1") != origin_bucket("2001:db8:2:2::1")


# ----- M1c: production without the required settings ----------------------------------------


def _settings(**kw: object) -> Settings:
    return Settings(**kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "missing",
    [
        {"public_base_url": "", "issuer_key_trusted_proxies": ["10.0.0.0/8"]},
        {"public_base_url": "https://m.example", "issuer_key_trusted_proxies": []},
    ],
)
def test_production_without_its_settings_disables_the_open_strategies(
    missing: dict[str, object],
) -> None:
    registry = default_registry()
    unsafe = _settings(env="production", **missing)
    meta = registry.metadata("https://m.example", unsafe)
    assert "registration_endpoint" not in meta
    assert "client_id_metadata_document_supported" not in meta
    assert registry.registration(unsafe) is None
    assert registry.claimant("https://app.example/c.json", unsafe) is None
    # Hand-registered clients and assistant secrets have no open endpoint
    # behind them, and keep working.
    assert registry.claimant("mycelium_pc_x", unsafe) is not None
    assert registry.claimant("6f1e1d5a-4a51-4c35-9c4b-8f3fbbd0a3a2", unsafe) is not None


def test_production_with_its_settings_keeps_the_open_strategies() -> None:
    registry = default_registry()
    safe = _settings(
        env="production",
        public_base_url="https://m.example",
        issuer_key_trusted_proxies=["10.0.0.0/8"],
    )
    meta = registry.metadata("https://m.example", safe)
    assert meta["registration_endpoint"] == "https://m.example/api/oauth/register"
    assert meta["client_id_metadata_document_supported"] is True
