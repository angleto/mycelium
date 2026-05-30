"""``flow invoice`` — draft, line, transmit (SdICoop) and inspect invoices.

Mirrors the MCP ``create_invoice`` / ``add_invoice_line`` / ``transmit_invoice``
tools over the same REST surface (``/invoices*``). ``transmit`` is the
SdICoop send: it allocates the progressive number, builds + XSD-validates the
FatturaPA XML and POSTs it through the accredited channel, returning the
``IdentificativoSdI`` SdI assigns. The destinatario / PEC come from the client
profile, so a transmitted invoice needs a client with one of those set.
"""

from __future__ import annotations

import typer

from flow_cli.cmds._common import client, get_json, resolve_id, short_id
from flow_cli.http import CLIError
from flow_cli.ui import emit_json, emit_table, info, json_mode, out

app = typer.Typer(no_args_is_help=True, help="Invoices: draft, transmit (SdICoop), inspect.")


def _resolve_client_tag(c: object, name_or_id: str) -> str:
    """Accept a client name or a (short) UUID, return its tag id."""
    rows = get_json(c.get("/clients"))  # type: ignore[attr-defined]
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
        raise CLIError(f"no client matches '{name_or_id}'.")
    return str(match.get("id") or match.get("tag_id"))


def _show_invoice(inv: dict) -> None:
    if json_mode():
        emit_json(inv)
        return
    num = f"{inv.get('series')}-{inv.get('number')}" if inv.get("number") else "(draft)"
    out().print(f"[bold]{num}[/bold]  ({short_id(inv.get('id'))})  {inv.get('state')}")
    for k in (
        "document_type",
        "total",
        "currency",
        "identificativo_sdi",
        "sdi_status",
        "conservation_status",
        "payment_status",
        "purpose",
    ):
        v = inv.get(k)
        if v not in (None, ""):
            out().print(f"  {k}: {v}")


@app.command("list")
def invoice_list() -> None:
    """List invoices (draft + emitted)."""
    with client() as c:
        rows = get_json(c.get("/invoices"))
    if json_mode():
        emit_json(rows)
        return
    if not rows:
        info("[dim]no invoices.[/dim]")
        return
    emit_table(
        None,
        ["id", "number", "state", "total", "sdi", "id_sdi"],
        [
            (
                short_id(r.get("id")),
                f"{r.get('series')}-{r.get('number')}" if r.get("number") else "draft",
                r.get("state"),
                r.get("total"),
                r.get("sdi_status"),
                r.get("identificativo_sdi") or "",
            )
            for r in rows
        ],
    )


@app.command()
def show(invoice_id: str = typer.Argument(..., help="Invoice id (full or short prefix).")) -> None:
    """Show one invoice (state, totals, IdentificativoSdI, sdi_status)."""
    with client() as c:
        iid = resolve_id(c, invoice_id, endpoint="/invoices", kind="invoice")
        _show_invoice(get_json(c.get(f"/invoices/{iid}")))


@app.command()
def create(
    client_ref: str = typer.Option(..., "--client", "-c", help="Client name or UUID."),
    series: str | None = typer.Option(None, "--series", help="Override the per-client sezionale."),
    purpose: str | None = typer.Option(None, "--purpose", help="Document purpose."),
) -> None:
    """Create a draft invoice for a client."""
    with client() as c:
        tag = _resolve_client_tag(c, client_ref)
        body: dict[str, object] = {"client_tag_id": tag}
        if series:
            body["series"] = series
        if purpose:
            body["purpose"] = purpose
        _show_invoice(get_json(c.post("/invoices", json=body)))


@app.command()
def line(
    invoice_id: str = typer.Argument(..., help="Invoice id (full or short prefix)."),
    description: str = typer.Argument(..., help="Line description."),
    price: float = typer.Option(..., "--price", "-p", help="Unit price."),
    qty: float = typer.Option(1.0, "--qty", "-q", help="Quantity."),
    vat: float | None = typer.Option(
        None, "--vat", help="VAT rate %% (default: resolved from issuer regime)."
    ),
    vat_nature: str | None = typer.Option(
        None, "--vat_nature", help="FatturaPA Natura (e.g. N2.2)."
    ),
) -> None:
    """Add a line to a draft invoice."""
    with client() as c:
        iid = resolve_id(c, invoice_id, endpoint="/invoices", kind="invoice")
        body: dict[str, object] = {"description": description, "unit_price": price, "quantity": qty}
        if vat is not None:
            body["vat_rate"] = vat
        if vat_nature:
            body["vat_nature"] = vat_nature
        row = get_json(c.post(f"/invoices/{iid}/lines", json=body))
    if json_mode():
        emit_json(row)
        return
    out().print(
        f"[green]added[/green] line {row.get('line_no')}: {row.get('description')} "
        f"x{row.get('quantity')} @ {row.get('unit_price')}"
    )


@app.command()
def transmit(
    invoice_id: str = typer.Argument(..., help="Invoice id (full or short prefix)."),
    progressivo: str | None = typer.Option(
        None, "--progressivo", help="Override the file ProgressivoInvio (advanced)."
    ),
) -> None:
    """Transmit a draft through the SdICoop channel; prints the IdentificativoSdI."""
    with client() as c:
        iid = resolve_id(c, invoice_id, endpoint="/invoices", kind="invoice")
        body: dict[str, object] = {}
        if progressivo:
            body["progressivo"] = progressivo
        inv = get_json(c.post(f"/invoices/{iid}/transmit", json=body))
    if not json_mode():
        out().print(
            f"[green]transmitted[/green] {inv.get('series')}-{inv.get('number')} -> "
            f"IdentificativoSdI [bold]{inv.get('identificativo_sdi')}[/bold]"
        )
    _show_invoice(inv)


@app.command()
def xml(invoice_id: str = typer.Argument(..., help="Invoice id (full or short prefix).")) -> None:
    """Print the FatturaPA XML (transmitted = frozen, draft = live preview)."""
    with client() as c:
        iid = resolve_id(c, invoice_id, endpoint="/invoices", kind="invoice")
        row = get_json(c.get(f"/invoices/{iid}/xml"))
    if json_mode():
        emit_json(row)
        return
    out().print(row.get("xml", ""))
