"""Aggregate Mycelium MCP usage telemetry into a per-tool cost report.

The companion to the opt-in telemetry in ``mycelium_mcp.gateway``: run a real
session (or a scripted one) with ``MYCELIUM_MCP_TELEMETRY`` pointing at a
file, then feed that file here to turn the coarse "~N% of tokens went to
the MCP" attribution into a measured per-tool breakdown.

The gateway appends one JSONL row per meta-tool call:

    {"ts": "...", "kind": "execute", "tool": "list_tasks", "result_bytes": 6420}

``kind`` is ``search`` / ``describe`` / ``execute``; for ``execute`` the
``tool`` is the concrete tool name. This script reports, per tool and per
kind: call count, total/avg response bytes, an estimated token figure
(bytes / 4, the same coarse rule the baseline script uses) and each row's
share of the total response cost. That product — frequency x cost — is
what the static ``measure_baseline.py`` shape numbers cannot give on
their own.

Usage:

    # human-readable, reads $MYCELIUM_MCP_TELEMETRY by default
    uv run python scripts/perf/usage_report.py
    uv run python scripts/perf/usage_report.py /tmp/mycelium-mcp.jsonl
    uv run python scripts/perf/usage_report.py --json   # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Same coarse rule as scripts/perf/measure_baseline.py: ~4 bytes/token
# for JSON text. Approximate; the real Claude tokenizer differs, but the
# ratio is stable enough to rank and size the buckets.
_BYTES_PER_TOKEN = 4


@dataclass
class _Bucket:
    calls: int = 0
    total_bytes: int = 0
    max_bytes: int = 0
    samples: list[int] = field(default_factory=list)

    def add(self, n: int) -> None:
        self.calls += 1
        self.total_bytes += n
        self.max_bytes = max(self.max_bytes, n)
        self.samples.append(n)

    @property
    def avg_bytes(self) -> float:
        return self.total_bytes / self.calls if self.calls else 0.0

    def as_dict(self, grand_total_bytes: int) -> dict[str, Any]:
        share = self.total_bytes / grand_total_bytes if grand_total_bytes else 0.0
        return {
            "calls": self.calls,
            "total_bytes": self.total_bytes,
            "avg_bytes": round(self.avg_bytes, 1),
            "max_bytes": self.max_bytes,
            "est_total_tokens": self.total_bytes // _BYTES_PER_TOKEN,
            "share_of_bytes": round(share, 4),
        }


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                print(f"  warning: skipping malformed JSON at line {lineno}", file=sys.stderr)
                continue
            if "result_bytes" in row and "tool" in row:
                rows.append(row)
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, _Bucket] = defaultdict(_Bucket)
    by_kind: dict[str, _Bucket] = defaultdict(_Bucket)
    grand_total = 0
    for row in rows:
        n = int(row.get("result_bytes", 0))
        grand_total += n
        # ``execute`` rows are keyed by concrete tool; search/describe
        # collapse under their meta-tool name so the three protocol costs
        # stay visible alongside the data-tool costs.
        by_tool[row["tool"]].add(n)
        by_kind[row.get("kind", "?")].add(n)

    tools_sorted = sorted(by_tool.items(), key=lambda kv: kv[1].total_bytes, reverse=True)
    return {
        "total_calls": len(rows),
        "total_bytes": grand_total,
        "est_total_tokens": grand_total // _BYTES_PER_TOKEN,
        "by_kind": {k: b.as_dict(grand_total) for k, b in sorted(by_kind.items())},
        "by_tool": {k: b.as_dict(grand_total) for k, b in tools_sorted},
    }


def _print_human(report: dict[str, Any]) -> None:
    tot_b = report["total_bytes"]
    tot_t = report["est_total_tokens"]
    print(
        f"MCP usage telemetry: {report['total_calls']:,} calls, "
        f"{tot_b:,} B (~{tot_t:,} tok) of responses read by the model"
    )
    print()
    print("by kind:")
    print(f"  {'kind':<10} {'calls':>6} {'total B':>12} {'~tok':>9} {'share':>7}")
    for kind, b in report["by_kind"].items():
        print(
            f"  {kind:<10} {b['calls']:>6,} {b['total_bytes']:>12,} "
            f"{b['est_total_tokens']:>9,} {b['share_of_bytes'] * 100:>6.1f}%"
        )
    print()
    print("by tool (response cost, heaviest first):")
    print(f"  {'tool':<32} {'calls':>6} {'total B':>11} {'avg B':>8} {'~tok':>8} {'share':>7}")
    for tool, b in report["by_tool"].items():
        print(
            f"  {tool:<32} {b['calls']:>6,} {b['total_bytes']:>11,} "
            f"{b['avg_bytes']:>8,.0f} {b['est_total_tokens']:>8,} "
            f"{b['share_of_bytes'] * 100:>6.1f}%"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate Mycelium MCP usage telemetry.")
    parser.add_argument(
        "path",
        nargs="?",
        default=os.environ.get("MYCELIUM_MCP_TELEMETRY"),
        help="JSONL telemetry file (default: $MYCELIUM_MCP_TELEMETRY)",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    if not args.path:
        parser.error(
            "no telemetry file given and MYCELIUM_MCP_TELEMETRY is unset. "
            "Run a session with MYCELIUM_MCP_TELEMETRY=/tmp/mycelium-mcp.jsonl first, "
            "or pass the file path."
        )
    path = Path(args.path)
    if not path.exists():
        parser.error(f"telemetry file not found: {path}")

    rows = _load_rows(path)
    report = aggregate(rows)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
