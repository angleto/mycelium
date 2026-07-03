"""Structured security events (task d3dd69c3, issuer-key ops hardening).

One dedicated logger (``mycelium.security``) emitting single-line JSON with a
stable ``event`` name, so the log pipeline (kubectl logs / Loki / whatever
ships the pod logs) can filter and threshold WITHOUT any in-app metrics
infrastructure. The v1 alerting boundary is deliberate: the app emits the
signal, the log stack owns the thresholds (documented, with query examples,
in docs/runbooks/issuer-key-pepper.md).

Event names are part of the operational contract -- treat them like message
codes, never rename casually:

- ``issuer_key.auth_failed``          credential invalid (unknown / revoked /
                                      expired / grace-expired), collapsed
                                      like the 401 -- carries only source ip
- ``issuer_key.ip_denied``            valid credential, source IP outside the
                                      key's allowlist
- ``issuer_key.ip_unresolved``        valid credential on an allowlisted key
                                      but the source could not be attributed
                                      to a trustworthy value (the trusted-proxy
                                      chain is not configured) -- fail-closed
                                      deny; almost always a MISCONFIG signal,
                                      not an attack
- ``issuer_key.grace_secret_used``    the PREVIOUS (rotation-grace) secret
                                      authenticated
- ``issuer_key.previous_pepper_used`` a key authenticated via the PREVIOUS
                                      pepper (rotation window progress; after
                                      a compromise, evidence the old pepper
                                      is still live)
- ``issuer_key.dormant_key_used``     a key silent for >= issuer_key_dormant_days
                                      woke up (stolen-credential signal)
- ``issuer_key.rate_limited``         a key tripped the per-class 429 budget

Values must be log-safe: never a raw secret, never a full key -- key identity
is the non-secret ``key_id`` / ``key_public_id``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("mycelium.security")


def emit(event: str, **fields: Any) -> None:
    """Emit one structured security event as single-line JSON at WARNING.

    WARNING (not INFO) on purpose: these are the lines an operator alerts
    on, and production log levels commonly drop INFO.
    """
    payload = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    log.warning("security_event %s", json.dumps(payload, sort_keys=True, default=str))
