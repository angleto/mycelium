"""WS-EVAL T1 CLI: generate a synthetic alberatura (protocol note 0cb0dda0
§2, task c903ec2c) and write the versioned artifacts.

    uv run python scripts/gen_workspace.py --seed 42 --scale 1000 \
        --out ~/data/WORK/mycelium-bench/workspaces/dev-42

Artifacts (corpus.jsonl, registry.jsonl, manifest.json with SHA256) are
deterministic in the seed: the benchmark IS the artifact (§1.6). The
``--blank-content`` variant emits the metadata-ablation corpus (§2).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mycelium_core.services.eval_workspace import generate_workspace, write_artifacts


def main() -> None:
    ap = argparse.ArgumentParser(description="WS-EVAL synthetic workspace generator.")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--scale", type=int, default=1000, help="approximate unit count")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output dir (default ~/data/WORK/mycelium-bench/workspaces/<seed>-<scale>)",
    )
    ap.add_argument("--locale-mix", type=float, default=0.5, help="fraction of IT units")
    ap.add_argument("--blank-content", action="store_true", help="metadata-ablation corpus")
    args = ap.parse_args()

    out = args.out or (
        Path.home() / "data/WORK/mycelium-bench/workspaces" / f"ws-{args.seed}-{args.scale}"
    )
    ws = generate_workspace(seed=args.seed, scale=args.scale, locale_mix=args.locale_mix)
    manifest = write_artifacts(ws, out, blank_content=args.blank_content)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
