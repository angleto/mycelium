"""Client ID Metadata Documents, fetched safely (ADR-0069).

draft-ietf-oauth-client-id-metadata-document, which the MCP authorization
specification adopts: a client's ``client_id`` is an https URL, and the JSON
document at that URL is the client's registration -- its name and its
redirect URIs. No registration endpoint, no stored row.

The price is that the server fetches a URL chosen by an unauthenticated
caller, which is a server-side request forgery surface. Every defence below
is against a way that fetch could be turned on the server or its network:

- **https only**, and the id must be a plain URL with a path: no userinfo, no
  fragment, no dot segments, bounded length (``is_client_id_url``).
- **Only global addresses.** The name is resolved once, and EVERY address it
  resolves to must be globally routable (``ipaddress.is_global``, with IPv4-
  mapped IPv6 unwrapped first): no loopback, private, link-local (cloud
  metadata at 169.254.169.254), carrier-grade NAT, multicast or reserved
  range. One bad address refuses the name, because which one a client
  library would pick is not ours to predict.
- **Connect to the verified address.** The request goes to the IP that was
  checked, with the name carried as ``Host`` and as TLS SNI (so the
  certificate is still verified against the name). A second lookup at
  connection time is what DNS rebinding exploits, and there is none.
- **No redirects** followed: a 3xx is a refusal, since a redirect is a way
  to a second, unchecked address. No proxy from the environment either.
- **Bounded**: 3 s for the whole fetch, 16 KiB read as a stream and
  abandoned past the limit, ``application/json`` (or ``+json``) only.
- **The document must describe this client**: its ``client_id`` equals the
  URL exactly, its redirect URIs pass the deployment's redirect policy, it
  claims no secret and no authentication method other than ``none``.

**Cache: in this process, bounded.** A document is reused for its
``Cache-Control: max-age``, clamped to between 5 minutes and 24 hours (a
missing or ``no-store`` header gets the 5-minute floor: refetching on every
/authorize would hand any caller a way to make the server fetch at will). A
failure is remembered for one minute so a bad id cannot be used to hammer a
host through us. In the process rather than in Postgres because what is
cached is public, cheap to refetch and not needed for correctness: each
replica fetching once per window is the whole cost, and a table would add
writes on an unauthenticated path to save it. The cache holds at most
``CACHE_SIZE`` entries, least recently used evicted first.

**No network on /token.** The flow resolves the client at /authorize and
snapshots what it needs (name, kind, the redirect URI used) on the stored
request; the consent page and the exchange read that row. Only /authorize
fetches.

What this does NOT protect against: a public host that serves a document
naming its own redirect URIs is exactly a legitimate client, and the consent
page (which shows where the code goes) is the control for that. A host that
is slow but within 3 s still costs this process a connection for that long.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from mycelium_core.oauth.redirects import RedirectPolicy, RedirectUriError, parse_authority

MAX_CLIENT_ID_LENGTH = 2048
MAX_DOCUMENT_BYTES = 16 * 1024
FETCH_TIMEOUT_SECONDS = 3.0
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 86400
NEGATIVE_TTL_SECONDS = 60
CACHE_SIZE = 512
MAX_CLIENT_NAME_LENGTH = 100

# Metering, charged BEFORE any I/O and only for cache misses. Each fetch is
# an outbound connection made on behalf of an unauthenticated caller, so it
# is bounded three ways: per origin (``oauth.origin``) per window, for
# everybody per window, and in flight at once -- the last refused at once
# rather than queued, because a queue of slow fetches is the resource
# exhaustion this exists to prevent. Per process: each replica meters its own
# fetches, which bounds what the deployment does by replicas x these numbers.
FETCHES_PER_ORIGIN = 10
FETCHES_GLOBAL = 120
FETCH_WINDOW_SECONDS = 60
MAX_CONCURRENT_FETCHES = 4
_MAX_TRACKED_ORIGINS = 10_000

# What the client is told. Every network outcome -- unresolvable, non-public
# address, refused connection, timeout, TLS failure, any status but 200, a
# redirect, a wrong content type or encoding, too large -- is ONE answer, or
# the fetch becomes a scanner that reports which ports and services answer
# where. The detail goes to the log only.
FETCH_FAILED = "the client metadata document could not be retrieved"
FETCH_BUSY = "too many client metadata document fetches; try again later"

_log = logging.getLogger("mycelium.oauth.cimd")

# getaddrinfo blocks a thread and cannot be cancelled. A pool of its own, as
# small as the concurrency cap, so a resolver that never answers can only
# ever occupy these threads -- never the default executor the rest of the
# process shares. And the threads are COUNTED until they really end, not
# until the fetch that started them gives up: a lookup abandoned at the
# timeout still holds its thread, and a meter that forgot it admitted fetches
# that then waited for a thread until they timed out too (second security
# review, 2026-11-13). Full is a local condition, refused at once and never
# cached against the id being asked for.
_DNS_WORKERS = MAX_CONCURRENT_FETCHES
_DNS_POOL = ThreadPoolExecutor(max_workers=_DNS_WORKERS, thread_name_prefix="cimd-dns")
_dns_lock = threading.Lock()
_dns_busy = 0

# Nesting a parser will follow. A document is a flat object with two lists;
# anything deeper is not a client registration, and ``json.loads`` recurses,
# so a 16 KiB run of ``[`` raised RecursionError through the request (500).
MAX_JSON_DEPTH = 32


def dns_threads_busy() -> int:
    """Lookups submitted to the pool and not yet finished, abandoned or not."""
    with _dns_lock:
        return _dns_busy


def _dns_done(_future: object) -> None:
    global _dns_busy
    with _dns_lock:
        _dns_busy -= 1


_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*(\d+)\s*(?:,|$)", re.IGNORECASE)

Resolve = Callable[[str, int], Awaitable[list[str]]]


class CimdError(Exception):
    """The document could not be used. ``reason`` is safe to show the
    client's developer: it never names an address, a port or a network
    outcome (see ``FETCH_FAILED``)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ClientMetadata:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]


def is_client_id_url(value: str) -> bool:
    """Whether ``value`` has the shape of a metadata-document client id. Pure,
    no I/O: this is the strategy's ``claims``."""
    if not value.startswith("https://") or len(value) > MAX_CLIENT_ID_LENGTH:
        return False
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    if parts.fragment or "#" in value or "\\" in value:
        return False
    # The authority gets the redirect policy's strict parse (ASCII LDH labels,
    # canonical dotted-quad IPv4, no trailing dot, no ``%``, no userinfo), so
    # the name this server resolves is the name anybody else would read.
    try:
        _host, port = parse_authority(parts.netloc)
    except ValueError:
        return False
    # The https port or none. Any other port turns the fetch into a way to
    # probe whatever listens there on a public host.
    if port not in (None, 443):
        return False
    if not parts.path or parts.path == "/":
        return False
    # Dot segments, percent-encoded or not: a server may normalise them, and
    # then the document served is not the one the id names.
    segments = [unquote(seg) for seg in parts.path.split("/")]
    return "." not in segments and ".." not in segments


async def system_resolve(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for ``host``, resolved on the
    module's own small thread pool (see ``_DNS_POOL``)."""
    global _dns_busy
    with _dns_lock:
        _dns_busy += 1
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except BaseException:
        _dns_done(None)
        raise
    # Fires when the thread finishes, or when a queued lookup is cancelled
    # before it started: either way the thread is free again.
    future.add_done_callback(_dns_done)
    infos = await asyncio.wrap_future(future)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
# SIIT IPv4-translated addresses (RFC 2765/6145), which ``ipv4_mapped`` does
# not recognise, and the deprecated site-local range, which ``is_global``
# does not exclude. Both refused outright.
_SIIT_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


def _is_global(address: str) -> bool:
    """Whether a connection to ``address`` reaches the public internet.

    An IPv6 address that CARRIES an IPv4 address is judged by the IPv4 it
    carries, because that is where a translator or tunnel delivers it:
    IPv4-mapped (``::ffff:a.b.c.d``), IPv4-compatible (``::a.b.c.d``), NAT64
    (``64:ff9b::/96``) and 6to4 (``2002::/16``). Local-use NAT64
    (``64:ff9b:1::/48``), SIIT (``::ffff:0:0:0/96``), site-local
    (``fec0::/10``) and Teredo are refused outright: what they reach depends
    on a translator or a site this process cannot see."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if (
            ip in _NAT64_LOCAL
            or ip in _SIIT_TRANSLATED
            or ip in _SITE_LOCAL
            or ip.teredo is not None
        ):
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_WELL_KNOWN or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


def _ttl(cache_control: str | None) -> float:
    if cache_control:
        m = _MAX_AGE.search(cache_control)
        if m:
            return float(min(MAX_TTL_SECONDS, max(MIN_TTL_SECONDS, int(m.group(1)))))
    return float(MIN_TTL_SECONDS)


class MetadataFetcher:
    """Fetch, check, meter and cache metadata documents.

    ``resolve`` and ``transport`` are the seams: production uses the system
    resolver and httpx's own transport, tests pass fakes and no real network
    is touched. ``clock`` is monotonic seconds; ``timeout`` bounds the whole
    fetch, name resolution included."""

    def __init__(
        self,
        *,
        resolve: Resolve = system_resolve,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_size: int = CACHE_SIZE,
        timeout: float = FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self._resolve = resolve
        self._transport = transport
        self._clock = clock
        self._cache_size = cache_size
        self._timeout = timeout
        self._cache: OrderedDict[str, tuple[float, ClientMetadata | CimdError]] = OrderedDict()
        self._per_origin: OrderedDict[str, deque[float]] = OrderedDict()
        self._global: deque[float] = deque()
        self._in_flight = 0

    def cached_entries(self) -> int:
        return len(self._cache)

    def _admit(self, origin: str | None, now: float) -> bool:
        """Charge one fetch to ``origin`` and to everybody, or refuse. The
        event loop is single-threaded and nothing here awaits, so the check
        and the charge cannot interleave with another request's."""
        if self._in_flight >= MAX_CONCURRENT_FETCHES or dns_threads_busy() >= _DNS_WORKERS:
            return False
        cutoff = now - FETCH_WINDOW_SECONDS
        while self._global and self._global[0] <= cutoff:
            self._global.popleft()
        if len(self._global) >= FETCHES_GLOBAL:
            return False
        if origin is not None:
            stamps = self._per_origin.get(origin)
            if stamps is None:
                stamps = deque()
                self._per_origin[origin] = stamps
                while len(self._per_origin) > _MAX_TRACKED_ORIGINS:
                    self._per_origin.popitem(last=False)
            while stamps and stamps[0] <= cutoff:
                stamps.popleft()
            if len(stamps) >= FETCHES_PER_ORIGIN:
                return False
            stamps.append(now)
            self._per_origin.move_to_end(origin)
        self._global.append(now)
        return True

    async def get(
        self, client_id: str, policy: RedirectPolicy, *, origin: str | None = None
    ) -> ClientMetadata:
        now = self._clock()
        hit = self._cache.get(client_id)
        if hit is not None and hit[0] > now:
            self._cache.move_to_end(client_id)
            if isinstance(hit[1], CimdError):
                raise hit[1]
            return hit[1]
        if not is_client_id_url(client_id):
            raise CimdError(FETCH_FAILED)
        if not self._admit(origin, now):
            _log.info("client metadata fetch refused by the meter: origin=%s", origin)
            raise CimdError(FETCH_BUSY)
        self._in_flight += 1
        try:
            meta, ttl = await asyncio.wait_for(
                self._fetch(client_id, policy), timeout=self._timeout
            )
        except CimdError as exc:
            self._remember(client_id, now + NEGATIVE_TTL_SECONDS, exc)
            raise
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            _log.info("client metadata fetch failed for %s: %r", client_id, exc)
            failure = CimdError(FETCH_FAILED)
            self._remember(client_id, now + NEGATIVE_TTL_SECONDS, failure)
            raise failure from None
        finally:
            self._in_flight -= 1
        self._remember(client_id, now + ttl, meta)
        return meta

    def _remember(self, key: str, until: float, value: ClientMetadata | CimdError) -> None:
        self._cache[key] = (until, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _refuse(self, client_id: str, detail: str) -> CimdError:
        _log.info("client metadata fetch refused for %s: %s", client_id, detail)
        return CimdError(FETCH_FAILED)

    async def _fetch(self, client_id: str, policy: RedirectPolicy) -> tuple[ClientMetadata, float]:
        parts = urlsplit(client_id)
        host = parts.hostname or ""
        addresses = await self._resolve(host, 443)
        if not addresses:
            raise self._refuse(client_id, "no address")
        if not all(_is_global(a) for a in addresses):
            raise self._refuse(client_id, f"non-public address among {addresses}")
        ip = addresses[0].split("%", 1)[0]
        netloc = f"[{ip}]" if ":" in ip else ip
        pinned = urlunsplit(("https", netloc, parts.path, parts.query, ""))
        host_header = host  # the port, if spelled, can only be 443

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                pinned,
                # identity: the size limit below counts what is on the wire,
                # and a compressed body would be inflated before it is
                # counted -- 496 bytes of nested gzip became 459 MB when this
                # read decoded content (security review, 2026-11-13).
                headers={
                    "Host": host_header,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": host},
            ) as resp:
                if resp.status_code != 200:
                    raise self._refuse(client_id, f"HTTP {resp.status_code}")
                encoding = resp.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    raise self._refuse(client_id, f"content-encoding {encoding!r}")
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype != "application/json" and not ctype.endswith("+json"):
                    raise self._refuse(client_id, f"content-type {ctype!r}")
                buf = bytearray()
                # aiter_raw: the bytes as received, never decoded.
                async for chunk in resp.aiter_raw():
                    buf.extend(chunk)
                    if len(buf) > MAX_DOCUMENT_BYTES:
                        raise self._refuse(client_id, "larger than the limit")
                ttl = _ttl(resp.headers.get("cache-control"))
        return _parse(client_id, bytes(buf), policy), ttl


def _depth_within_limit(raw: bytes) -> bool:
    """Whether the structure nests at most ``MAX_JSON_DEPTH`` deep, counted
    on the bytes before parsing; brackets inside strings are not structure."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return True


def _parse(client_id: str, raw: bytes, policy: RedirectPolicy) -> ClientMetadata:
    if not _depth_within_limit(raw):
        raise CimdError("the client metadata document nests too deeply")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        raise CimdError("the client metadata document is not valid JSON") from None
    if not isinstance(doc, dict):
        raise CimdError("the client metadata document is not a JSON object")
    if doc.get("client_id") != client_id:
        raise CimdError("the document's client_id does not equal its URL")
    if "client_secret" in doc or "client_secret_expires_at" in doc:
        raise CimdError("a client metadata document must not carry a secret")
    method = doc.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise CimdError("only token_endpoint_auth_method 'none' is supported")
    try:
        uris = policy.validate_list(doc.get("redirect_uris"))
    except RedirectUriError:
        raise CimdError("the document's redirect_uris are missing or not acceptable") from None
    name = doc.get("client_name")
    if isinstance(name, str):
        cleaned = "".join(ch for ch in " ".join(name.split()) if ch.isprintable())
        name = cleaned[:MAX_CLIENT_NAME_LENGTH]
    if not isinstance(name, str) or not name:
        # A nameless client is called by its own URL's host: the one thing
        # about it the fetch has proved.
        name = urlsplit(client_id).hostname or client_id[:MAX_CLIENT_NAME_LENGTH]
    return ClientMetadata(client_id=client_id, client_name=name, redirect_uris=tuple(uris))


_shared: MetadataFetcher = MetadataFetcher()


def shared_fetcher() -> MetadataFetcher:
    """The process-wide fetcher, and so the process-wide cache."""
    return _shared


def set_shared_fetcher(fetcher: MetadataFetcher) -> MetadataFetcher:
    """Install a fetcher (tests: one with a fake network); returns the
    previous one so the caller can restore it."""
    global _shared
    previous = _shared
    _shared = fetcher
    return previous


__all__ = [
    "CACHE_SIZE",
    "FETCHES_GLOBAL",
    "FETCHES_PER_ORIGIN",
    "FETCH_BUSY",
    "FETCH_FAILED",
    "FETCH_TIMEOUT_SECONDS",
    "FETCH_WINDOW_SECONDS",
    "MAX_CONCURRENT_FETCHES",
    "MAX_DOCUMENT_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "NEGATIVE_TTL_SECONDS",
    "CimdError",
    "ClientMetadata",
    "MetadataFetcher",
    "dns_threads_busy",
    "is_client_id_url",
    "set_shared_fetcher",
    "shared_fetcher",
    "system_resolve",
]
