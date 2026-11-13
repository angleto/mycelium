"""The origin this deployment is reached at, as the outside world sees it.

Three surfaces publish absolute URLs built from it, and they must agree to
the character or an OAuth client stops halfway through discovery:

- the authorization-server and protected-resource metadata (the API's
  ``routers/oauth.py``), which name the issuer, the endpoints and the MCP
  resource;
- the ``WWW-Authenticate`` challenge the MCP transport sends with a 401
  (``mycelium_mcp.server_http``), which names where the resource metadata
  lives -- the first thing a client reads and the URL it starts from;
- the redirect ``/authorize`` sends a browser to the consent page.

It lives in core because two of those are in different packages and the MCP
package must not import the API one: the API mounts the MCP app, so the
reverse import would be a cycle. One function, read by both.

**In production, set ``MYCELIUM_PUBLIC_BASE_URL``.** It wins over every
header, and it is the ONLY thing that pins this server's name. Without it the
origin is derived from ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` /
``Host``; nginx forwards ``$host``, which is the client's own Host header, so
a caller choosing its Host chooses the name the server describes itself
under. Without the setting in production the open OAuth strategies are
switched off (``oauth.production``). Each header is still checked against a
strict grammar -- proto is ``http`` or ``https``, host is a hostname or IP
literal with an optional port, matched in full -- and ignored if it fails, so
a header can never carry a quote, a space or a line break into the
``WWW-Authenticate`` challenge or the metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from mycelium_core.config import get_settings

_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?)*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?"
)


def _host(value: str | None) -> str | None:
    # fullmatch, not match: ``$`` also matches before a trailing newline, so
    # ``match`` accepted "evil.com\n" (second security review, 2026-11-13).
    if value and len(value) <= 260 and _HOST.fullmatch(value):
        return value
    return None


def issuer(headers: Mapping[str, str], *, scheme: str, netloc: str) -> str:
    """The issuer URL, no trailing slash.

    ``headers`` is the request's header mapping (case-insensitive in both
    Starlette and httpx); ``scheme`` / ``netloc`` are the request URL's own,
    used only when nothing well-formed was forwarded."""
    override = get_settings().public_base_url.strip()
    if override:
        return override.rstrip("/")
    forwarded_proto = headers.get("x-forwarded-proto")
    proto = forwarded_proto if forwarded_proto in ("http", "https") else scheme
    if proto not in ("http", "https"):
        proto = "https"
    host = (
        _host(headers.get("x-forwarded-host"))
        or _host(headers.get("host"))
        or _host(netloc)
        or "localhost"
    )
    return f"{proto}://{host}"


def mcp_resource(issuer_url: str) -> str:
    """The MCP endpoint as an OAuth protected resource (RFC 8707 / 9728)."""
    return f"{issuer_url}/mcp"


def protected_resource_metadata_url(issuer_url: str) -> str:
    """Where the MCP resource's metadata lives. RFC 9728 inserts the
    well-known segment between the host and the resource path, so a
    resource at ``/mcp`` is described at ``/.well-known/oauth-protected-
    resource/mcp``."""
    return f"{issuer_url}/.well-known/oauth-protected-resource/mcp"


def bearer_challenge(issuer_url: str) -> str:
    """The ``WWW-Authenticate`` value for an MCP 401 (RFC 9728 §5.1). The URL
    goes inside a quoted-string, so a quote or backslash in it is escaped;
    ``issuer`` already never produces one, and this keeps the header safe
    even if a future caller passes something else."""
    url = protected_resource_metadata_url(issuer_url)
    escaped = url.replace("\\", "\\\\").replace('"', '\\"')
    return f'Bearer resource_metadata="{escaped}"'


__all__ = ["bearer_challenge", "issuer", "mcp_resource", "protected_resource_metadata_url"]
