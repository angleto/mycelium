"""``flow client`` and ``flow project`` — list and inspect."""

from __future__ import annotations

import typer

from flow_cli.cmds._common import client, get_json, short_id
from flow_cli.ui import emit_json, emit_table, info, json_mode, out

clients_app = typer.Typer(no_args_is_help=True, help="Clients (tag kind=client).")
projects_app = typer.Typer(no_args_is_help=True, help="Projects (tag kind=project).")


@clients_app.command("list")
def client_list() -> None:
    """List workspace clients (includes the auto-provisioned 'Personal')."""
    with client() as c:
        rows = get_json(c.get("/clients"))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no clients.[/dim]")
        return
    emit_table(
        None,
        ["id", "name", "rate", "currency", "billable"],
        [
            (
                short_id(r.get("id") or r.get("tag_id")),
                r.get("name"),
                r.get("hourly_rate") or "",
                r.get("currency") or "",
                r.get("default_billable"),
            )
            for r in rows
        ],
    )


@clients_app.command()
def show(name_or_id: str = typer.Argument(...)) -> None:
    """Show a client's billing details."""
    with client() as c:
        rows = get_json(c.get("/clients"))
    match = next(
        (
            r
            for r in rows
            if str(r.get("id") or r.get("tag_id", "")).startswith(name_or_id)
            or str(r.get("name", "")).lower() == name_or_id.lower()
        ),
        None,
    )
    if match is None:
        from flow_cli.http import CLIError

        raise CLIError(f"no client matches '{name_or_id}'.")
    if json_mode():
        emit_json(match)
        return
    out().print(
        f"[bold]{match.get('name')}[/bold]  ({short_id(match.get('id') or match.get('tag_id'))})"
    )
    for k in (
        "legal_name",
        "tax_code",
        "address",
        "city",
        "province",
        "postal_code",
        "country",
        "pec",
        "sdi_code",
        "hourly_rate",
        "currency",
        "default_billable",
        "default_payment_terms_days",
        "default_payment_conditions_code",
        "default_payment_method_code",
        "invoice_language",
    ):
        v = match.get(k)
        if v not in (None, ""):
            out().print(f"  {k}: {v}")


@projects_app.command("list")
def project_list(
    client_name: str | None = typer.Option(
        None, "--client", "-c", help="Filter by client name or UUID."
    ),
) -> None:
    """List workspace projects, optionally narrowed to one client."""
    params: dict[str, str] = {}
    with client() as c:
        if client_name:
            clients = get_json(c.get("/clients"))
            match = next(
                (
                    r
                    for r in clients
                    if str(r.get("id") or r.get("tag_id", "")).startswith(client_name)
                    or str(r.get("name", "")).lower() == client_name.lower()
                ),
                None,
            )
            if match:
                params["client_tag_id"] = str(match.get("id") or match.get("tag_id"))
        rows = get_json(c.get("/projects", params=params))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no projects.[/dim]")
        return
    emit_table(
        None,
        ["id", "name", "client", "budget", "status"],
        [
            (
                short_id(r.get("id") or r.get("tag_id")),
                r.get("name"),
                short_id(r.get("client_tag_id")),
                r.get("budget") or "",
                r.get("status") or "",
            )
            for r in rows
        ],
    )
