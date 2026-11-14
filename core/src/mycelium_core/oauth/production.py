"""When the open OAuth strategies may run (ADR-0069).

Dynamic Client Registration and Client ID Metadata Documents act on behalf of
a caller holding nothing: one writes a row, the other makes an outbound
fetch. Both are bounded per origin, and both publish URLs built from the
issuer. Two settings make those protections real, in ANY environment:

``MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES``  without it, behind a proxy chain, no
    request's origin can be attributed, so every per-origin bound is off.
``MYCELIUM_PUBLIC_BASE_URL``  without it, the issuer is built from the
    request's Host, which the caller chooses (nginx forwards ``$host``).

The two strategies run ONLY when both are set, and a value that is set but
wrong (a proxy entry that does not parse, a catch-all network, a base URL that
is not an https origin) stops the process at startup (``oauth.deployment``).
The gate does not try to recognise production: an earlier version did, by ``MYCELIUM_ENV``, which
defaults to ``dev`` and that nothing else reads, so a production deployment
that never set it got the strategies on and unprotected -- it failed open
(third security review, 2026-11-13). Now the safe state needs no recognition.

The only way to run them without the two settings is an explicit opt-out,
``MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK=true``, named for what it costs: for
a development stack or a test suite with no proxy chain to anchor and no
public name to pin. It is IGNORED, with an error at startup, where the
environment is declared production: there it would be the exposure the two
settings exist to close.

Off means: absent from the discovery document, ``/register`` answers 404,
https client ids are refused. Everything else -- the app, assistant secrets,
hand-registered clients -- is unaffected, which is why this switches two
strategies off rather than refusing to start.
"""

from __future__ import annotations

from mycelium_core.config import Settings

_PRODUCTION = frozenset({"production", "prod"})


def is_production(settings: Settings) -> bool:
    """Declared production. Used ONLY to refuse the opt-out: the gate itself
    never depends on recognising the environment."""
    return settings.env.strip().lower() in _PRODUCTION


def missing_for_open_endpoints(settings: Settings) -> list[str]:
    """The settings the open strategies need, that are missing."""
    missing: list[str] = []
    if not settings.public_base_url.strip():
        missing.append("MYCELIUM_PUBLIC_BASE_URL")
    if not settings.issuer_key_trusted_proxies:
        missing.append("MYCELIUM_ISSUER_KEY_TRUSTED_PROXIES")
    return missing


def opt_out_honoured(settings: Settings) -> bool:
    return settings.oauth_unattributed_origins_ok and not is_production(settings)


def open_endpoints_allowed(settings: Settings) -> bool:
    return not missing_for_open_endpoints(settings) or opt_out_honoured(settings)


def startup_problems(settings: Settings) -> list[str]:
    """What to log, as errors, when the app starts. Empty when the open
    strategies run as configured."""
    problems: list[str] = []
    missing = missing_for_open_endpoints(settings)
    if settings.oauth_unattributed_origins_ok and is_production(settings):
        problems.append("MYCELIUM_OAUTH_UNATTRIBUTED_ORIGINS_OK is ignored in production")
    if missing and not opt_out_honoured(settings):
        problems.append(
            "OAuth dynamic client registration and client metadata documents are "
            f"DISABLED: missing {', '.join(missing)} (ADR-0069, production configuration)"
        )
    return problems


__all__ = [
    "is_production",
    "missing_for_open_endpoints",
    "open_endpoints_allowed",
    "opt_out_honoured",
    "startup_problems",
]
