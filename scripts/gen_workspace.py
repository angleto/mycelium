"""WS-EVAL T1 CLI: generate a synthetic alberatura (protocol note 0cb0dda0
§2, task c903ec2c) and write the versioned artifacts.

    uv run python scripts/gen_workspace.py --seed 42 --scale 1000 \
        --out ~/data/WORK/mycelium-bench/workspaces/dev-42

Artifacts (corpus.jsonl, registry.jsonl, manifest.json with SHA256) are
deterministic in the seed: the benchmark IS the artifact (§1.6). The
``--blank-content`` variant emits the metadata-ablation corpus (§2); the
``--enrich provider:model`` variant runs the layer-2 LLM enricher (A11), with
fact-preservation verified per unit (lossy units keep the template text).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mycelium_core.services.eval_workspace import (
    Enricher,
    LLMEnricher,
    generate_workspace,
    write_artifacts,
)

# OpenAI-compatible chat endpoints (same convention as bench_distill_providers).
_ENDPOINTS = {
    "scaleway": (
        os.environ.get("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1"),
        "SCALEWAY_API_KEY",
    ),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
}


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _build_enricher(spec: str) -> Enricher:
    """``provider:model`` -> an LLMEnricher wired to an httpx chat client. The
    HTTP client lives here, never in core."""
    import httpx  # local import: only when enrichment is requested

    provider, _, model = spec.partition(":")
    if provider not in _ENDPOINTS:
        raise SystemExit(f"unknown provider {provider!r} (known: {sorted(_ENDPOINTS)})")
    base, key_env = _ENDPOINTS[provider]
    key = os.environ.get(key_env, "")
    if not key:
        raise SystemExit(f"missing {key_env} in env for {spec}")

    def complete(prompt: str) -> str:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.4,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()

    return LLMEnricher(complete, provider=provider, model=model, temperature=0.4)


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
    ap.add_argument(
        "--enrich",
        default=None,
        metavar="PROVIDER:MODEL",
        help="LLM enrichment (A11), e.g. scaleway:mistral-medium-3.5-128b; "
        "reads ~/data/WORK/mycelium-bench/.env for the API key",
    )
    args = ap.parse_args()

    out = args.out or (
        Path.home() / "data/WORK/mycelium-bench/workspaces" / f"ws-{args.seed}-{args.scale}"
    )
    enricher: Enricher | None = None
    if args.enrich:
        _load_env(Path.home() / "data/WORK/mycelium-bench/.env")
        enricher = _build_enricher(args.enrich)
    ws = generate_workspace(
        seed=args.seed, scale=args.scale, locale_mix=args.locale_mix, enricher=enricher
    )
    manifest = write_artifacts(ws, out, blank_content=args.blank_content)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
