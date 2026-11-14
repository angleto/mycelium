"""The two deployment settings the open OAuth strategies stand on, checked at
startup (ADR-0069).

``oauth.production`` switches dynamic registration and metadata documents off
when ``MYCELIUM_PUBLIC_BASE_URL`` or ``MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES`` is
EMPTY. It used to switch them on as soon as both were non-empty, whatever they
said, and two wrong values passed that gate while disabling what it exists to
guarantee (mycelium-cc review, 2026-11-13):

- a proxy list with an entry that does not parse: the client-address
  resolver (``deps._resolve_issuer_client_ip``) turns the ``ValueError`` into
  "unattributable" on every request, and an unattributable request skips the
  per-origin bounds of registration and of the metadata-document fetch;
- ``0.0.0.0/0`` or ``::/0`` (or any set of networks covering a whole address
  family): every hop is trusted, so the leftmost ``X-Forwarded-For`` entry,
  which the caller writes, is taken as its address;
- a base URL that is not an origin (``http://`` to a public name, a path,
  credentials, garbage): it is published verbatim as the issuer and in the
  MCP ``WWW-Authenticate`` challenge.

Each of those is a configuration nobody means, so the settings refuse it and
the process does not start, rather than running with a protection that is
there only by name. Empty stays valid: it is the "off" the gate already
handles, and every deployment that has not set these yet keeps starting.

Plain ``http`` is accepted for a loopback host only (a development stack has
no certificate); anything reachable from outside is ``https``.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_LOOPBACK_NAMES = frozenset({"localhost"})

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_public_base_url(value: str) -> str:
    """The base URL normalised to ``scheme://host[:port]``, or ``""``.

    Raises ``ValueError`` for anything that is not an origin."""
    raw = value.strip()
    if not raw:
        return ""
    name = "MYCELIUM_PUBLIC_BASE_URL"
    if any(c.isspace() or c in "\"'<>\\" for c in raw) or not raw.isascii():
        raise ValueError(f"{name} must be an origin such as https://mycelium.example: {raw!r}")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{name} is not a URL: {raw!r}") from exc
    host = parts.hostname or ""
    if parts.scheme not in ("https", "http") or not host:
        raise ValueError(f"{name} must be an https origin: {raw!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise ValueError(f"{name} must be an origin, with no path, query or fragment: {raw!r}")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError(f"{name} must be https unless the host is loopback: {raw!r}")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc.lower()}"


def _parse_network(entry: str) -> IPNetwork:
    try:
        return ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(
            f"MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES: {entry!r} is not an address or CIDR"
        ) from exc


def validate_trusted_proxies(entries: list[str]) -> list[str]:
    """The proxy networks in canonical CIDR form, or ``[]``.

    Raises ``ValueError`` for an entry that does not parse, or for a list that
    trusts a whole address family (``0.0.0.0/0``, ``::/0``, or networks that
    add up to one of them, such as ``0.0.0.0/1`` with ``128.0.0.0/1``)."""
    nets = [_parse_network(e) for e in entries]
    for family in (ipaddress.IPv4Network, ipaddress.IPv6Network):
        same = [n for n in nets if isinstance(n, family)]
        if any(c.prefixlen == 0 for c in ipaddress.collapse_addresses(same)):  # type: ignore[type-var]
            raise ValueError(
                "MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES trusts every address of a family: "
                "every caller could then write its own address into X-Forwarded-For. "
                "List the proxy hops in front of the backend."
            )
    return [str(n) for n in nets]


__all__ = ["validate_public_base_url", "validate_trusted_proxies"]
