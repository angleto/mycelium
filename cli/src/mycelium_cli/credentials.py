"""Credential store at ``~/.config/flow/credentials.toml``.

Kept in a separate file from config so a token leak does not
contaminate the more-shareable config file. File permissions are
enforced to 0600 on every write.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from mycelium_cli.config import config_dir


def credentials_path() -> Path:
    return config_dir() / "credentials.toml"


@dataclass(slots=True)
class Credential:
    token: str
    token_id: str | None = None
    user_id: str | None = None
    email: str | None = None
    workspace_id: str | None = None
    expires_at: dt.datetime | None = None


def _load_raw() -> dict[str, Any]:
    p = credentials_path()
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def load_credential(profile: str) -> Credential | None:
    raw = _load_raw()
    body = (raw.get("credentials") or {}).get(profile)
    if not isinstance(body, dict):
        return None
    token = body.get("token")
    if not isinstance(token, str) or not token:
        return None
    expires_at_raw = body.get("expires_at")
    expires_at: dt.datetime | None = None
    if isinstance(expires_at_raw, dt.datetime):
        expires_at = expires_at_raw
    elif isinstance(expires_at_raw, str):
        try:
            expires_at = dt.datetime.fromisoformat(expires_at_raw)
        except ValueError:
            expires_at = None
    return Credential(
        token=token,
        token_id=_opt_str(body.get("token_id")),
        user_id=_opt_str(body.get("user_id")),
        email=_opt_str(body.get("email")),
        workspace_id=_opt_str(body.get("workspace_id")),
        expires_at=expires_at,
    )


def save_credential(profile: str, cred: Credential) -> None:
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_raw()
    creds = raw.get("credentials") or {}
    if not isinstance(creds, dict):
        creds = {}
    creds[profile] = _credential_dict(cred)
    raw["credentials"] = creds
    # Write to a sibling temp file then rename for atomicity; this also
    # ensures the 0600 chmod always lands on a fresh inode.
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(raw, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def delete_credential(profile: str) -> bool:
    p = credentials_path()
    if not p.exists():
        return False
    raw = _load_raw()
    creds = raw.get("credentials") or {}
    if not isinstance(creds, dict) or profile not in creds:
        return False
    del creds[profile]
    raw["credentials"] = creds
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(raw, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return True


def assert_secure(path: Path) -> None:
    """Warn (do not raise) if the credentials file is group/world-readable.

    On Windows the POSIX mode is irrelevant, so we skip silently.
    """
    if sys.platform == "win32":
        return
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        # Caller decides whether to surface; we never raise to avoid
        # hard-blocking a working credential just because of perms.
        os.chmod(path, 0o600)


def _opt_str(v: Any) -> str | None:
    return str(v) if isinstance(v, str) and v else None


def _credential_dict(cred: Credential) -> dict[str, Any]:
    out: dict[str, Any] = {"token": cred.token}
    for k, v in {
        "token_id": cred.token_id,
        "user_id": cred.user_id,
        "email": cred.email,
        "workspace_id": cred.workspace_id,
    }.items():
        if v is not None:
            out[k] = v
    if cred.expires_at is not None:
        out["expires_at"] = cred.expires_at
    return out
