"""Mint one credential per agent session, so possession names an agent.

ADR-0063 gives a task a holder. Who that holder is has four candidate
answers, and three of them are the same value for every session in this
workspace: one user, one assistant identity, one token. Only the
caller-supplied worker id separates them, which works -- and leaves the
holder unreadable to a human, because ``task_leases_list`` then shows
fifteen rows attributed to one assistant, and every revision and audit
row stamps the same token.

This closes that. One ``ai_assistant`` per session, ``runtime=external``
(the dispatch loop cannot drive a client that executes elsewhere), each
with its own token, all carrying the scope of the assistant named by
``--like``. After it runs, three things become true that were not:

- ``task_leases_list`` and the activity log name the agent, not the pool;
- a session that forgets to pass ``worker_id`` still falls back to a
  DISTINCT label, because the fallback is derived from the token. That is
  what makes ``exclude_own_handoffs`` safe on by default: before this,
  every checker read as the author and every check would be refused;
- revoking one agent does not revoke the others.

It does NOT distribute the secrets. They are written to a 0600 file and
shown once by the server; putting them anywhere shared would undo the
point of having fifteen of them. Run it, place the tokens, delete the
file.

Usage:

    uv run python scripts/provision_agent_identities.py --workers 10 --checkers 5

Re-running creates MORE assistants rather than reconciling: the API has
no upsert and guessing which of two same-labelled rows was meant is
worse than refusing. Check ``--list`` first.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tomllib
from typing import Any

import httpx

BASE = "https://mycelium.xeno.garden/api"
PATH = "/ai-assistants"


def _creds() -> tuple[str, str]:
    p = pathlib.Path.home() / ".config/mycelium/credentials.toml"
    c = tomllib.loads(p.read_text())["credentials"]["default"]
    return c["token"], c["workspace_id"]


def _client(token: str, ws: str) -> httpx.Client:
    """An HTTP client, not a shelled-out curl.

    ``X-Workspace-Role: owner`` is the documented act-as lever and is
    clamped DOWN to the caller's real membership
    (``api/deps.effective_role``), so it cannot escalate: a member asking
    for owner stays a member. It is needed because minting a credential
    is owner-gated, and the default is least privilege -- a request acts
    as a plain member unless it says otherwise.
    """
    return httpx.Client(
        base_url=BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Workspace-Id": ws,
            "X-Workspace-Role": "owner",
            "Content-Type": "application/json",
        },
        timeout=30.0,
        follow_redirects=True,
    )


def _list(client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get(PATH)
    if r.status_code != 200:
        sys.exit(f"list failed: {r.status_code} {r.text[:400]}")
    parsed: list[dict[str, Any]] = r.json()
    return parsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--checkers", type=int, default=5)
    ap.add_argument("--like", default="Claude", help="assistant whose scope to copy")
    ap.add_argument("--list", action="store_true", help="show what exists and stop")
    ap.add_argument(
        "--out",
        default=str(pathlib.Path.home() / "mycelium-agent-tokens.json"),
        help="0600 file the secrets are written to",
    )
    a = ap.parse_args()

    token, ws = _creds()
    client = _client(token, ws)
    rows = _list(client)
    if a.list:
        for row in rows:
            print(
                f"{row.get('label'):<20} {row['id']}  runtime={row.get('runtime')} "
                f"active={row.get('is_active')} scopes={len(row.get('scope') or [])}"
            )
        return 0

    ref = next((x for x in rows if x.get("label") == a.like), None)
    if ref is None:
        sys.exit(f"no assistant labelled {a.like!r}; run with --list")
    scope = ref["scope"]

    names = [f"claude-w{i}" for i in range(1, a.workers + 1)]
    names += [f"claude-v{i}" for i in range(1, a.checkers + 1)]
    existing = {x.get("label") for x in rows}
    clash = [n for n in names if n in existing]
    if clash:
        sys.exit(f"already present, refusing to duplicate: {', '.join(clash)}")

    made: list[dict[str, str]] = []
    for n in names:
        r = client.post(
            PATH,
            json={
                "label": n,
                "scope": scope,
                "runtime": "external",
                "notes": (
                    "Una sessione agente del pool. Ogni sessione ha la sua credenziale "
                    "cosi' che il possesso di un task (ADR-0063) e l'attribuzione di ogni "
                    "revisione nominino l'agente e non il pool. w = esecuzione, v = verifica."
                ),
            },
        )
        body = r.json()
        if "raw_secret" not in body:
            print(f"FAILED at {n}: {r.status_code} {json.dumps(body)[:300]}", file=sys.stderr)
            break
        made.append({"handle": n, "assistant_id": body["id"], "secret": body["raw_secret"]})
        print(f"ok {n} {body['id']}")

    dest = pathlib.Path(a.out)
    dest.write_text(json.dumps(made, indent=1))
    dest.chmod(0o600)
    print(f"\n{len(made)} credentials written to {dest} (0600).")
    print("Place them, then delete the file. The server will not show them again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
