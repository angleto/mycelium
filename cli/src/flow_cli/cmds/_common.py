"""Shared command helpers: client construction from the active profile."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from flow_cli.config import Profile, load_config
from flow_cli.credentials import Credential, assert_secure, credentials_path, load_credential
from flow_cli.http import CLIError, authed_client


def active_profile() -> tuple[str, Profile, Credential]:
    cfg = load_config()
    name = cfg.current_profile
    prof = cfg.profiles.get(name, Profile())
    assert_secure(credentials_path())
    cred = load_credential(name)
    if cred is None:
        raise CLIError(
            f"profile '{name}' is not logged in.",
        )
    return name, prof, cred


@contextmanager
def client(*, role: str | None = None, admin_mode: bool = False) -> Iterator[httpx.Client]:
    _, prof, cred = active_profile()
    with authed_client(prof.base_url, cred, role=role, admin_mode=admin_mode) as c:
        yield c


def short_id(uid: str | None) -> str:
    if not uid:
        return ""
    return str(uid).split("-")[0]
