"""WS-EVAL T2 CLI: generate the adversarial query set from a (regenerated)
T1 workspace and write the versioned artifacts (protocol note 0cb0dda0 §3,
task 4a2670ac).

    uv run python scripts/gen_queries.py --ws-seed 42 --scale 1000 \
        --query-seed 1042 --out ~/data/WORK/mycelium-bench/workspaces/ws-42-1000

The workspace is REGENERATED from its seed (T1 is deterministic, so this is
byte-equivalent to loading the artifacts) and the query seed is independent
from the corpus seed. ``--reviewer-pack`` also exports the human-anchor pack
(registry only, no corpus text).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycelium_core.services.eval_queries import (
    build_queries,
    export_reviewer_pack,
    write_query_artifacts,
)
from mycelium_core.services.eval_workspace import generate_workspace


def main() -> None:
    ap = argparse.ArgumentParser(description="WS-EVAL adversarial query generator.")
    ap.add_argument("--ws-seed", type=int, required=True, help="T1 workspace seed")
    ap.add_argument("--scale", type=int, default=1000)
    ap.add_argument("--query-seed", type=int, required=True, help="independent query seed")
    ap.add_argument("--locale-mix", type=float, default=0.5)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output dir (default ~/data/WORK/mycelium-bench/workspaces/ws-<seed>-<scale>)",
    )
    ap.add_argument("--reviewer-pack", action="store_true", help="export the human-anchor pack")
    args = ap.parse_args()

    out = args.out or (
        Path.home() / "data/WORK/mycelium-bench/workspaces" / f"ws-{args.ws_seed}-{args.scale}"
    )
    ws = generate_workspace(seed=args.ws_seed, scale=args.scale, locale_mix=args.locale_mix)
    records = build_queries(ws, seed=args.query_seed)
    manifest = write_query_artifacts(records, out)
    if args.reviewer_pack:
        n = export_reviewer_pack(ws, out / "reviewer_pack.md", seed=args.query_seed)
        manifest["reviewer_pack_facts"] = n
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
