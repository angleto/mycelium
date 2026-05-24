"""``flow auth mfa`` — TOTP enrolment, status, disable.

Setup flow:
  1. ``flow auth mfa setup`` prints the otpauth URL, an ASCII QR code,
     and the base32 secret. Scan the QR with your authenticator (or
     paste the secret manually).
  2. ``flow auth mfa activate <CODE>`` confirms a fresh TOTP code and
     enables MFA; the response includes one-time backup codes — save
     them.
  3. From the next login onward, ``flow auth login`` will prompt for
     a TOTP code when the server answers 401 auth.mfa_required.
"""

from __future__ import annotations

import base64
import io
import sys

import typer

from flow_cli.cmds._common import client, get_json
from flow_cli.ui import emit_json, json_mode, out, success

app = typer.Typer(no_args_is_help=True, help="Multi-factor (TOTP) enrolment + disable.")


@app.command()
def status() -> None:
    """Show whether MFA is enabled and how many backup codes remain."""
    with client() as c:
        st = get_json(c.get("/mfa/status"))
    if json_mode():
        emit_json(st)
        return
    enabled = st.get("enabled")
    out().print(
        f"MFA: [{'bold green' if enabled else 'red'}]"
        f"{'enabled' if enabled else 'disabled'}[/]"
        + (f" since {st.get('enabled_at')}" if enabled and st.get("enabled_at") else "")
    )
    if st.get("pending"):
        out().print("[yellow]pending activation[/yellow] (run: flow auth mfa activate <CODE>)")
    if enabled:
        out().print(f"backup codes remaining: {st.get('backup_codes_remaining', 0)}")


@app.command()
def setup(
    no_qr: bool = typer.Option(
        False, "--no-qr", help="Skip ASCII QR rendering; print URI + secret only."
    ),
) -> None:
    """Begin TOTP enrolment: shows the otpauth URI and an ASCII QR code."""
    with client() as c:
        body = get_json(c.post("/mfa/setup"))
    if json_mode():
        emit_json(body)
        return
    uri = body.get("provisioning_uri", "")
    secret = body.get("secret", "")
    out().print("[bold]Scan with your authenticator app:[/bold]\n")
    if not no_qr:
        rendered = _render_qr_ascii(uri, fallback_png_b64=body.get("qr_png_base64"))
        if rendered:
            out().print(rendered)
    out().print(f"\nURI:    {uri}")
    out().print(f"Secret: [bold]{secret}[/bold]")
    out().print("\nThen confirm with: [bold]flow auth mfa activate <6-digit code>[/bold]")


@app.command()
def activate(
    code: str = typer.Argument(..., help="6-digit code from your authenticator."),
) -> None:
    """Confirm a TOTP code; enables MFA and prints one-shot backup codes."""
    with client() as c:
        body = get_json(c.post("/mfa/activate", json={"totp_code": code}))
    if json_mode():
        emit_json(body)
        return
    success(f"MFA enabled at {body.get('enabled_at')}")
    codes = body.get("backup_codes") or []
    if codes:
        out().print("\n[bold yellow]Backup codes (shown ONCE — store securely):[/bold yellow]")
        for c2 in codes:
            out().print(f"  {c2}")


@app.command()
def disable(
    code: str = typer.Option(
        ...,
        "--code",
        prompt="TOTP or backup code",
        help="A valid TOTP or backup code, required to disable.",
    ),
) -> None:
    """Disable MFA. Requires a valid TOTP or backup code."""
    with client() as c:
        resp = c.post("/mfa/disable", json={"code": code})
        if resp.status_code not in (200, 204):
            get_json(resp)
    success("MFA disabled.")


# --- helpers --------------------------------------------------------


def _render_qr_ascii(uri: str, *, fallback_png_b64: str | None) -> str | None:
    """Render an ASCII QR for the otpauth URI.

    First try the ``qrcode`` package (a transitive dep of the backend so
    usually present); on failure fall back to decoding the server's PNG
    if we can. Returns None if neither path works — the caller still
    prints the URI/secret as text.
    """
    try:
        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, tty=sys.stdout.isatty(), invert=True)
        return buf.getvalue()
    except Exception:  # noqa: S110 - QR rendering is cosmetic; URI + secret are still shown
        pass
    if fallback_png_b64:
        # Decode is best-effort: without a PNG-to-text lib we just hint.
        try:
            raw = base64.b64decode(fallback_png_b64)
            return (
                f"[dim](QR code is {len(raw)} bytes of PNG; install `qrcode` to "
                f"render ASCII, or scan the URI above with any TOTP app.)[/dim]"
            )
        except Exception:
            return None
    return None


__all__ = ["app"]
