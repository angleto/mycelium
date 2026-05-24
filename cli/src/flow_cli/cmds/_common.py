"""Shared command helpers: client construction, resolvers, response decode."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from flow_cli.config import Profile, load_config
from flow_cli.credentials import Credential, assert_secure, credentials_path, load_credential
from flow_cli.http import CLIError, authed_client, raise_for_response


def active_profile() -> tuple[str, Profile, Credential]:
    cfg = load_config()
    name = cfg.current_profile
    prof = cfg.profiles.get(name, Profile())
    assert_secure(credentials_path())
    cred = load_credential(name)
    if cred is None:
        raise CLIError(
            f"profile '{name}' is not logged in.",
            hint="Run: flow auth login",
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


def get_json(resp: httpx.Response) -> Any:
    raise_for_response(resp)
    return resp.json()


def resolve_id(c: httpx.Client, partial: str, *, endpoint: str, kind: str) -> str:
    """Accept either a full UUID or a unique short prefix.

    ``endpoint`` is the list endpoint (e.g. ``/tasks``, ``/notes``); we
    add ``include_archived=true`` so a recently archived row is still
    reachable. ``kind`` is the human label used in error messages.
    """
    if len(partial) >= 32:
        return partial
    rows = get_json(c.get(endpoint, params={"include_archived": "true"}))
    if not isinstance(rows, list):
        raise CLIError(f"unexpected listing payload at {endpoint}")
    matches = [r for r in rows if str(r.get("id", "")).startswith(partial)]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise CLIError(f"no {kind} matches '{partial}'.")
    raise CLIError(
        f"ambiguous {kind} prefix '{partial}' ({len(matches)} matches). Use more characters."
    )
