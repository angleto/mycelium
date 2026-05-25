"""Token/byte baseline for the Flow MCP surface and on-disk Claude corpus.

Run from the repo root with ``uv run python scripts/perf/measure_baseline.py``.
Writes ``docs/perf/baseline-<date>.json`` and prints a compact summary.

The numbers it captures are the cost surfaces an MCP client (e.g.
Claude Code) pays when it talks to Flow:

* ``tools/list`` payload size for both transports (HTTP gateway with 3
  meta-tools vs. stdio with all ~154 tools), with and without the
  ``_strip_auth`` pass that hides ``token``/``org_id``.
* Per-tool ``inputSchema`` + ``description`` weight: total, average,
  top-10 heaviest. This is what ``describe_tools`` will charge for
  later.
* ``search_tools`` output size for a representative query set
  (lexical/embedder branch is whichever the env exposes; payload
  shape is identical).
* Synthetic single-record sizes for the heavy serializers (``_task``,
  ``_task_full``, ``_note`` with and without ``transcript``) so list
  endpoints can be projected without standing up Postgres.
* Corpus on disk: ``~/.claude/plans``, the per-project auto-memory,
  ``docs/`` (root + ADRs), and the two CLAUDE.md.

Re-run after each optimization fork to read the delta.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _bytes(s: str | None) -> int:
    if s is None:
        return 0
    return len(s.encode("utf-8"))


def _jsize(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"))


# --------------------------------------------------------------- registry
def collect_registry() -> dict[str, Any]:
    from flow_mcp.gateway import _strip_auth, gateway
    from flow_mcp.server import mcp as registry

    def serialize(tool: Any, strip: bool) -> dict[str, Any]:
        schema = tool.parameters or {}
        if strip:
            schema = _strip_auth(schema)
        return {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": schema,
        }

    http_tools = gateway._tool_manager.list_tools()
    stdio_tools = registry._tool_manager.list_tools()

    http_payload = [serialize(t, strip=False) for t in http_tools]
    stdio_full = [serialize(t, strip=False) for t in stdio_tools]
    stdio_stripped = [serialize(t, strip=True) for t in stdio_tools]

    per_tool: list[dict[str, Any]] = []
    for t in stdio_tools:
        full = {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.parameters or {},
        }
        stripped = {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": _strip_auth(t.parameters or {}),
        }
        per_tool.append(
            {
                "name": t.name,
                "full_bytes": _jsize(full),
                "stripped_bytes": _jsize(stripped),
                "desc_bytes": _bytes(t.description),
                "schema_props": len((t.parameters or {}).get("properties", {})),
            }
        )

    per_tool.sort(key=lambda x: x["full_bytes"], reverse=True)
    total_full = sum(p["full_bytes"] for p in per_tool)
    total_stripped = sum(p["stripped_bytes"] for p in per_tool)
    total_desc = sum(p["desc_bytes"] for p in per_tool)

    return {
        "n_http_tools": len(http_tools),
        "n_stdio_tools": len(stdio_tools),
        "tools_list_http_bytes": _jsize(http_payload),
        "tools_list_stdio_full_bytes": _jsize(stdio_full),
        "tools_list_stdio_stripped_bytes": _jsize(stdio_stripped),
        "per_tool_total_full_bytes": total_full,
        "per_tool_total_stripped_bytes": total_stripped,
        "per_tool_total_desc_bytes": total_desc,
        "per_tool_avg_full_bytes": total_full // max(1, len(per_tool)),
        "per_tool_avg_stripped_bytes": total_stripped // max(1, len(per_tool)),
        "top10_heaviest_full": [
            {
                k: v
                for k, v in p.items()
                if k in ("name", "full_bytes", "stripped_bytes", "schema_props")
            }
            for p in per_tool[:10]
        ],
        "top10_verbose_descriptions": sorted(
            [{"name": p["name"], "desc_bytes": p["desc_bytes"]} for p in per_tool],
            key=lambda x: x["desc_bytes"],
            reverse=True,
        )[:10],
    }


# --------------------------------------------------------------- search
async def collect_search_samples() -> dict[str, Any]:
    from flow_mcp.gateway import search_tools

    queries = [
        "create a task",
        "list my notes",
        "stop the running timer",
        "send invoice for this client",
        "find appointments today",
        "embed a memory blob",
        "assign tag to project",
    ]
    out: dict[str, Any] = {}
    for q in queries:
        results = await search_tools(q, limit=8)
        out[q] = {"results": len(results), "bytes": _jsize(results)}
    return out


# --------------------------------------------------------------- serializer mocks
def collect_serializer_samples() -> dict[str, Any]:
    """Synthetic per-record size for the heavy serializers.

    The mocks emulate the duck-typed attributes the serializers touch.
    Numbers approximate the wire-size of a single element in
    ``list_tasks`` / ``list_notes`` etc. so we can extrapolate against
    realistic list sizes without a live database.
    """
    from flow_mcp.server import _note, _project_fields, _tag_brief, _task, _task_full

    def make_tag(name: str, kind: str = "generic") -> Any:
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            kind=SimpleNamespace(value=kind),
            name=name,
            color="#3b82f6",
            version=1,
        )

    def make_task() -> Any:
        return SimpleNamespace(
            id="22222222-2222-2222-2222-222222222222",
            title="Implement field projection on list_* tools",
            description=(
                "Add an optional `fields` parameter to list_tasks/notes/events "
                "so callers can opt out of UUID-only columns when they don't "
                "need them. Keep behaviour identical when omitted."
            ),
            state_id="33333333-3333-3333-3333-333333333333",
            priority=2,
            importance=4,
            urgency=3,
            start_date=None,
            due_date=None,
            billable=True,
            parent_task_id=None,
            estimate_effort_h=None,
            required_capabilities=[],
            monetary_cost=None,
            location=None,
            necessity=SimpleNamespace(value="should"),
            budget_id=None,
            is_archived=False,
            offered=False,
            deleted_at=None,
            version=1,
            created_by_identity_id="44444444-4444-4444-4444-444444444444",
            created_by_token_id=None,
            assignee_id="55555555-5555-5555-5555-555555555555",
        )

    def make_note(transcript_chars: int) -> Any:
        seed = "La nota tipica vive in tutto il flusso di cattura. "
        transcript = (seed * (transcript_chars // len(seed) + 1))[:transcript_chars]
        return SimpleNamespace(
            id="66666666-6666-6666-6666-666666666666",
            project_id="77777777-7777-7777-7777-777777777777",
            kind=SimpleNamespace(value="text"),
            status=SimpleNamespace(value="active"),
            title="Brainstorm: dynamic toolset cache",
            transcript=transcript,
            version=1,
        )

    tags_small = [make_tag("backend"), make_tag("perf"), make_tag("ACME", "client")]

    task_one = _task(make_task(), tags_small)
    task_full_one = _task_full(make_task(), tags_small)
    note_with_transcript = _note(make_note(1500), tags_small, primary_task_id=None)
    note_no_transcript = _note(
        make_note(1500), tags_small, primary_task_id=None, include_transcript=False
    )
    # Mirror the LLM-side projection pattern: a picker only needs
    # (id, title) for a task, or (id, title, kind) for a note.
    task_picker = _project_fields(task_one, ["title"])
    note_picker = _project_fields(note_no_transcript, ["title", "kind"])

    samples = {
        "task_brief_bytes": _jsize(task_one),
        "task_full_bytes": _jsize(task_full_one),
        "task_picker_projected_bytes": _jsize(task_picker),
        "note_with_transcript_1500_bytes": _jsize(note_with_transcript),
        "note_without_transcript_bytes": _jsize(note_no_transcript),
        "note_picker_projected_bytes": _jsize(note_picker),
        "tag_brief_bytes": _jsize(_tag_brief(make_tag("perf"))),
    }
    n = 50
    samples[f"list_tasks_{n}_brief_bytes"] = samples["task_brief_bytes"] * n
    samples[f"list_tasks_{n}_full_bytes"] = samples["task_full_bytes"] * n
    samples[f"list_tasks_{n}_picker_bytes"] = samples["task_picker_projected_bytes"] * n
    samples[f"list_notes_{n}_with_transcript_bytes"] = (
        samples["note_with_transcript_1500_bytes"] * n
    )
    samples[f"list_notes_{n}_without_transcript_bytes"] = (
        samples["note_without_transcript_bytes"] * n
    )
    samples[f"list_notes_{n}_picker_bytes"] = samples["note_picker_projected_bytes"] * n
    return samples


# --------------------------------------------------------------- corpus
def measure_dir(path: Path, glob: str = "**/*.md") -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "files": 0, "bytes": 0, "lines": 0}
    files, total_b, total_l, biggest = 0, 0, 0, []
    for p in path.glob(glob):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
            text = data.decode("utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        total_b += len(data)
        total_l += text.count("\n") + 1
        biggest.append(
            {
                "path": str(p.relative_to(path)),
                "bytes": len(data),
                "lines": text.count("\n") + 1,
            }
        )
    biggest.sort(key=lambda x: x["bytes"], reverse=True)
    return {
        "exists": True,
        "root": str(path),
        "files": files,
        "bytes": total_b,
        "lines": total_l,
        "top5": biggest[:5],
    }


def file_size(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "bytes": 0}
    return {"exists": True, "bytes": path.stat().st_size}


def collect_corpus() -> dict[str, Any]:
    home = Path.home()
    return {
        "plans_user": measure_dir(home / ".claude/plans"),
        "memory_project": measure_dir(
            home / ".claude/projects/-Users-angelo-data-WORK-flow/memory"
        ),
        "docs_repo": measure_dir(REPO / "docs"),
        "claude_md_user": file_size(home / ".claude/CLAUDE.md"),
        "claude_md_repo": file_size(REPO / "CLAUDE.md"),
        "settings_local_repo": file_size(REPO / ".claude/settings.local.json"),
    }


# --------------------------------------------------------------- main
async def main() -> int:
    out: dict[str, Any] = {
        "ts": date.today().isoformat(),
        "python": sys.version.split()[0],
        "env": {
            "FLOW_EMBEDDER": os.environ.get("FLOW_EMBEDDER", ""),
        },
    }
    out["registry"] = collect_registry()
    out["search_tools"] = await collect_search_samples()
    out["serializers"] = collect_serializer_samples()
    out["corpus"] = collect_corpus()

    # Aggregate cost summary (the "what does Claude pay" tldr).
    reg = out["registry"]
    out["summary"] = {
        "tools_list_http_tokens_est": reg["tools_list_http_bytes"] // 4,
        "tools_list_stdio_full_tokens_est": reg["tools_list_stdio_full_bytes"] // 4,
        "tools_list_stdio_stripped_tokens_est": reg["tools_list_stdio_stripped_bytes"] // 4,
        "stripping_savings_bytes": reg["tools_list_stdio_full_bytes"]
        - reg["tools_list_stdio_stripped_bytes"],
        "corpus_total_bytes": sum(
            v.get("bytes", 0) if isinstance(v, dict) else 0 for v in out["corpus"].values()
        ),
    }

    out_dir = REPO / "docs/perf"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"baseline-{out['ts']}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))

    summary = out["summary"]
    reg = out["registry"]
    corp = out["corpus"]
    http_b = reg["tools_list_http_bytes"]
    http_t = summary["tools_list_http_tokens_est"]
    stdio_b = reg["tools_list_stdio_full_bytes"]
    stdio_t = summary["tools_list_stdio_full_tokens_est"]
    strip_b = reg["tools_list_stdio_stripped_bytes"]
    strip_t = summary["tools_list_stdio_stripped_tokens_est"]
    save_b = summary["stripping_savings_bytes"]
    print(f"baseline written: {out_path.relative_to(REPO)}")
    print()
    print(f"tools/list HTTP (3 meta-tools):     {http_b:>9,} B  (~{http_t:,} tok)")
    print(f"tools/list stdio full (154 tools):  {stdio_b:>9,} B  (~{stdio_t:,} tok)")
    print(f"tools/list stdio stripped:          {strip_b:>9,} B  (~{strip_t:,} tok)")
    print(f"  auth-stripping savings:           {save_b:>9,} B")
    print()
    print(f"per-tool avg full schema bytes:     {reg['per_tool_avg_full_bytes']:>9,} B")
    print(f"per-tool avg stripped schema:       {reg['per_tool_avg_stripped_bytes']:>9,} B")
    print()
    print("top 5 heaviest tools (full):")
    for p in reg["top10_heaviest_full"][:5]:
        print(f"  {p['name']:<40} {p['full_bytes']:>6,} B  ({p['schema_props']} props)")
    print()
    print("serializer per-record bytes:")
    for k, v in out["serializers"].items():
        if k.startswith("list_"):
            continue
        print(f"  {k:<45} {v:>6,} B")
    print()
    print("projected list_*(50) payloads:")
    for k, v in out["serializers"].items():
        if k.startswith("list_"):
            print(f"  {k:<45} {v:>7,} B")
    print()
    print(f"corpus total on disk: {summary['corpus_total_bytes']:,} B")
    for k, v in corp.items():
        if isinstance(v, dict) and v.get("bytes"):
            print(f"  {k:<30} {v.get('files', '-'):>4}f  {v['bytes']:>9,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
