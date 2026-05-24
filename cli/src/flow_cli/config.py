"""User-facing configuration and on-disk paths.

We deliberately do NOT use ``platformdirs`` (which would resolve to
``~/Library/Application Support/flow`` on macOS): a dotfile-friendly
``~/.config/flow/`` matches what ``gh``, ``aws``, ``kubectl`` and friends
use across macOS and Linux and is what a tmux-resident user expects.

``FLOW_CONFIG_DIR`` overrides everything (used in tests and for ad-hoc
multi-profile setups).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


def config_dir() -> Path:
    env_override = os.environ.get("FLOW_CONFIG_DIR")
    if env_override:
        return Path(env_override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "flow"


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass(slots=True)
class Profile:
    base_url: str = "http://localhost:8000"
    workspace_id: str | None = None
    workspace_name: str | None = None


@dataclass(slots=True)
class Config:
    profiles: dict[str, Profile] = field(default_factory=dict)
    current_profile: str = "default"

    def profile(self) -> Profile:
        return self.profiles.setdefault(self.current_profile, Profile())


def _load_raw() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def load_config() -> Config:
    raw = _load_raw()
    current = str(raw.get("current_profile", "default"))
    profiles: dict[str, Profile] = {}
    raw_profiles = raw.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}
    for name, body in raw_profiles.items():
        if not isinstance(body, dict):
            continue
        profiles[str(name)] = Profile(
            base_url=str(body.get("base_url", "http://localhost:8000")),
            workspace_id=(str(body["workspace_id"]) if body.get("workspace_id") else None),
            workspace_name=(str(body["workspace_name"]) if body.get("workspace_name") else None),
        )
    cfg = Config(profiles=profiles, current_profile=current)
    cfg.profile()
    return cfg


def save_config(cfg: Config) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "current_profile": cfg.current_profile,
        "profiles": {
            name: {
                k: v
                for k, v in {
                    "base_url": prof.base_url,
                    "workspace_id": prof.workspace_id,
                    "workspace_name": prof.workspace_name,
                }.items()
                if v is not None
            }
            for name, prof in cfg.profiles.items()
        },
    }
    with p.open("wb") as fh:
        tomli_w.dump(data, fh)
    # Config file is not secret (no token here) but stays user-private
    # to avoid surprises if a profile name leaks workspace identifiers.
    try:
        os.chmod(p, 0o600)
    except OSError:
        # Best-effort on filesystems that ignore POSIX modes.
        if sys.platform != "win32":
            raise
