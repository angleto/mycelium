"""Pure unit smoke tests (no DB, no settings)."""

from __future__ import annotations

import pytest

from mycelium_core.errors import ForbiddenError
from mycelium_core.i18n import MessageCode, render
from mycelium_core.models.membership import Role
from mycelium_core.security import hash_password, verify_password
from mycelium_core.services.rbac import ensure_role


def test_render_basic_and_params() -> None:
    assert render(MessageCode.AUTH_INVALID_CREDENTIALS) == "Invalid credentials"
    assert (
        render(
            MessageCode.RBAC_ROLE_INSUFFICIENT,
            "en",
            current="member",
            minimum="admin",
        )
        == "Role member is insufficient, requires >= admin"
    )


def test_render_locale_fallback() -> None:
    assert render(MessageCode.ORG_NOT_FOUND, "xx") == "Workspace not found"


def test_ensure_role_rank() -> None:
    ensure_role(Role.owner, Role.admin)
    with pytest.raises(ForbiddenError):
        ensure_role(Role.member, Role.admin)


def test_password_hash_roundtrip() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)
