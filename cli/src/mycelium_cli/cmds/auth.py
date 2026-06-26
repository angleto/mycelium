"""``mycelium auth`` — login (mint PAT), logout, whoami, status."""

from __future__ import annotations

import datetime as dt
import socket
from typing import Any

import typer

from mycelium_cli.cmds.mfa import app as mfa_app
from mycelium_cli.config import Profile, load_config, save_config
from mycelium_cli.credentials import (
    Credential,
    credentials_path,
    delete_credential,
    load_credential,
    save_credential,
)
from mycelium_cli.http import (
    CLIError,
    MfaRequired,
    anon_client,
    authed_client,
    jwt_client,
    raise_for_response,
)
from mycelium_cli.ui import emit_json, fail, info, json_mode, out, success, warn

app = typer.Typer(no_args_is_help=True, help="Authenticate this machine to a Mycelium workspace.")
app.add_typer(mfa_app, name="mfa")


@app.command()
def login(
    base_url: str = typer.Option(
        "https://mycelium.xeno.garden/api",
        "--base-url",
        "-u",
        help="Mycelium API base URL (override for local dev / staging).",
    ),
    email: str | None = typer.Option(None, "--email", "-e", help="Account email."),
    password: str | None = typer.Option(
        None,
        "--password",
        "-p",
        help="Account password (omit for an interactive prompt).",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace ID or name to bind this profile to (defaults to the only/first one).",
    ),
    token_name: str | None = typer.Option(
        None,
        "--token-name",
        help="Name for the new agent token (defaults to mycelium-cli@<hostname>).",
    ),
    ttl_days: int = typer.Option(
        365,
        "--ttl-days",
        min=0,
        help="Token lifetime in days (0 = never expires).",
    ),
    profile: str = typer.Option("default", "--profile", help="CLI profile to write to."),
) -> None:
    """Interactive login. Trades email/password (+ TOTP if enabled) for a
    long-lived agent token (PAT) stored at ``~/.config/flow/credentials.toml``.
    Subsequent commands use that PAT.
    """
    email = email or typer.prompt("Email")
    password = password or typer.prompt("Password", hide_input=True)
    jwt = _login(base_url, email=email, password=password)

    # Resolve identity and the workspace this PAT will be bound to.
    with jwt_client(base_url, jwt) as c:
        me = _get_json(c.get("/auth/me"))
        workspaces = _get_json(c.get("/workspaces"))

    if not isinstance(workspaces, list) or not workspaces:
        raise CLIError("No workspaces available for this account.")
    selected = _pick_workspace(workspaces, workspace)
    info(
        f"Binding profile '[bold]{profile}[/bold]' to workspace "
        f"[cyan]{selected['name']}[/cyan] ({selected['id']})."
    )

    # Mint the agent token. Requires owner; we send X-Workspace-Role=owner
    # so the request runs at the owner tier when the user is owner. A
    # non-owner caller will get a clean ForbiddenError from the API.
    with jwt_client(base_url, jwt) as c:
        c.headers["X-Workspace-Id"] = str(selected["id"])
        c.headers["X-Workspace-Role"] = "owner"
        body: dict[str, Any] = {
            "name": token_name or f"mycelium-cli@{socket.gethostname()}",
            "scope": "cli",
        }
        if ttl_days > 0:
            body["ttl_days"] = ttl_days
        minted = _get_json(c.post("/agent-tokens", json=body))

    raw_token = minted.get("raw")
    if not isinstance(raw_token, str) or not raw_token:
        raise CLIError("Server did not return a raw token; refusing to save.")
    expires_at = _parse_dt(minted.get("expires_at"))

    cfg = load_config()
    cfg.current_profile = profile
    cfg.profiles[profile] = Profile(
        base_url=base_url,
        workspace_id=str(selected["id"]),
        workspace_name=str(selected["name"]),
    )
    save_config(cfg)
    save_credential(
        profile,
        Credential(
            token=raw_token,
            token_id=str(minted.get("id") or ""),
            user_id=str(me.get("user_id") or ""),
            email=str(me.get("email") or email),
            workspace_id=str(selected["id"]),
            expires_at=expires_at,
        ),
    )

    success(f"Logged in as [bold]{me.get('email', email)}[/bold].")
    info(f"Profile '{profile}' written to {credentials_path()} (mode 0600).")


@app.command()
def logout(
    profile: str = typer.Option("default", "--profile", help="CLI profile to clear."),
    revoke_remote: bool = typer.Option(
        True,
        "--revoke/--keep-remote",
        help="Also revoke the token server-side (recommended).",
    ),
) -> None:
    """Forget the saved PAT for this profile (and revoke it server-side)."""
    cred = load_credential(profile)
    if cred is None:
        warn(f"No credential stored for profile '{profile}'. Nothing to do.")
        raise typer.Exit(0)
    cfg = load_config()
    base_url = cfg.profiles.get(profile, Profile()).base_url
    if revoke_remote and cred.token_id:
        try:
            with authed_client(base_url, cred, role="owner") as c:
                resp = c.delete(f"/agent-tokens/{cred.token_id}")
                raise_for_response(resp)
        except CLIError as exc:
            warn(f"Server-side revoke failed ({exc}); removing local credential anyway.")
    delete_credential(profile)
    success(f"Profile '{profile}' logged out.")


@app.command()
def whoami(
    profile: str = typer.Option("default", "--profile"),
) -> None:
    """Print the identity behind the saved PAT (round-trips ``/auth/me`` is
    not applicable to agent tokens; we report the locally cached identity).
    """
    cred = load_credential(profile)
    if cred is None:
        fail(f"No credential stored for profile '{profile}'.", hint="Run: mycelium auth login")
        raise typer.Exit(1)
    if json_mode():
        emit_json(
            {
                "profile": profile,
                "email": cred.email,
                "user_id": cred.user_id,
                "workspace_id": cred.workspace_id,
                "token_id": cred.token_id,
                "expires_at": cred.expires_at,
            }
        )
        return
    out().print(
        f"[bold]{cred.email or '<unknown>'}[/bold] "
        f"(workspace [cyan]{cred.workspace_id or '<unset>'}[/cyan])"
    )


@app.command()
def status(profile: str = typer.Option("default", "--profile")) -> None:
    """Show whether this machine has a valid credential and can reach the API."""
    cfg = load_config()
    cred = load_credential(profile)
    prof = cfg.profiles.get(profile, Profile())
    if cred is None:
        fail(f"profile '{profile}' is not logged in.", hint="Run: mycelium auth login")
        raise typer.Exit(1)
    ok = True
    try:
        with authed_client(prof.base_url, cred) as c:
            resp = c.get("/buildinfo")
            raise_for_response(resp)
            buildinfo = resp.json()
    except CLIError as exc:
        ok = False
        buildinfo = {"error": str(exc)}
    payload = {
        "profile": profile,
        "base_url": prof.base_url,
        "workspace_id": prof.workspace_id,
        "workspace_name": prof.workspace_name,
        "email": cred.email,
        "reachable": ok,
        "server": buildinfo,
    }
    if json_mode():
        emit_json(payload)
        return
    info(
        f"profile: [bold]{profile}[/bold]\n"
        f"base_url: {prof.base_url}\n"
        f"workspace: {prof.workspace_name} ({prof.workspace_id})\n"
        f"email: {cred.email}\n"
        f"reachable: {'[green]yes[/green]' if ok else '[red]no[/red]'}"
    )
    if not ok:
        raise typer.Exit(1)


def _login(base_url: str, *, email: str, password: str) -> str:
    """Return a JWT, prompting for MFA only if the server demands it."""
    with anon_client(base_url) as c:
        try:
            resp = c.post("/auth/login", json={"email": email, "password": password})
            raise_for_response(resp)
            return str(resp.json()["token"])
        except MfaRequired:
            pass
        # MFA flow: combined endpoint, no separate "challenge".
        code = typer.prompt("TOTP code", hide_input=False).strip()
        resp = c.post(
            "/auth/login-mfa",
            json={"email": email, "password": password, "totp_code": code},
        )
        raise_for_response(resp)
        return str(resp.json()["token"])


def _pick_workspace(workspaces: list[dict[str, Any]], hint: str | None) -> dict[str, Any]:
    if hint:
        for w in workspaces:
            if str(w.get("id")) == hint or str(w.get("name")) == hint:
                return w
        raise CLIError(
            f"No workspace matches '{hint}'. "
            f"Available: {', '.join(str(w.get('name')) for w in workspaces)}"
        )
    if len(workspaces) == 1:
        return workspaces[0]
    # Multiple workspaces and no hint: ask, never auto-pick (would
    # silently bind the PAT to the wrong tenant on a multi-org account).
    typer.echo("Multiple workspaces found:")
    for i, w in enumerate(workspaces):
        typer.echo(f"  [{i}] {w.get('name')}  ({w.get('id')})  role={w.get('role')}")
    idx = int(typer.prompt("Pick one by index", default="0"))
    return workspaces[idx]


def _get_json(resp: Any) -> Any:
    raise_for_response(resp)
    return resp.json()


def _parse_dt(v: Any) -> dt.datetime | None:
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
