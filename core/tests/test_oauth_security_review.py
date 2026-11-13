"""Findings of the independent security review of ADR-0069, as unit tests.

Each test is the reviewer's demonstration turned around: it asserts the SAFE
behaviour, and each was observed failing against the tree the review read
(2026-11-13) before the fix it guards. Grouped by finding.

The redirect vectors in ``_WHATWG`` were produced by the WHATWG URL parser of
node v26.5.0 (``new URL(u).host``), which is what every browser runs; they are
fixed here so the suite does not depend on node.
"""

from __future__ import annotations

import asyncio
import gzip
import tracemalloc

import httpx
import pytest
from tests_helpers import streamed_response

from mycelium_core import public_origin
from mycelium_core.oauth import cimd
from mycelium_core.oauth.origin import origin_bucket
from mycelium_core.oauth.redirects import RedirectPolicy, RedirectUriError, redirect_target
from mycelium_core.services.oauth_consent import verify_pkce

CID = "https://app.example/oauth/client.json"
DOC = {
    "client_id": CID,
    "client_name": "App",
    "redirect_uris": ["https://app.example/cb"],
}
POLICY = RedirectPolicy()


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _fetcher(handler: object, **kw: object) -> cimd.MetadataFetcher:
    return cimd.MetadataFetcher(
        resolve=kw.pop("resolve", _public),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# ----- 1. decompression bomb ----------------------------------------------------


async def test_a_compressed_document_is_refused_without_inflating_it() -> None:
    raw = b"\0" * (200 * 1024 * 1024)
    body = gzip.compress(gzip.compress(raw, 9), 9)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return streamed_response(
            200,
            content=body,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip, gzip"},
        )

    tracemalloc.start()
    try:
        with pytest.raises(cimd.CimdError):
            await _fetcher(handler).get(CID, POLICY)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB: the body was inflated"
    assert seen[0].headers["accept-encoding"] == "identity"


async def test_any_content_encoding_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(
            200,
            content=gzip.compress(b'{"client_id": "x"}'),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )

    with pytest.raises(cimd.CimdError):
        await _fetcher(handler).get(CID, POLICY)


# ----- 3. metered, port 443 only, uniform errors ----------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("https://app.example/c.json", True),
        ("https://app.example:443/c.json", True),
        ("https://app.example:8443/c.json", False),
        ("https://app.example:22/c.json", False),
        ("https://app.example:6379/c.json", False),
    ],
)
def test_only_the_https_port_is_fetched(value: str, ok: bool) -> None:
    assert cimd.is_client_id_url(value) is ok


def _raising(exc: Exception) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        _raising(httpx.ConnectError("connection refused")),
        _raising(httpx.ReadTimeout("timed out")),
        _raising(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")),
        lambda r: streamed_response(404),
        lambda r: streamed_response(500),
        lambda r: streamed_response(302, headers={"Location": "https://x.example/"}),
        lambda r: streamed_response(200, content=b"<html>", headers={"Content-Type": "text/html"}),
    ],
)
async def test_network_outcomes_are_indistinguishable_to_the_client(handler: object) -> None:
    """Otherwise the fetch is a port and service scanner with a readable
    answer. The detail goes to the log only."""
    with pytest.raises(cimd.CimdError) as caught:
        await _fetcher(handler).get(CID, POLICY)
    assert caught.value.reason == cimd.FETCH_FAILED


async def test_a_non_public_address_reads_the_same_as_a_dead_one() -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    with pytest.raises(cimd.CimdError) as caught:
        await _fetcher(lambda r: streamed_response(200), resolve=private).get(CID, POLICY)
    assert caught.value.reason == cimd.FETCH_FAILED


async def test_fetches_are_metered_per_origin_before_any_io() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return streamed_response(404)

    fetcher = _fetcher(handler)
    refused_busy = 0
    for i in range(cimd.FETCHES_PER_ORIGIN + 5):
        try:
            await fetcher.get(f"https://app.example/c{i}.json", POLICY, origin="203.0.113.7")
        except cimd.CimdError as exc:
            if exc.reason == cimd.FETCH_BUSY:
                refused_busy += 1
    assert len(seen) == cimd.FETCHES_PER_ORIGIN
    assert refused_busy == 5
    # Another origin still has its own budget.
    with pytest.raises(cimd.CimdError) as other:
        await fetcher.get("https://app.example/other.json", POLICY, origin="203.0.113.8")
    assert other.value.reason == cimd.FETCH_FAILED


async def test_concurrent_fetches_are_capped_and_the_excess_refused_at_once() -> None:
    gate = asyncio.Event()
    inside = 0

    async def slow(host: str, port: int) -> list[str]:
        nonlocal inside
        inside += 1
        await gate.wait()
        return ["93.184.216.34"]

    fetcher = _fetcher(lambda r: streamed_response(404), resolve=slow)
    tasks = [
        asyncio.create_task(fetcher.get(f"https://app.example/s{i}.json", POLICY, origin=f"o{i}"))
        for i in range(cimd.MAX_CONCURRENT_FETCHES)
    ]
    while inside < cimd.MAX_CONCURRENT_FETCHES:  # noqa: ASYNC110 (polls a fake's counter)
        await asyncio.sleep(0.01)
    with pytest.raises(cimd.CimdError) as busy:
        await asyncio.wait_for(
            fetcher.get("https://app.example/late.json", POLICY, origin="late"), 0.5
        )
    assert busy.value.reason == cimd.FETCH_BUSY
    gate.set()
    for t in tasks:
        with pytest.raises(cimd.CimdError):
            await t


async def test_a_resolver_that_never_answers_is_abandoned() -> None:
    async def never(host: str, port: int) -> list[str]:
        await asyncio.Event().wait()
        return []

    fetcher = _fetcher(lambda r: streamed_response(404), resolve=never, timeout=0.2)
    with pytest.raises(cimd.CimdError) as caught:
        await asyncio.wait_for(fetcher.get(CID, POLICY), 2)
    assert caught.value.reason == cimd.FETCH_FAILED


# ----- 4. redirect host confusion ---------------------------------------------------

# Accepted URIs and the host a browser contacts for them (node v26.5.0 URL).
_WHATWG = {
    "https://claude.ai/api/mcp/auth_callback": "https://claude.ai",
    "https://Claude.AI/cb": "https://claude.ai",
    "https://a.example:443/cb": "https://a.example",
    "https://a.example:8443/cb": "https://a.example:8443",
    "http://127.0.0.1:33418/cb": "http://127.0.0.1:33418",
    "http://[::1]:8/cb": "http://[::1]:8",
    "https://xn--clude-7ve.ai/cb": "https://xn--clude-7ve.ai",
}


@pytest.mark.parametrize(("uri", "host"), sorted(_WHATWG.items()))
def test_the_target_shown_is_the_host_a_browser_contacts(uri: str, host: str) -> None:
    assert POLICY.validate(uri) == uri
    assert redirect_target(uri) == host


@pytest.mark.parametrize(
    "uri",
    [
        # node: host "evil.example", path "/.claude.ai/api/callback"
        "https://evil.example\\.claude.ai/api/callback",
        # node: host "a.example", path "/@b.example/cb"
        "https://a.example\\@b.example/cb",
        "https://a.example/x\\y",  # node rewrites the path to /x/y
        "https://%65vil.example/cb",  # node: host "evil.example"
        "https://cl" + chr(0x430) + "ude.ai/cb",  # Cyrillic a; node: "xn--clude-5ve.ai"
        "https://claude.ai/caf" + chr(0xE9),  # non-ASCII anywhere
        "https://claude.ai./cb",  # trailing dot
        "https://:443/cb",  # empty host
        "https://0x7f.1/cb",  # node: 127.0.0.1
        "https://127.1/cb",  # node: 127.0.0.1
        "https://01.2.3.4/cb",  # leading zero: octal in some parsers
        "https://-a.example/cb",
        "https://a_b.example/cb",
        "http://localhost./cb",
    ],
)
def test_authorities_a_browser_reads_differently_are_refused(uri: str) -> None:
    with pytest.raises(RedirectUriError):
        POLICY.validate(uri)


def test_private_use_authorities_get_the_same_strictness() -> None:
    native = RedirectPolicy(private_use_schemes=frozenset({"cursor"}))
    assert native.validate("cursor://anysphere.cursor-mcp/cb")
    for bad in ("cursor://a\\b/cb", "cursor://%61/cb", "cursor://" + chr(0xE4) + "/cb"):
        with pytest.raises(RedirectUriError):
            native.validate(bad)


# ----- 5. origin buckets ----------------------------------------------------------------


def test_ipv6_origins_share_a_bucket() -> None:
    # Per /64 at the first review, per /48 since the second
    # (test_oauth_security_review_2.py): a /64 is always inside one bucket.
    assert origin_bucket("2001:db8:1:2::1") == origin_bucket("2001:db8:1:2:ffff::9")
    assert origin_bucket("2001:db8:1:2::1") != origin_bucket("2001:db8:2:2::1")
    assert origin_bucket("203.0.113.9") == "203.0.113.9"
    assert origin_bucket("::ffff:203.0.113.9") == "203.0.113.9"
    assert origin_bucket(None) is None
    assert origin_bucket("not an ip") is None


# ----- 6. client-controlled issuer ----------------------------------------------------


def test_forwarded_host_and_proto_must_be_well_formed() -> None:
    h = {"host": "backend:8000", "x-forwarded-host": 'evil.example", error="injected'}
    assert public_origin.issuer(h, scheme="http", netloc="backend:8000") == "http://backend:8000"
    h2 = {"host": "m.example", "x-forwarded-proto": "javascript"}
    assert public_origin.issuer(h2, scheme="http", netloc="m.example") == "http://m.example"
    h3 = {"host": "m.example\r\nX: y"}
    assert public_origin.issuer(h3, scheme="https", netloc="fallback.example") == (
        "https://fallback.example"
    )
    ok = {"host": "backend", "x-forwarded-host": "m.example:8443", "x-forwarded-proto": "https"}
    assert public_origin.issuer(ok, scheme="http", netloc="backend") == "https://m.example:8443"


def test_the_configured_base_url_wins_over_any_header(monkeypatch: pytest.MonkeyPatch) -> None:
    from mycelium_core.config import get_settings

    monkeypatch.setenv("MYCELIUM_PUBLIC_BASE_URL", "https://mycelium.example/")
    get_settings.cache_clear()
    try:
        h = {"host": "evil.example", "x-forwarded-host": "evil.example"}
        assert public_origin.issuer(h, scheme="http", netloc="evil.example") == (
            "https://mycelium.example"
        )
    finally:
        monkeypatch.delenv("MYCELIUM_PUBLIC_BASE_URL")
        get_settings.cache_clear()


def test_the_challenge_header_value_is_quoted_safely() -> None:
    assert public_origin.bearer_challenge("https://m.example") == (
        'Bearer resource_metadata="https://m.example/.well-known/oauth-protected-resource/mcp"'
    )
    # Even if an issuer with a quote ever reached it, it could not add a parameter.
    assert '", ' not in public_origin.bearer_challenge('https://a"b.example')


# ----- 7. addresses with an embedded IPv4 -----------------------------------------------


@pytest.mark.parametrize(
    ("address", "ok"),
    [
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("64:ff9b::7f00:1", False),  # NAT64 of 127.0.0.1
        ("64:ff9b::a00:1", False),  # NAT64 of 10.0.0.1
        ("64:ff9b::5db8:d822", True),  # NAT64 of 93.184.216.34
        ("64:ff9b:1::a00:1", False),  # local-use NAT64
        ("::7f00:1", False),  # IPv4-compatible 127.0.0.1
        ("::ffff:10.0.0.1", False),
        ("::ffff:93.184.216.34", True),
        ("2002:7f00:1::1", False),  # 6to4 of 127.0.0.1
        ("2002:a9fe:a9fe::1", False),  # 6to4 of 169.254.169.254
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),  # Teredo
    ],
)
def test_embedded_ipv4_is_checked_not_trusted(address: str, ok: bool) -> None:
    assert cimd._is_global(address) is ok


# ----- 8. PKCE verifier grammar --------------------------------------------------------


def test_the_verifier_must_have_rfc7636_shape() -> None:
    import base64
    import hashlib

    def challenge(v: str) -> str:
        d = hashlib.sha256(v.encode()).digest()
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    good = "a" * 43
    assert verify_pkce(good, challenge(good))
    for bad in ("a" * 42, "a" * 129, "a" * 42 + "!", "a" * 42 + " "):
        assert not verify_pkce(bad, challenge(bad)), bad
