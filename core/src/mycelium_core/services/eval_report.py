"""The single reporting path of the WS-EVAL protocol (task d7c0693e,
nota WS-EVAL §8-§9): per-query result records + a frozen config in, ONE
deterministic report out -- every number with its CI, primary endpoints
under Holm-Bonferroni, everything else explicitly exploratory.

The article's tables come from here and only from here: any ad-hoc
aggregation invites the post-hoc cherry-picking the protocol bans.

Record schema (one JSON object per line):
    qid: str            category: str        fact_id: str
    rank: int | null    impossible: bool     abstained: bool
    top_score: float    served_tokens: int   gold_tokens: int
    event: bool         (only for zero-event categories: a leak /
                         erasure survivor OBSERVED on this probe)
    ndcg: float         (optional, precomputed via eval_metrics.ndcg_at_k)

Config schema:
    k: int              seed: int            alpha: float
    n_resamples: int
    primary: [ {name, category, metric: recall|zero_event,
                threshold (recall) | max_bound (zero_event)} ]
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any, cast

from mycelium_core.services.eval_metrics import abstention_metrics, context_token_ratio
from mycelium_core.services.eval_stats import (
    binomial_tail_pvalue,
    clopper_pearson_upper_zero,
    cluster_bootstrap,
    holm_bonferroni,
    wilson_ci,
)


@dataclass(frozen=True)
class QueryRecord:
    qid: str
    category: str
    fact_id: str
    rank: int | None
    impossible: bool
    abstained: bool
    top_score: float
    served_tokens: int
    gold_tokens: int
    event: bool
    ndcg: float | None

    @property
    def hit(self) -> bool:
        return self.rank is not None


def load_records(path: str | Path) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        obj = json.loads(line)
        try:
            records.append(
                QueryRecord(
                    qid=str(obj["qid"]),
                    category=str(obj["category"]),
                    fact_id=str(obj["fact_id"]),
                    rank=obj.get("rank"),
                    impossible=bool(obj.get("impossible", False)),
                    abstained=bool(obj.get("abstained", False)),
                    top_score=float(obj.get("top_score", 0.0)),
                    served_tokens=int(obj.get("served_tokens", 0)),
                    gold_tokens=int(obj.get("gold_tokens", 0)),
                    event=bool(obj.get("event", False)),
                    ndcg=(
                        float(obj["ndcg"]) if "ndcg" in obj and obj["ndcg"] is not None else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_no}: malformed record: {exc}") from exc
    if not records:
        raise ValueError(f"{path}: no records")
    return records


def load_config(path: str | Path) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("config: expected a JSON object")
    for key in ("k", "seed", "alpha", "primary"):
        if key not in cfg:
            raise ValueError(f"config: missing required key {key!r}")
    return cast(dict[str, Any], cfg)


def _fmt_ci(lo: float, hi: float) -> str:
    return f"[{lo:.3f},{hi:.3f}]"


def build_report(records: Sequence[QueryRecord], config: dict[str, Any]) -> str:
    """Render the deterministic report. The rng is seeded from the
    config, so re-running on the same inputs reproduces every bootstrap
    interval digit for digit."""
    rng = Random(int(config["seed"]))  # noqa: S311 (resampling determinism, not crypto)
    n_resamples = int(config.get("n_resamples", 2000))
    alpha = float(config["alpha"])
    lines: list[str] = [
        f"WS-EVAL report  k={config['k']}  seed={config['seed']}  "
        f"alpha={alpha}  n_resamples={n_resamples}  n_records={len(records)}",
        "",
        f"{'category':<24}{'n':>5}  {'recall':>7} {'wilson95':>15}  "
        f"{'mrr':>6} {'cluster95':>15}  {'ndcg':>6}  {'ctx':>6}",
    ]
    by_cat: dict[str, list[QueryRecord]] = {}
    for r in records:
        by_cat.setdefault(r.category, []).append(r)
    zero_event_cats = {
        str(p["category"]) for p in config["primary"] if p.get("metric") == "zero_event"
    }

    for cat in sorted(by_cat):
        if cat in zero_event_cats:
            continue  # probes, not recall queries: reported in their own section
        rs = [r for r in by_cat[cat] if not r.impossible]
        if not rs:
            continue
        hits = sum(1 for r in rs if r.hit)
        recall = hits / len(rs)
        w_lo, w_hi = wilson_ci(hits, len(rs))
        rr = [(r.fact_id, 1.0 / r.rank if r.rank else 0.0) for r in rs]
        mrr = sum(v for _, v in rr) / len(rr)
        b_lo, b_hi = cluster_bootstrap(rr, rng=rng, n_resamples=n_resamples)
        ndcgs = [r.ndcg for r in rs if r.ndcg is not None]
        ndcg = f"{sum(ndcgs) / len(ndcgs):.3f}" if ndcgs else "-"
        ratios = [
            context_token_ratio(r.served_tokens, r.gold_tokens) for r in rs if r.gold_tokens > 0
        ]
        ctx = f"{sum(ratios) / len(ratios):.2f}" if ratios else "-"
        lines.append(
            f"{cat:<24}{len(rs):>5}  {recall:>7.3f} {_fmt_ci(w_lo, w_hi):>15}  "
            f"{mrr:>6.3f} {_fmt_ci(b_lo, b_hi):>15}  {ndcg:>6}  {ctx:>6}"
        )

    abst = abstention_metrics([(r.impossible, r.abstained) for r in records])
    lines += [
        "",
        f"abstention  n={abst.n} prevalence={abst.prevalence:.3f}  "
        f"precision={abst.precision:.3f} recall={abst.recall:.3f} f1={abst.f1:.3f}",
    ]

    zero_cats = sorted(
        {str(p["category"]) for p in config["primary"] if p.get("metric") == "zero_event"}
    )
    for cat in zero_cats:
        rs = by_cat.get(cat, [])
        if not rs:
            lines.append(f"zero-event  {cat}: NO PROBES (cannot claim anything)")
            continue
        events = sum(1 for r in rs if r.event)
        bound = clopper_pearson_upper_zero(len(rs)) if events == 0 else None
        bound_s = f"CP95-upper={bound:.5f}" if bound is not None else "bound n/a (events>0)"
        lines.append(f"zero-event  {cat}: {events}/{len(rs)} observed  {bound_s}")

    named: list[tuple[str, float]] = []
    meta: dict[str, str] = {}
    for p in config["primary"]:
        name, cat, metric = str(p["name"]), str(p["category"]), str(p["metric"])
        rs = by_cat.get(cat, [])
        if metric == "recall":
            answerable = [r for r in rs if not r.impossible]
            if not answerable:
                raise ValueError(f"primary {name!r}: no answerable records in {cat!r}")
            hits = sum(1 for r in answerable if r.hit)
            threshold = float(p["threshold"])
            pv = binomial_tail_pvalue(hits, len(answerable), threshold)
            named.append((name, pv))
            meta[name] = f"{hits}/{len(answerable)} vs p0={threshold}"
        elif metric == "zero_event":
            if not rs:
                raise ValueError(f"primary {name!r}: no probes in {cat!r}")
            events = sum(1 for r in rs if r.event)
            max_bound = float(p["max_bound"])
            # The gate is exact, not asymptotic: zero events AND the CP
            # bound within the pre-registered maximum. Encoded as a
            # pseudo p-value (0 = pass, 1 = fail) so Holm ranks it first
            # without affecting the recall endpoints' thresholds.
            bound = clopper_pearson_upper_zero(len(rs)) if events == 0 else 1.0
            passed = events == 0 and bound <= max_bound
            named.append((name, 0.0 if passed else 1.0))
            meta[name] = f"{events}/{len(rs)} events, bound={bound:.5f} vs max={max_bound}"
        else:
            raise ValueError(f"primary {name!r}: unknown metric {metric!r}")

    lines += ["", f"PRIMARY ENDPOINTS (Holm-Bonferroni, alpha={alpha})"]
    for res in holm_bonferroni(named, alpha=alpha):
        verdict = "PASS" if res.rejected else "FAIL"
        lines.append(
            f"  {verdict}  {res.name:<28} p={res.pvalue:.5f} "
            f"(vs {res.adjusted_alpha:.5f})  {meta[res.name]}"
        )
    lines.append("everything not listed as primary is EXPLORATORY (CI only, no pass/fail).")
    return "\n".join(lines)
