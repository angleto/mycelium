"""``mycelium search`` — hybrid (keyword + semantic) memory retrieval."""

from __future__ import annotations

import uuid

import typer

from mycelium_cli.cmds._common import client, get_json
from mycelium_cli.ui import emit_json, emit_table, info, json_mode


def search(
    query: str = typer.Argument(..., help="Free-text query."),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=100),
    channel: str | None = typer.Option(
        None, "--channel", help="Memory channel system key (e.g. note, conversation)."
    ),
) -> None:
    """Hybrid search across memory blobs (notes + ingested context).

    Uses ``/memory/search`` with RRF fusion of keyword and semantic
    retrieval. Each call generates a fresh operation_id for telemetry.
    """
    payload = {
        "query": query,
        "limit": limit,
        "operation_id": str(uuid.uuid4()),
    }
    if channel:
        payload["channel_key"] = channel
    with client() as c:
        hits = get_json(c.post("/memory/search", json=payload))
    if json_mode():
        emit_json(hits)
        return
    if not hits:
        info("[dim]no hits.[/dim]")
        return
    rows = []
    for h in hits:
        blob = h.get("blob") or {}
        text = (blob.get("text") or blob.get("summary") or "")[:120]
        rows.append(
            (
                f"{h.get('rrf', 0):.3f}",
                str(blob.get("id", ""))[:8],
                blob.get("namespace") or blob.get("tier") or "",
                text,
            )
        )
    emit_table(None, ["score", "id", "ns", "snippet"], rows)
