"""Client ID Metadata Documents: the defences, against a fake network.

A CIMD client id is an https URL the server fetches, on an UNAUTHENTICATED
path, so every property below is a way that fetch could be turned against
the server or its network: reaching an internal address, being redirected
there, being made to read without bound, or being re-pointed between the
check and the connection (DNS rebinding). Each is asserted with a fake
resolver and an ``httpx.MockTransport``: the fetcher publishes both as seams,
and nothing here touches the real network.

Observed failing before the module existed (2026-11-13): ``mycelium_core.
oauth.cimd`` did not import.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from tests_helpers import streamed_response

from mycelium_core.oauth.cimd import (
    MAX_DOCUMENT_BYTES,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    CimdError,
    MetadataFetcher,
    is_client_id_url,
)
from mycelium_core.oauth.redirects import RedirectPolicy

CID = "https://app.example/oauth/client.json"
POLICY = RedirectPolicy()
GOOD_DOC = {
    "client_id": CID,
    "client_name": "Example App",
    "redirect_uris": ["https://app.example/cb"],
    "token_endpoint_auth_method": "none",
}


class FakeResolver:
    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        return list(self.table.get(host, []))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _transport(
    body: object = GOOD_DOC,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        content = raw if raw is not None else json.dumps(body).encode()
        return streamed_response(
            status,
            content=content,
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    return httpx.MockTransport(handler)


def _fetcher(
    transport: httpx.AsyncBaseTransport,
    table: dict[str, list[str]] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[MetadataFetcher, FakeResolver]:
    resolver = FakeResolver(table if table is not None else {"app.example": ["93.184.216.34"]})
    return MetadataFetcher(resolve=resolver, transport=transport, clock=clock or Clock()), resolver


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (CID, True),
        ("https://app.example/", False),  # needs a path beyond the root
        ("https://app.example", False),
        ("http://app.example/c.json", False),
        ("https://app.example/c.json#f", False),
        ("https://u@app.example/c.json", False),
        ("https://app.example/a/../c.json", False),
        ("https://app.example/./c.json", False),
        ("mycelium_pc_x", False),
        ("6f1e1d5a-4a51-4c35-9c4b-8f3fbbd0a3a2", False),
        ("https://app.example/" + "a" * 2100, False),
    ],
)
def test_what_counts_as_a_metadata_document_client_id(value: str, ok: bool) -> None:
    assert is_client_id_url(value) is ok


async def test_a_good_document_is_read_and_connects_to_the_verified_ip() -> None:
    seen: list[httpx.Request] = []
    fetcher, _ = _fetcher(_transport(seen=seen))
    meta = await fetcher.get(CID, POLICY)
    assert meta.client_id == CID
    assert meta.client_name == "Example App"
    assert meta.redirect_uris == ("https://app.example/cb",)
    # The connection goes to the address that was checked, not to a name a
    # second lookup could re-point; the name travels as Host and as SNI.
    (req,) = seen
    assert req.url.host == "93.184.216.34"
    assert req.url.path == "/oauth/client.json"
    assert req.headers["host"] == "app.example"
    assert req.extensions.get("sni_hostname") == "app.example"


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.5",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 (an address to refuse, nothing binds to it)
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_a_name_resolving_to_a_non_global_address_is_refused(ip: str) -> None:
    seen: list[httpx.Request] = []
    fetcher, _ = _fetcher(_transport(seen=seen), {"app.example": [ip]})
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)
    assert seen == [], "no connection may be opened to a refused address"


async def test_one_non_global_address_among_several_refuses_the_name() -> None:
    fetcher, _ = _fetcher(_transport(), {"app.example": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


async def test_an_unresolvable_name_is_refused() -> None:
    fetcher, _ = _fetcher(_transport(), {})
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


async def test_a_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []
    fetcher, _ = _fetcher(
        _transport(status=302, headers={"Location": "http://10.0.0.1/doc"}, seen=seen)
    )
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)
    assert len(seen) == 1


async def test_an_oversized_document_is_refused() -> None:
    padded = dict(GOOD_DOC, padding="x" * (MAX_DOCUMENT_BYTES + 10))
    fetcher, _ = _fetcher(_transport(padded))
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


async def test_a_non_json_content_type_is_refused() -> None:
    fetcher, _ = _fetcher(_transport(headers={"Content-Type": "text/html"}))
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


@pytest.mark.parametrize(
    "doc",
    [
        dict(GOOD_DOC, client_id="https://app.example/oauth/other.json"),  # mismatch
        dict(GOOD_DOC, client_id=CID + "/"),
        dict(GOOD_DOC, redirect_uris=["http://app.example/cb"]),  # policy refuses
        dict(GOOD_DOC, redirect_uris=[]),
        dict(GOOD_DOC, token_endpoint_auth_method="client_secret_post"),
        dict(GOOD_DOC, client_secret="s3cret"),
        ["not", "an", "object"],
    ],
)
async def test_a_document_that_does_not_describe_this_client_is_refused(doc: object) -> None:
    fetcher, _ = _fetcher(_transport(doc))
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


async def test_invalid_json_is_refused() -> None:
    fetcher, _ = _fetcher(_transport(raw=b"{not json"))
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)


async def test_a_cache_hit_does_no_io_and_expiry_refetches() -> None:
    seen: list[httpx.Request] = []
    clock = Clock()
    fetcher, resolver = _fetcher(
        _transport(seen=seen, headers={"Cache-Control": "max-age=600"}), clock=clock
    )
    await fetcher.get(CID, POLICY)
    await fetcher.get(CID, POLICY)
    assert len(seen) == 1
    assert len(resolver.calls) == 1
    clock.now += 601
    await fetcher.get(CID, POLICY)
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("header", "ttl"),
    [
        ("max-age=1", MIN_TTL_SECONDS),
        ("no-store", MIN_TTL_SECONDS),
        (None, MIN_TTL_SECONDS),
        ("max-age=99999999", MAX_TTL_SECONDS),
        ("public, max-age=3600", 3600),
    ],
)
async def test_cache_lifetime_honours_max_age_within_bounds(header: str | None, ttl: int) -> None:
    seen: list[httpx.Request] = []
    clock = Clock()
    headers = {"Cache-Control": header} if header else {}
    fetcher, _ = _fetcher(_transport(seen=seen, headers=headers), clock=clock)
    await fetcher.get(CID, POLICY)
    clock.now += ttl - 1
    await fetcher.get(CID, POLICY)
    assert len(seen) == 1
    clock.now += 2
    await fetcher.get(CID, POLICY)
    assert len(seen) == 2


async def test_a_failure_is_cached_briefly() -> None:
    seen: list[httpx.Request] = []
    clock = Clock()
    fetcher, _ = _fetcher(_transport(status=404, seen=seen), clock=clock)
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)
    with pytest.raises(CimdError):
        await fetcher.get(CID, POLICY)
    assert len(seen) == 1


async def test_the_cache_is_bounded() -> None:
    fetcher = MetadataFetcher(
        resolve=FakeResolver({"app.example": ["93.184.216.34"]}),
        transport=_transport_echo(),
        clock=Clock(),
        cache_size=3,
    )
    for i in range(10):
        await fetcher.get(f"https://app.example/c{i}.json", POLICY)
    assert fetcher.cached_entries() <= 3


def _transport_echo() -> httpx.MockTransport:
    """A server whose document always names the URL it was fetched from."""

    def handler(request: httpx.Request) -> httpx.Response:
        cid = f"https://{request.headers['host']}{request.url.path}"
        doc = dict(GOOD_DOC, client_id=cid)
        return streamed_response(200, json=doc)

    return httpx.MockTransport(handler)
