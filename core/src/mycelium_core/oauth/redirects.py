"""Where an authorization code may be sent: one policy, every strategy.

A code goes to whatever URI this policy accepts for the client asking, so
this is the component that decides where a credential can end up. Every
client strategy uses it -- registered clients when they register, metadata-
document clients when their document is read, assistant-secret clients at
/authorize -- and none of them carries a rule of its own.

**What is accepted.**

- ``https://`` with a host.
- ``http://`` on a loopback host (``127.0.0.1``, ``localhost``, ``[::1]``),
  for native apps that receive the redirect on a local port (RFC 8252 §7.3).
- A private-use scheme (RFC 8252 §7.1: ``cursor://...``, ``vscode://...``,
  ``com.example.app:/cb``) ONLY if the deployment lists the scheme in
  ``oauth_private_use_redirect_schemes``. Empty by default: such a scheme is
  claimed by whichever app registered it on the device, so allowing one is a
  decision about which native apps to trust with a code, made by a person
  and not by a registrant.

**What is always refused**, whatever is configured: ``javascript:``,
``data:``, ``file:``, ``about:``, ``blob:``, ``vbscript:`` (they execute or
read locally instead of delivering to a party), ``http`` on a non-loopback
host (the code would cross the network in clear), any fragment (RFC 6749
§3.1.2) and any userinfo -- ``https://claude.ai@evil.example/cb`` reads as one
host and goes to another, and the consent page's defence is showing a host
the person can trust. Listing one of these schemes in the allowlist is a
configuration error the service refuses to start with.

**Matching** is exact string equality, with one exception the RFC requires:
an ``http`` loopback URI matches whatever port the client listens on today.
Scheme, host, path and query are still compared exactly. A registered URI the
policy no longer accepts (its scheme was removed from the allowlist) is not
honoured, so withdrawing a scheme takes effect on clients already registered.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mycelium_core.config import Settings

MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2000

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Schemes that execute, read locally or render instead of delivering a code
# to a party. Refused as redirect targets and refused in the allowlist.
HARD_REFUSED_SCHEMES = frozenset({"javascript", "data", "file", "about", "blob", "vbscript"})

# RFC 3986 §3.1: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ).
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*$")


class RedirectUriError(ValueError):
    """A redirect URI the policy refuses. ``uri`` names which one."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        super().__init__(uri)


def validate_scheme_allowlist(schemes: Iterable[str]) -> frozenset[str]:
    """Normalise the configured private-use schemes, refusing the ones no
    configuration may enable. Raised at startup by ``Settings``."""
    out: set[str] = set()
    for raw in schemes:
        scheme = str(raw).strip().lower()
        if not _SCHEME.match(scheme):
            raise ValueError(f"not a URI scheme: {raw!r}")
        if scheme in HARD_REFUSED_SCHEMES or scheme in {"http", "https"}:
            raise ValueError(f"scheme {scheme!r} cannot be allowlisted as a private-use redirect")
        out.add(scheme)
    return frozenset(out)


def _loopback(hostname: str | None) -> bool:
    return hostname is not None and hostname.lower() in LOOPBACK_HOSTS


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_DEFAULT_PORTS = {"https": 443, "http": 80}


def parse_authority(netloc: str) -> tuple[str, int | None]:
    """The host a browser would contact, lower-cased, and the explicit port.

    Strict on purpose: anything the WHATWG URL parser (what every browser
    runs) would read differently from ``urllib`` is refused rather than
    interpreted, because the consent page shows the host this function
    returns and the code goes to the host the browser picks. Measured
    against node's ``URL`` (v26.5.0):

    - a backslash is a path separator to a browser: ``https://evil.example``
      + backslash + ``.claude.ai/cb`` goes to ``evil.example``. Refused
      anywhere in the URI.
    - ``%`` in a host is decoded: ``%65vil.example`` is ``evil.example``.
    - non-ASCII is converted to punycode (``xn--...``), so a Cyrillic look-
      alike reads as one host and is another. Only ASCII is accepted; a
      host already in punycode is accepted and shown as punycode.
    - a numeric last label makes the whole host an IPv4 address in
      shorthand: ``127.1`` and ``0x7f.1`` are ``127.0.0.1``. Only canonical
      dotted quads are accepted.
    - a trailing dot, an empty host, userinfo, ``_`` and labels that start
      or end with ``-`` are refused.
    """
    if not netloc or "@" in netloc or "%" in netloc or "\\" in netloc:
        raise ValueError(netloc)
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            raise ValueError(netloc)
        host = netloc[1:end]
        ipaddress.IPv6Address(host)  # raises
        rest = netloc[end + 1 :]
        host = f"[{ipaddress.IPv6Address(host).compressed}]"
    else:
        host, sep, rest = netloc.partition(":")
        rest = sep + rest
        host = host.lower()
        labels = host.split(".")
        if not host or len(host) > 253 or any(not _LABEL.match(lb) for lb in labels):
            raise ValueError(netloc)
        if _NUMERIC_LABEL.match(labels[-1]):
            if len(labels) != 4 or any(
                not lb.isdigit() or (len(lb) > 1 and lb[0] == "0") or int(lb) > 255 for lb in labels
            ):
                raise ValueError(netloc)
    port: int | None = None
    if rest:
        digits = rest[1:] if rest.startswith(":") else ""
        if not digits.isdigit() or not 0 < int(digits) <= 65535:
            raise ValueError(netloc)
        port = int(digits)
    return host, port


@dataclass(frozen=True)
class RedirectPolicy:
    private_use_schemes: frozenset[str] = frozenset()

    def validate(self, uri: object) -> str:
        """Return ``uri`` unchanged if accepted, else raise. Unchanged, not
        normalised: matching is by exact string, so what is stored must be
        exactly what the client sends."""
        if not isinstance(uri, str) or not uri or len(uri) > MAX_REDIRECT_URI_LENGTH:
            raise RedirectUriError(str(uri)[:200])
        # Whitespace and control characters: browsers strip some of them
        # before resolving, which is how a string that reads as one host is
        # followed to another. Refused rather than reasoned about.
        if any(ord(ch) <= 0x20 or ord(ch) >= 0x7F for ch in uri) or "\\" in uri:
            # Controls and whitespace are stripped by browsers before they
            # resolve a URL; non-ASCII is rewritten to punycode; a backslash
            # is a path separator. Each is how a string that reads as one
            # host is followed to another, so each is refused outright.
            raise RedirectUriError(uri)
        try:
            parts = urlsplit(uri)
        except ValueError:
            raise RedirectUriError(uri) from None
        scheme = parts.scheme.lower()
        if not scheme or scheme in HARD_REFUSED_SCHEMES:
            raise RedirectUriError(uri)
        if parts.fragment or "#" in uri:
            raise RedirectUriError(uri)
        host: str | None = None
        if parts.netloc or scheme in _DEFAULT_PORTS:
            try:
                host, _port = parse_authority(parts.netloc)
            except ValueError:
                raise RedirectUriError(uri) from None
        if scheme == "https":
            return uri
        if scheme == "http":
            if _loopback((host or "").strip("[]")):
                return uri
            raise RedirectUriError(uri)
        if scheme in self.private_use_schemes:
            # ``cursor://host/path`` and ``com.example.app:/path`` are both
            # RFC 8252 shapes; the host is optional, a path is not.
            if not (parts.netloc or parts.path):
                raise RedirectUriError(uri)
            return uri
        raise RedirectUriError(uri)

    def validate_list(self, uris: object) -> list[str]:
        """The list as a whole: present, bounded, every entry accepted,
        duplicates dropped in order."""
        if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RedirectUriError("")
        out: list[str] = []
        for uri in uris:
            accepted = self.validate(uri)
            if accepted not in out:
                out.append(accepted)
        return out

    def matches(self, registered: str, presented: str) -> bool:
        """Exact match, except that an http loopback URI ignores the port."""
        if secrets.compare_digest(registered.encode(), presented.encode()):
            return True
        try:
            reg = urlsplit(registered)
            pre = urlsplit(presented)
            _ = pre.port
        except ValueError:
            return False
        if reg.scheme != "http" or pre.scheme != "http":
            return False
        if not _loopback(reg.hostname) or not _loopback(pre.hostname):
            return False
        if (reg.hostname or "").lower() != (pre.hostname or "").lower():
            return False
        if pre.username is not None or pre.password is not None or pre.fragment:
            return False
        return reg.path == pre.path and reg.query == pre.query

    def accepts(self, registered: Sequence[str] | None, presented: str) -> bool:
        """Whether a code may be sent to ``presented`` for a client whose
        registered URIs are ``registered``.

        ``None`` means the client registers no URIs and any URI the policy
        accepts will do: the assistant-secret clients, whose code is useless
        without the secret. Otherwise the presented URI must itself pass the
        policy AND match a registered one."""
        try:
            self.validate(presented)
        except RedirectUriError:
            return False
        if registered is None:
            return True
        return any(self.matches(r, presented) for r in registered)


def redirect_target(uri: str) -> str:
    """Scheme and host, as the consent page shows it: the host a browser
    contacts, lower-cased, with the port only where it is not the scheme's
    default (as a browser shows it). The port is kept otherwise, because
    ``localhost:3000`` and ``localhost:8080`` are different programs; the
    path is dropped, because it is not where the trust decision lies. Call
    it on a URI the policy accepted."""
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if not parts.netloc:
        return f"{scheme}:"
    host, port = parse_authority(parts.netloc)
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def policy_from_settings(settings: Settings) -> RedirectPolicy:
    """The deployment's policy. ``Settings`` already refused a bad allowlist
    at startup (it calls ``validate_scheme_allowlist``); this normalises it
    again because the call is free and the type says frozenset."""
    return RedirectPolicy(
        private_use_schemes=validate_scheme_allowlist(settings.oauth_private_use_redirect_schemes)
    )


__all__ = [
    "HARD_REFUSED_SCHEMES",
    "LOOPBACK_HOSTS",
    "MAX_REDIRECT_URIS",
    "MAX_REDIRECT_URI_LENGTH",
    "RedirectPolicy",
    "RedirectUriError",
    "parse_authority",
    "policy_from_settings",
    "redirect_target",
    "validate_scheme_allowlist",
]
