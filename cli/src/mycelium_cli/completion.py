"""Shell-completion helpers.

Typer installs static completion via ``--install-completion``. Dynamic
completion on UUIDs (task_id, note_id) requires actual API calls every
TAB press — too slow without caching, since each TAB spawns a fresh
Python process.

This module caches the listing on disk for a short TTL under
``$XDG_CACHE_HOME/flow/`` (or ``~/.cache/flow/``). The cache key is a
sha1 of (base_url, workspace, endpoint); credentials are loaded the
same way the regular CLI does. Failures (no creds, no network, server
down) return an empty list silently — completion must never raise.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

from mycelium_cli.config import Profile, load_config
from mycelium_cli.credentials import Credential, load_credential
from mycelium_cli.http import authed_client

_TTL_SECONDS = 60


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "flow"


def _cache_path(profile: str, prof: Profile, endpoint: str) -> Path:
    h = hashlib.sha1(  # noqa: S324 - cache key, not a security hash
        f"{prof.base_url}|{prof.workspace_id}|{endpoint}".encode()
    ).hexdigest()[:16]
    return _cache_dir() / f"complete-{profile}-{h}.json"


def _load_cache(path: Path) -> list[str] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if time.time() - st.st_mtime > _TTL_SECONDS:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [str(x) for x in data]


def _save_cache(path: Path, ids: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(ids, fh)
        os.replace(tmp, path)
    except OSError:
        # Completion shouldn't fail the shell command if the cache
        # filesystem is read-only / out of space.
        pass


def _fetch_ids(prof: Profile, cred: Credential, endpoint: str) -> list[str]:
    try:
        with authed_client(prof.base_url, cred) as c:
            resp = c.get(endpoint, params={"include_archived": "true"}, timeout=2.0)
            if resp.status_code != 200:
                return []
            payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(r.get("id")) for r in payload if isinstance(r, dict) and r.get("id")]


def _ids_for(endpoint: str) -> list[str]:
    """Best-effort listing of IDs for an endpoint, with on-disk cache."""
    try:
        cfg = load_config()
        prof = cfg.profiles.get(cfg.current_profile)
        if prof is None:
            return []
        cred = load_credential(cfg.current_profile)
        if cred is None:
            return []
        path = _cache_path(cfg.current_profile, prof, endpoint)
        cached = _load_cache(path)
        if cached is not None:
            return cached
        ids = _fetch_ids(prof, cred, endpoint)
        if ids:
            _save_cache(path, ids)
        return ids
    except Exception:
        # Belt and braces: completion is best-effort.
        return []


def _complete(endpoint: str, incomplete: str) -> Iterable[str]:
    ids = _ids_for(endpoint)
    inc = incomplete.lower()
    if not inc:
        # Return up to 25 to avoid flooding the shell with hundreds of UUIDs.
        return ids[:25]
    return [i for i in ids if i.lower().startswith(inc)]


# Public callbacks bound on Argument(autocompletion=...)


def complete_task_id(incomplete: str) -> Iterable[str]:
    return _complete("/tasks", incomplete)


def complete_note_id(incomplete: str) -> Iterable[str]:
    return _complete("/notes", incomplete)
