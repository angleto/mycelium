"""The network origin an unauthenticated OAuth request is counted against.

The open endpoints (registration, /authorize, the metadata-document fetch)
are bounded per origin, and an origin is not an address. One IPv4 address is
one origin. An IPv6 address is not: a single host is routinely handed a whole
/64 and can rotate through it at will, and a single subscriber is routinely
handed a /56 or a /48 -- 256 or 65536 /64s -- so counting per /64 still gave
one caller tens of thousands of budgets (second security review,
2026-11-13). IPv6 is therefore counted per /48, the largest prefix a single
site is normally assigned. The cost is the IPv4 one, made explicit: everyone
behind one site prefix shares a budget, as everyone behind one NAT address
does. A second, finer bound per /64 was rejected: it would only add budget,
not take any away. An IPv4-mapped IPv6 address is the IPv4 address it
carries.

``None`` means the server could not attribute the request (a forwarding
chain with no configured trusted proxy, see ``deps._resolve_issuer_client_ip``)
and such requests are only subject to the global bounds -- which evict
rather than refuse, so an unattributable flood cannot lock anybody out.
"""

from __future__ import annotations

import ipaddress


def origin_bucket(address: str | None) -> str | None:
    if address is None:
        return None
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 48), strict=False))
    return str(ip)


__all__ = ["origin_bucket"]
