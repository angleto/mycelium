"""Cosa costa un cross-encoder SUL NODO, non sul portatile (task 03cdf674).

Due cifre, e si prendono solo qui:

**Memoria.** Letta dal cgroup del container (``/sys/fs/cgroup/memory.current``
e ``memory.peak``), non dall'RSS del processo. La differenza non e'
pignoleria: safetensors mappa i pesi in memoria, quindi l'RSS conta le pagine
toccate e in locale entrambi i modelli riportavano +0,42 GB, che per 568M
parametri a fp32 e' impossibile (sarebbero 2,27 GB). Il cgroup conta le
pagine della page cache attribuite al container, che e' la cifra su cui il
kernel decide di uccidere.

**Latenza.** Mediana di tre giri di ``predict`` su coppie di lunghezza
realistica, dopo un giro di riscaldamento che non entra nella statistica: il
primo passaggio paga la compilazione dei kernel e non e' cio' che un utente
vedrebbe alla seconda ricerca.

Uso (dentro il Job, con l'immagine del backend del tag in esercizio):

    python reranker_pod_probe.py BAAI/bge-reranker-v2-m3
    python reranker_pod_probe.py Alibaba-NLP/gte-multilingual-reranker-base \\
        --trust-remote-code --code-revision 40ced75c3017eb27626c9d4ea981bde21a2662f4

Esce con JSON su stdout e la cronaca su stderr, cosi' ``kubectl logs`` resta
leggibile e il risultato resta estraibile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "src"))

from mycelium_core.reranker import LocalReranker

# La lunghezza media dei documenti serviti nel bench. Coppie piu' corte
# misurerebbero un caso che la produzione non ha.
_DOC_CHARS = 1100
_QUERY = "chi ha l'autorita' nella vista advisory, il core deterministico o il modello?"
_DOC = (
    "La decisione presa il 2026-08 e' che il core deterministico resta l'autorita' e il "
    "modello linguistico narra soltanto: ogni proposta autonoma resta una proposta, e "
    "l'ultima parola e' di chi legge. Il motivo non e' diffidenza verso il modello ma la "
    "forma dell'errore: un errore del core e' ripetibile e si corregge una volta sola, un "
    "errore del narratore e' plausibile e si corregge ogni volta che qualcuno ci casca. "
) * 4


def _cgroup(name: str) -> int | None:
    """Una cifra del cgroup v2, o None fuori da un container."""
    for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/memory"):
        p = Path(base) / name
        if p.exists():
            try:
                return int(p.read_text().strip())
            except (ValueError, OSError):
                return None
    return None


def _mib(v: int | None) -> float | None:
    return None if v is None else round(v / (1024 * 1024), 1)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--code-revision", default=None)
    ap.add_argument("--revision", default=None)
    ap.add_argument("--top-k", type=int, nargs="+", default=[10, 16, 50])
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    doc = _DOC[:_DOC_CHARS]
    out: dict[str, object] = {
        "model": args.model,
        "doc_chars": len(doc),
        "rounds": args.rounds,
        "cgroup_before_mib": _mib(_cgroup("memory.current")),
    }
    print(f"[probe] cgroup prima del caricamento: {out['cgroup_before_mib']} MiB", file=sys.stderr)

    rr = LocalReranker(
        args.model,
        trust_remote_code=args.trust_remote_code,
        code_revision=args.code_revision,
        revision=args.revision,
    )
    t0 = time.monotonic()
    await rr.prewarm()
    out["load_seconds"] = round(time.monotonic() - t0, 2)
    out["cgroup_after_load_mib"] = _mib(_cgroup("memory.current"))
    print(
        f"[probe] caricato in {out['load_seconds']}s, cgroup {out['cgroup_after_load_mib']} MiB",
        file=sys.stderr,
    )

    # Riscaldamento fuori statistica.
    await rr.rerank(_QUERY, [doc] * 4)

    lat: dict[str, float] = {}
    for k in args.top_k:
        pairs = [doc] * k
        runs = []
        for _ in range(args.rounds):
            t = time.monotonic()
            await rr.rerank(_QUERY, pairs)
            runs.append(time.monotonic() - t)
        lat[str(k)] = round(statistics.median(runs), 3)
        print(f"[probe] top_k={k}: mediana {lat[str(k)]}s su {runs}", file=sys.stderr)
    out["median_seconds_by_top_k"] = lat
    out["cgroup_peak_mib"] = _mib(_cgroup("memory.peak"))
    out["cgroup_after_predict_mib"] = _mib(_cgroup("memory.current"))
    out["cgroup_max_mib"] = _mib(_cgroup("memory.max"))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
