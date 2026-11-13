"""Shared command helpers: client construction, resolvers, response decode."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import typer

from mycelium_cli.config import Profile, load_config
from mycelium_cli.credentials import Credential, assert_secure, credentials_path, load_credential
from mycelium_cli.http import (
    CLIError,
    MfaRequired,
    anon_client,
    authed_client,
    jwt_client,
    raise_for_response,
)


def active_profile() -> tuple[str, Profile, Credential]:
    cfg = load_config()
    name = cfg.current_profile
    prof = cfg.profiles.get(name, Profile())
    assert_secure(credentials_path())
    cred = load_credential(name)
    if cred is None:
        raise CLIError(
            f"profile '{name}' is not logged in.",
            hint="Run: mycelium auth login",
        )
    return name, prof, cred


@contextmanager
def client(*, role: str | None = None, admin_mode: bool = False) -> Iterator[httpx.Client]:
    _, prof, cred = active_profile()
    with authed_client(prof.base_url, cred, role=role, admin_mode=admin_mode) as c:
        yield c


def login_jwt(base_url: str, *, email: str, password: str) -> str:
    """Trade a person's password for a session JWT, asking for the TOTP code
    only if the server demands it. Prompts go to stderr so ``--json`` output
    stays parseable."""
    with anon_client(base_url) as c:
        try:
            resp = c.post("/auth/login", json={"email": email, "password": password})
            raise_for_response(resp)
            return str(resp.json()["token"])
        except MfaRequired:
            pass
        # MFA flow: combined endpoint, no separate "challenge".
        code = typer.prompt("TOTP code", hide_input=False, err=True).strip()
        resp = c.post(
            "/auth/login-mfa",
            json={"email": email, "password": password, "totp_code": code},
        )
        raise_for_response(resp)
        return str(resp.json()["token"])


@contextmanager
def person_client(*, role: str | None = None) -> Iterator[httpx.Client]:
    """A client authenticated as the PERSON, for the commands reserved to one.

    The token ``mycelium auth login`` stores is an agent credential: every
    agent session on this machine can read the file it lives in, so the server
    refuses it the routes reserved to a person (account, MFA, workspaces,
    credentials). Those commands ask for the password here, run on the session
    it opens, and close that session on the way out, failure included.

    No flag or environment variable supplies the password, on purpose: a
    password that can be set once and read later is one every agent session
    can read too, which is the hole this closes."""
    _, prof, cred = active_profile()
    email = cred.email or typer.prompt("Email", err=True)
    password = typer.prompt(f"Password for {email}", hide_input=True, err=True)
    jwt = login_jwt(prof.base_url, email=email, password=password)
    with jwt_client(prof.base_url, jwt) as c:
        if cred.workspace_id:
            c.headers["X-Workspace-Id"] = cred.workspace_id
        if role:
            c.headers["X-Workspace-Role"] = role
        try:
            yield c
        finally:
            # Best effort: the session dies with its TTL anyway, and a failed
            # logout must not mask the command's own outcome.
            with contextlib.suppress(httpx.HTTPError):
                c.post("/auth/logout")


def short_id(uid: str | None) -> str:
    if not uid:
        return ""
    return str(uid).split("-")[0]


def attachment_markdown_ref(att: dict[str, Any]) -> str:
    """A paste-ready markdown reference to an uploaded attachment.

    Images become an inline embed (``![name](...)``), everything else a
    download link (``[name](...)``). The url is the bearer-auth
    ``/attachments/<id>/download`` route the SPA renderer and editor
    resolve through authFetch; it is never public. Same convention the
    web editor inserts, so a body written from the CLI/MCP renders
    identically in the app."""
    att_id = att.get("id", "")
    name = att.get("filename") or "file"
    mime = att.get("mime_type") or ""
    url = f"/attachments/{att_id}/download"
    bang = "!" if mime.startswith("image/") else ""
    # The label is a filename, i.e. user data: `Report ]final.pdf` used to
    # emit `[Report ]final.pdf](...)`, which parses as the link `[Report ]`
    # followed by literal text. Escape `\`, then the brackets, and collapse
    # newline runs (a blank line inside a label truncates the link).
    #
    # Duplicated on purpose from ``mycelium_core.markdown_inline`` and
    # ``web/src/lib/markdownInline.ts``: the CLI ships standalone, with no
    # dependency on core. Change one, change all three.
    label = name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    label = re.sub(r"\s*[\r\n]+\s*", " ", label)
    return f"{bang}[{label}]({url})"


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
