"""What the consent page preselects (ADR-0069).

Found by driving the official MCP SDK client: it requests EVERY key in the
advertised ``scopes_supported``, and the page preselected whatever a client
asked for when all of it was catalogue keys. That ticked the dangerous keys
(deletions among them) without the confirmation the picker asks for when a
person ticks one by hand. A client's request may preselect read and write
keys; a dangerous one is only ever ticked by the person.

Observed failing before the fix (2026-11-13): ``delete:tasks`` came back
preselected.
"""

from __future__ import annotations

from mycelium_core.mcp_scopes import DEFAULT_SCOPES, SCOPE_CATALOG
from mycelium_core.services.oauth_consent import suggested_scope

_DANGER = {s.key for s in SCOPE_CATALOG if s.category == "danger"}


def test_a_request_for_everything_preselects_nothing_dangerous() -> None:
    everything = " ".join(s.key for s in SCOPE_CATALOG)
    picked = suggested_scope(everything)
    assert not (set(picked) & _DANGER)
    assert "tasks:read" in picked


def test_a_request_for_only_dangerous_keys_falls_back_to_the_default() -> None:
    assert suggested_scope("delete:tasks") == list(DEFAULT_SCOPES)


def test_unknown_keys_fall_back_to_the_default() -> None:
    assert suggested_scope("openid mcp") == list(DEFAULT_SCOPES)
    assert suggested_scope(None) == list(DEFAULT_SCOPES)
    assert suggested_scope("tasks:read notes:read") == ["tasks:read", "notes:read"]
