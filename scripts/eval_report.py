"""WS-EVAL report CLI (task d7c0693e): the SINGLE reporting path of the
protocol -- per-query result records (JSONL) + frozen config (JSON) in,
one deterministic report out (nota WS-EVAL 0cb0dda0 §8).

    uv run python scripts/eval_report.py --results out/records.jsonl \
        --config out/wseval_config.json

The config is part of the pre-registered freeze (T5): seed, k, alpha,
primary endpoints with thresholds. Re-running on the same inputs
reproduces every bootstrap digit (the rng is seeded from the config).
All the logic lives in ``mycelium_core.services.eval_report`` so the CI
golden test exercises exactly what this entry point prints.
"""

from __future__ import annotations

import argparse

from mycelium_core.services.eval_report import build_report, load_config, load_records


def main() -> None:
    ap = argparse.ArgumentParser(description="WS-EVAL deterministic report.")
    ap.add_argument("--results", required=True, help="per-query records JSONL")
    ap.add_argument("--config", required=True, help="frozen protocol config JSON")
    args = ap.parse_args()
    print(build_report(load_records(args.results), load_config(args.config)))


if __name__ == "__main__":
    main()
