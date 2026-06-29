"""Per-row FTS language for ``memory_blobs`` (task b1baaf52).

The lexical retrieval branch stems text with a Postgres text-search
dictionary. A single hardcoded dictionary (``italian``, migration 0007)
mis-stems every other language -- a French/English note gets Italian
stems, so morphological matching silently fails and snippets miss
stemmed hits. We instead store the config PER ROW
(``memory_blobs.fts_language``) and stem each row with its own
dictionary, falling back to ``simple`` (no stemming, never wrong) for
short / ambiguous / unsupported text.

Detection is a dependency-free stopword vote: function words are the
highest-signal, lowest-cost language markers, and the ``simple`` fallback
bounds any miss (exact match still works via the ``fts`` ``simple``
column). It is deliberately coarse -- the goal is "stem French with the
French dictionary, not the Italian one", not forensic language ID. When
the caller already knows the language it can pass it explicitly and skip
detection entirely.
"""

from __future__ import annotations

import re

#: ``simple`` = no stemming. The universal safe fallback: a row tagged
#: ``simple`` is only ever matched verbatim, which is correct (never a
#: wrong stem), just less recall on morphology.
FTS_SIMPLE = "simple"

#: The Snowball text-search configs Postgres ships (``pg_ts_config``).
#: We only ever store a value from this set or ``simple`` so the
#: ``fts_language::regconfig`` cast in retrieval can never raise. Kept
#: static (the Snowball set is stable across PG versions); an explicit
#: language outside it is coerced to ``simple`` by
#: :func:`normalize_fts_language`.
SUPPORTED_CONFIGS: frozenset[str] = frozenset(
    {
        FTS_SIMPLE,
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)

# High-frequency function words per language. Detection only covers the
# Latin-script languages this corpus realistically sees; anything else is
# left to an explicit language or the ``simple`` fallback. Sets are kept
# distinct enough that the close it/es/pt/fr families separate on a
# few-token sentence; genuine ambiguity resolves to ``simple``.
_STOPWORDS: dict[str, frozenset[str]] = {
    "english": frozenset(
        "the a an and or but of to in on for with at by from as is are was were be been "
        "this that these those it its he she they we you not no".split()
    ),
    "italian": frozenset(
        "il lo la i gli le un uno una e ed o ma di a da in con su per tra fra che chi "
        "non si come anche questo questa sono è perché del della dei delle nel nella".split()
    ),
    "french": frozenset(
        "le la les un une des et ou mais de du au aux dans sur pour avec par ne pas que "
        "qui ce cette ces je tu il elle nous vous ils est sont être plus".split()
    ),
    "spanish": frozenset(
        "el la los las un una unos unas y o pero de del al en con por para que se no "
        "es son su sus como más este esta estos estas porque entre sin".split()
    ),
    "portuguese": frozenset(
        "o a os as um uma uns umas e ou mas de do da dos das em no na nos nas com por "
        "para que se não é são seu sua como mais este esta porque entre".split()
    ),
    "german": frozenset(
        "der die das den dem des ein eine einer einem einen und oder aber mit von zu im "
        "in auf für ist sind war nicht auch sich dass werden wird als wie".split()
    ),
    "dutch": frozenset(
        "de het een en of maar van te in op voor met aan door is zijn was niet ook dat "
        "die dit deze als om naar bij uit over worden wordt".split()
    ),
}

_TOKEN_RE = re.compile(r"[a-zà-öø-ÿ]+")

# Below this many word tokens the stopword vote is too noisy; ``simple``
# is the honest answer (a 2-3 word task title needs no stemmer anyway).
_MIN_TOKENS = 3
# The winner must clear this many stopword hits AND strictly beat the
# runner-up, else the languages are too close to call -> ``simple``.
_MIN_HITS = 2


def detect_fts_language(text: str | None) -> str:
    """Best-effort text-search config for ``text``.

    Returns a name from :data:`SUPPORTED_CONFIGS` when a single language
    wins the stopword vote with enough margin, else :data:`FTS_SIMPLE`.
    Pure and deterministic (no I/O, no global state) so writes and tests
    are reproducible.
    """
    if not text:
        return FTS_SIMPLE
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < _MIN_TOKENS:
        return FTS_SIMPLE
    bag = set(tokens)
    scores = {lang: len(bag & words) for lang, words in _STOPWORDS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    best_hits = scores[best]
    if best_hits < _MIN_HITS:
        return FTS_SIMPLE
    runner_up = max((h for lang, h in scores.items() if lang != best), default=0)
    if best_hits == runner_up:
        return FTS_SIMPLE
    return best


def normalize_fts_language(value: str | None) -> str:
    """Coerce a caller-supplied language to a value we can safely store:
    a known config (case-insensitive), else :data:`FTS_SIMPLE`."""
    if not value:
        return FTS_SIMPLE
    candidate = value.strip().lower()
    return candidate if candidate in SUPPORTED_CONFIGS else FTS_SIMPLE
