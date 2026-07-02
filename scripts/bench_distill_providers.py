"""Multi-provider distillation bench — scientific provider choice (task N1 kin).

Compares candidate LLM providers/models on mycelium's OWN distillation task,
against a fixed baseline (e.g. the external claude atoms), so the internal-LLM
provider decision is made on measurements, not vibes.

Design invariants:
  * SAME prompt for every candidate: `_DISTILL_SYSTEM` imported from
    services/decomposition.py (the production prompt, single source of truth).
  * SAME judge for every atom: `_VERIFY_SYSTEM` run by ONE judge model over
    (source, draft) -> corrected draft; the metric is CLAIM SURVIVAL = kept
    claims / draft claims (a dropped claim = unsupported by the source).
    The judge should NOT be one of the ranked candidates.
  * temperature 0 everywhere; usage tokens + latency recorded per call.
  * A BLIND pack is emitted (atoms per source under letter codes, mapping in a
    separate file) so the human ranking cannot be biased by the model name.

Providers are OpenAI-compatible chat/completions endpoints:
  scaleway -> https://api.scaleway.ai/v1      (env SCALEWAY_API_KEY)
  openai   -> https://api.openai.com/v1       (env OPENAI_API_KEY)
  mistral  -> https://api.mistral.ai/v1       (env MISTRAL_API_KEY)
  anthropic-> via its OpenAI-compat endpoint  (env ANTHROPIC_API_KEY,
              https://api.anthropic.com/v1)

Usage:
  uv run python scripts/bench_distill_providers.py \
      --sources bench/sources.jsonl \
      --models scaleway:mistral-small-3.2-24b-instruct-2506 scaleway:llama-3.3-70b-instruct \
      --baseline claude-external:bench/baseline_atoms.jsonl \
      --judge openai:gpt-4o-mini \
      --out bench/out

sources.jsonl lines:  {"id": "...", "title": "...", "text": "..."}
baseline JSONL lines: {"source_id": "...", "text": "..."}

Keys come from env only (never argv). The script performs NO writes to
mycelium: it is an offline bench; the winning model is then configured
per-org in the SPA (fail-closed probe) and the atoms it produces in prod
still pass the review gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import string
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from mycelium_core.services.decomposition import _DISTILL_SYSTEM, _VERIFY_SYSTEM

_ENDPOINTS = {
    # SCALEWAY_BASE_URL override pins a PROJECT-scoped endpoint
    # (https://api.scaleway.ai/{PROJECT_ID}/v1), so calls cannot land on the
    # default project by mistake.
    "scaleway": (
        os.environ.get("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1"),
        "SCALEWAY_API_KEY",
    ),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
}
_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)


@dataclass
class CallResult:
    model: str
    source_id: str
    text: str
    tokens_in: int
    tokens_out: int
    latency_s: float


def _chat(spec: str, system: str, user: str, timeout: float = 120.0) -> CallResult:
    provider, _, model = spec.partition(":")
    if provider not in _ENDPOINTS:
        raise SystemExit(f"unknown provider '{provider}' (known: {sorted(_ENDPOINTS)})")
    base, key_env = _ENDPOINTS[provider]
    key = os.environ.get(key_env, "")
    if not key:
        raise SystemExit(f"missing {key_env} in env for {spec}")
    t0 = time.monotonic()
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage") or {}
    return CallResult(
        model=spec,
        source_id="",
        text=(data["choices"][0]["message"]["content"] or "").strip(),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        latency_s=round(time.monotonic() - t0, 2),
    )


def _claims(text: str) -> list[str]:
    return _BULLET.split(text)[1:] if _BULLET.search(text) else []


def _format_ok(text: str) -> bool:
    """Mechanical shape check: a lesson line, <=5 claim bullets, <=3 keywords."""
    bullets = _claims(text)
    kw = re.search(r"keywords?\s*:\s*(.+)", text, re.IGNORECASE)
    n_kw = len(kw.group(1).split(",")) if kw else 0
    # Tolerant: models legitimately wrap the label ("**Lesson:**", "(1) One-sentence
    # lesson:") -- look for the word anywhere in the head instead of at line start.
    has_lesson = bool(re.search(r"\b(lesson|lezione)\b", text[:200], re.IGNORECASE))
    return has_lesson and 1 <= len(bullets) <= 5 and 0 < n_kw <= 3


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--sources", required=True, help="JSONL {id,title,text}")
    ap.add_argument("--models", nargs="+", default=[], help="candidate specs provider:model")
    ap.add_argument(
        "--baseline",
        action="append",
        default=[],
        help="label:path.jsonl of pre-produced atoms ({source_id,text}) to include",
    )
    ap.add_argument(
        "--judge",
        default="none",
        help="judge spec provider:model (NOT a candidate), or 'none' to skip the "
        "API judge (a human/external judge scores the emitted atom files)",
    )
    ap.add_argument("--out", required=True, help="output directory")
    args = ap.parse_args()

    if args.judge in args.models:
        raise SystemExit("the judge must not be one of the ranked candidates")

    out = pathlib.Path(args.out)
    (out / "atoms").mkdir(parents=True, exist_ok=True)
    (out / "blind").mkdir(parents=True, exist_ok=True)

    sources = [
        json.loads(line)
        for line in pathlib.Path(args.sources).read_text().splitlines()
        if line.strip()
    ]

    # 1. produce candidate atoms (temperature 0, production prompt)
    atoms: list[dict[str, Any]] = []  # {model, source_id, text, tokens_in, tokens_out, latency_s}
    for spec in args.models:
        for src in sources:
            res = _chat(spec, _DISTILL_SYSTEM, src["text"])
            res.source_id = src["id"]
            atoms.append(asdict(res))
            # Persist immediately: a crash on a later model must not lose
            # already-paid-for atoms (bit us with a flagship-model timeout).
            safe = spec.replace(":", "_").replace("/", "_")
            pdir = out / "atoms" / safe
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / f"{src['id']}.md").write_text(res.text)
            print(f"[distill] {spec} x {src['id']}: {res.tokens_out} tok out, {res.latency_s}s")
    # baseline atoms enter judging + blind without API calls
    for spec in args.baseline:
        label, _, path = spec.partition(":")
        for line in pathlib.Path(path).read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            atoms.append(
                {
                    "model": label,
                    "source_id": row["source_id"],
                    "text": row["text"],
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "latency_s": 0.0,
                }
            )

    src_by_id = {s["id"]: s for s in sources}
    results: list[dict[str, Any]] = []
    for a in atoms:
        src = src_by_id.get(a["source_id"])
        if src is None:
            print(f"[skip] atom for unknown source {a['source_id']}")
            continue
        n0 = len(_claims(a["text"]))
        if args.judge == "none":
            n1, survival = -1, None  # judged externally (worksheet = atom files)
        else:
            judge_user = f"SOURCE:\n{src['text']}\n\nDRAFT:\n{a['text']}"
            corrected = _chat(args.judge, _VERIFY_SYSTEM, judge_user)
            n1 = len(_claims(corrected.text))
            survival = round(n1 / n0, 3) if n0 else 0.0
        results.append(
            {
                **a,
                "claims": n0,
                "claims_kept_by_judge": n1,
                "claim_survival": survival,
                "format_ok": _format_ok(a["text"]),
            }
        )
        safe_model = a["model"].replace(":", "_").replace("/", "_")
        p = out / "atoms" / safe_model
        p.mkdir(exist_ok=True)
        (p / f"{a['source_id']}.md").write_text(a["text"])
        if survival is not None:
            print(f"[judge] {a['model']} x {a['source_id']}: survival {n1}/{n0} = {survival}")

    # 2. blind pack: per source, atoms under letter codes; mapping kept apart
    mapping: dict[str, dict[str, str]] = {}
    for src in sources:
        rows = [r for r in results if r["source_id"] == src["id"]]
        letters = string.ascii_uppercase
        m: dict[str, str] = {}
        d = out / "blind" / src["id"]
        d.mkdir(parents=True, exist_ok=True)

        def _blind_key(r: dict[str, Any], sid: str = src["id"]) -> str:
            # Stable, non-crypto shuffle key so letter codes are reproducible.
            return hashlib.sha256(f"{r['model']}|{sid}".encode()).hexdigest()

        for i, r in enumerate(sorted(rows, key=_blind_key)):
            code = letters[i]
            m[code] = r["model"]
            (d / f"{code}.md").write_text(r["text"])
        mapping[src["id"]] = m
    (out / "blind_mapping.json").write_text(json.dumps(mapping, indent=2))

    (out / "results.jsonl").write_text("\n".join(json.dumps(r) for r in results) + "\n")

    # 3. table
    print(f"\n{'model':<44}{'srcs':>5}{'surv':>7}{'fmt':>5}{'tok_out':>9}{'p50 lat':>9}")
    by_model: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)
    for model, rows in sorted(by_model.items()):
        survs = sorted(r["claim_survival"] for r in rows if r["claim_survival"] is not None)
        surv = survs[len(survs) // 2] if survs else float("nan")
        lat = sorted(r["latency_s"] for r in rows)[len(rows) // 2]
        fmt = sum(1 for r in rows if r["format_ok"])
        tok = sum(r["tokens_out"] for r in rows)
        print(f"{model:<44}{len(rows):>5}{surv:>7.2f}{fmt:>4}/{len(rows)}{tok:>8}{lat:>8.1f}s")
    print(
        f"\nblind pack in {out}/blind (mapping in blind_mapping.json — do not peek before ranking)"
    )


if __name__ == "__main__":
    main()
