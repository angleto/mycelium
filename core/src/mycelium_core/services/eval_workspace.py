"""WS-EVAL T1: deterministic synthetic-workspace generator (protocol note
0cb0dda0 §1-§2, task c903ec2c).

Generates an "alberatura" (notes + tasks + typed links + tags + KG facts +
archive chains, mixed IT/EN, multi-persona) whose ground truth is known BY
CONSTRUCTION: a FACT REGISTRY records which unit(s) carry each fact, so the
retrieval harness never needs post-hoc annotation.

The adversarial-panel obligations are encoded as code, not prose:

- **No unique tuples** (§1.3): every queryable gold fact ships with >=2
  collision facts (same attribute, different value: a SIBLING entity of the
  same type and -- where the attribute is history-able -- a TEMPORAL previous
  value of the same entity), realized in other units.
- **Distributed facts** (§1.3): a pre-registered fraction of gold facts is
  split across two units, neither of which carries the full
  entity/attribute/value tuple (the value-bearing unit refers to the entity
  anaphorically and is ``related``-linked to the attribute-bearing unit).
- **Anaphora**: a fraction of single-unit gold realizations names the entity
  indirectly.
- **Anti-surface controls** (§2): gold-bearing and noise units are assembled
  by the SAME pipeline (noise units carry decoy facts at the same density and
  positions), and the generator self-checks that length / facts-per-unit /
  position-class distributions are indistinguishable (two-sample KS for the
  numeric ones, total-variation distance for the categorical one). A failed
  check regenerates with a derived seed (bounded retries) and the final
  p-values land in the manifest.
- **Decoy history** (§2): supersede history, tags and links are given to
  entities that are never queryable, so metadata cannot separate gold.
- **Metadata ablation** (§2): ``blank_content=True`` emits the same corpus
  with every fact-bearing string removed (text and titles blanked), for the
  "metadata-only retrieval ~ chance" check.
- **Determinism / reproducibility** (§1.6): the seed fully determines the
  artifacts; no wall-clock timestamps are embedded, so the same seed yields
  byte-identical corpus and registry files. The manifest carries SHA256 of
  both. Layer-2 LLM enrichment is a SEAM only (``Enricher`` protocol,
  ``NoopEnricher`` default): every unit records its generation matrix
  (provider/model/temperature/prompt), ``template-v1`` for this layer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.note import NoteKind
from mycelium_core.models.note_part_index_pointer import NotePartIndexPointer
from mycelium_core.models.tag import TagKind

SOURCE_KIND_WSEVAL = "wseval"
GENERATION_MATRIX_V1 = {
    "provider": "deterministic",
    "model": "template-v1",
    "temperature": None,
    "prompt": "wseval-templates-v1",
}

# Pre-registered quotas (protocol §1.3 / §3). Changing them is a protocol
# change and belongs to the T5 freeze, not to a run.
DISTRIBUTED_FRACTION = 0.20
ANAPHORA_FRACTION = 0.15
MIN_COLLISIONS_PER_GOLD = 2
QUERYABLE_FRACTION = 0.40

# Anti-surface self-check thresholds (§2): KS p-value floor for the numeric
# distributions, total-variation ceiling for position classes.
KS_MIN_P = 0.05
POSITION_TVD_MAX = 0.10
_MAX_REGEN_ATTEMPTS = 3

_POSITION_CLASSES = ("begin", "middle", "end", "table", "list")


# ── world schema ────────────────────────────────────────────────────────────

# entity_type -> attribute -> (label_it, label_en, history_able, value_kind)
_SCHEMA: dict[str, dict[str, tuple[str, str, bool, str]]] = {
    "person": {
        "ruolo": ("ruolo", "role", False, "role"),
        "tariffa": ("tariffa oraria", "hourly rate", True, "money"),
        "citta": ("città", "city", False, "city"),
        "telefono": ("telefono", "phone", False, "phone"),
    },
    "project": {
        "scadenza": ("scadenza", "deadline", True, "date"),
        "budget": ("budget", "budget", True, "money"),
        "repository": ("repository", "repository", False, "url"),
        "database": ("database", "database engine", False, "dbengine"),
        "referente": ("referente", "lead", True, "person"),
    },
    "client": {
        "partita_iva": ("partita IVA", "VAT number", False, "vat"),
        "rinnovo": ("rinnovo contratto", "contract renewal", True, "date"),
        "pagamento": ("condizioni di pagamento", "payment terms", False, "terms"),
    },
    "system": {
        "versione": ("versione", "version", True, "version"),
        "porta": ("porta", "port", False, "port"),
        "endpoint": ("endpoint", "endpoint", False, "url"),
        "backup": ("piano di backup", "backup schedule", False, "schedule"),
    },
}

_PERSON_NAMES = (
    "Giulia Ferri",
    "Marco Ricasoli",
    "Elena Vantini",
    "Paolo Grandi",
    "Sara Contini",
    "Luca Merisi",
    "Anna Foschi",
    "Davide Roveri",
    "Chiara Lanzi",
    "Stefano Baldi",
)
_PROJECT_NAMES = (
    "Aquilone",
    "Basalto",
    "Corindone",
    "Dolmen",
    "Ekfrasi",
    "Fenicottero",
    "Girasole",
    "Hekla",
)
_CLIENT_NAMES = ("Nordwind SRL", "Ostuni Digitale SPA")
_SYSTEM_NAMES = (
    "gateway-pagamenti",
    "collettore-metriche",
    "portale-clienti",
    "motore-ricerca",
    "coda-eventi",
    "registro-audit",
    "cache-sessioni",
    "bus-notifiche",
)
_CITIES = ("Milano", "Torino", "Bologna", "Firenze", "Bari", "Padova")
_ROLES_IT = ("sviluppatrice senior", "sistemista", "product owner", "analista dati")
_DB_ENGINES = ("PostgreSQL 16", "MariaDB 11", "MongoDB 7", "SQLite 3")
_TERMS = ("30 giorni fine mese", "60 giorni data fattura", "pagamento anticipato")
_SCHEDULES = ("ogni notte alle 02:00", "ogni domenica", "ogni 6 ore")
_GENERIC_TAGS = ("riunione", "decisione", "infrastruttura", "fatturazione", "onboarding")


@dataclasses.dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    name: str
    queryable: bool  # False = decoy: gets history/tags/links but no queries


@dataclasses.dataclass
class Fact:
    fact_id: str
    entity_id: str
    entity_name: str
    entity_type: str
    attribute: str
    value: str
    lang: str  # language of the PRIMARY realization
    category: str  # gold | collision_sibling | collision_temporal | decoy
    queryable: bool
    gold_unit_ids: list[str] = dataclasses.field(default_factory=list)
    distributed: bool = False
    anaphoric: bool = False
    position_class: str = "middle"
    kg: bool = False
    old_value: str | None = None  # temporal history (anti-recency pairs)
    stale_unit_id: str | None = None  # unit realizing old_value


@dataclasses.dataclass
class Unit:
    unit_id: str
    unit_kind: str  # note | task
    title: str
    text: str
    lang: str
    project: str
    client: str
    actor: str
    tags: list[str]
    links: list[dict[str, str]]  # {kind, to}
    archived: bool
    fact_ids: list[str]
    comments: list[str] = dataclasses.field(default_factory=list)
    generation: dict[str, Any] = dataclasses.field(
        default_factory=lambda: dict(GENERATION_MATRIX_V1)
    )


class Enricher(Protocol):
    """Layer-2 seam (§2): turns template text into richer prose WITHOUT
    changing the facts. Implementations must record their generation matrix
    on the unit. The deterministic layer uses :class:`NoopEnricher`."""

    def enrich(self, unit: Unit, facts: Sequence[Fact]) -> Unit: ...


class NoopEnricher:
    def enrich(self, unit: Unit, facts: Sequence[Fact]) -> Unit:
        return unit


# ── value generators ────────────────────────────────────────────────────────


def _value(rng: random.Random, kind: str, taken: set[str]) -> str:
    for _ in range(64):
        if kind == "date":
            v = f"{rng.choice(['15', '22', '03', '28', '09'])}/{rng.randint(1, 12):02d}/2026"
        elif kind == "money":
            v = f"{rng.randrange(40, 220) * 25} EUR"
        elif kind == "phone":
            v = f"+39 0{rng.randint(2, 9)} {rng.randrange(2000000, 9999999)}"
        elif kind == "url":
            sub = rng.choice(["git", "staging", "api", "docs"])
            v = f"https://{sub}.example-{rng.randrange(10, 99)}.it"
        elif kind == "vat":
            v = f"IT{rng.randrange(10**10, 10**11 - 1)}"
        elif kind == "version":
            v = f"{rng.randint(1, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}"
        elif kind == "port":
            v = str(rng.randrange(1024, 65000))
        elif kind == "city":
            v = rng.choice(_CITIES)
        elif kind == "role":
            v = rng.choice(_ROLES_IT)
        elif kind == "dbengine":
            v = rng.choice(_DB_ENGINES)
        elif kind == "terms":
            v = rng.choice(_TERMS)
        elif kind == "schedule":
            v = rng.choice(_SCHEDULES)
        elif kind == "person":
            v = rng.choice(_PERSON_NAMES)
        else:  # pragma: no cover - schema is closed
            raise ValueError(f"unknown value kind {kind!r}")
        if v not in taken:
            taken.add(v)
            return v
    # Small pools (city/role/...) legitimately repeat across entities.
    return v


# ── text realization (template-v1) ──────────────────────────────────────────

# Sentence frames: {label} attribute label, {e} entity name, {v} value.
_FACT_FRAMES = {
    "it": (
        "La {label} di {e} è {v}.",
        "Abbiamo confermato che per {e} la {label} è {v}.",
        "Aggiornamento: {e}, {label} → {v}.",
        "Dopo il confronto di ieri, la {label} di {e} resta {v}.",
    ),
    "en": (
        "The {label} for {e} is {v}.",
        "We confirmed that {e} has {label} {v}.",
        "Update: {e}, {label} → {v}.",
        "After yesterday's review, the {label} of {e} stays {v}.",
    ),
}
_ANAPHORIC_FRAMES = {
    "it": (
        "Per il {etype_it} di cui sopra la {label} è {v}.",
        "Come discusso, la {label} in questione è {v}.",
    ),
    "en": (
        "For the {etype_en} mentioned above, the {label} is {v}.",
        "As discussed, the {label} in question is {v}.",
    ),
}
_DISTRIBUTED_A_FRAMES = {
    "it": ("Per {e} abbiamo rivisto la {label}; il valore definitivo è nella nota collegata.",),
    "en": ("We revised the {label} for {e}; the final value is in the linked note.",),
}
_DISTRIBUTED_B_FRAMES = {
    "it": ("Come da nota collegata, il valore definitivo della {label} è {v}.",),
    "en": ("As per the linked note, the final {label} value is {v}.",),
}
_ETYPE_LABELS = {
    "person": ("collaboratore", "person"),
    "project": ("progetto", "project"),
    "client": ("cliente", "client"),
    "system": ("sistema", "system"),
}
_FILLER = {
    "it": (
        "La riunione di allineamento è andata lunga ma il clima resta buono.",
        "Restano da verbalizzare due decisioni della scorsa settimana.",
        "Il backlog è stato ripulito dalle voci duplicate.",
        "Da rivedere la checklist di onboarding con il team.",
        "Le metriche del trimestre verranno discusse al prossimo stato avanzamento.",
        "Il documento di architettura ha bisogno di una passata di revisione.",
    ),
    "en": (
        "The alignment meeting ran long but morale is fine.",
        "Two decisions from last week still need minutes.",
        "The backlog was cleaned of duplicate entries.",
        "The onboarding checklist needs a review with the team.",
        "Quarterly metrics will be discussed at the next status meeting.",
        "The architecture document needs a review pass.",
    ),
}


def _fact_sentence(rng: random.Random, fact: Fact, lang: str) -> str:
    label = _SCHEMA[fact.entity_type][fact.attribute][0 if lang == "it" else 1]
    if fact.anaphoric:
        frame = rng.choice(_ANAPHORIC_FRAMES[lang])
        it_l, en_l = _ETYPE_LABELS[fact.entity_type]
        return frame.format(label=label, v=fact.value, etype_it=it_l, etype_en=en_l)
    frame = rng.choice(_FACT_FRAMES[lang])
    return frame.format(label=label, e=fact.entity_name, v=fact.value)


def _fact_block(rng: random.Random, fact: Fact, lang: str, position: str) -> str:
    label = _SCHEMA[fact.entity_type][fact.attribute][0 if lang == "it" else 1]
    if position == "table":
        head = "| campo | valore |" if lang == "it" else "| field | value |"
        return f"{head}\n|---|---|\n| {label} {fact.entity_name} | {fact.value} |"
    if position == "list":
        return f"- {label} {fact.entity_name}: {fact.value}"
    return _fact_sentence(rng, fact, lang)


def _assemble_text(
    rng: random.Random,
    facts: Sequence[Fact],
    positions: Sequence[str],
    lang: str,
) -> str:
    """One assembly path for EVERY unit (gold and noise): filler sentences
    with fact blocks spliced at their position class. This shared path is
    what makes the anti-surface KS checks pass by construction."""
    n_filler = rng.randint(3, 7)
    filler = [rng.choice(_FILLER[lang]) for _ in range(n_filler)]
    begin: list[str] = []
    middle: list[str] = []
    end: list[str] = []
    for fact, pos in zip(facts, positions, strict=True):
        block = _fact_block(rng, fact, lang, pos)
        if pos == "begin":
            begin.append(block)
        elif pos == "end":
            end.append(block)
        else:  # middle, table, list all live mid-document
            middle.append(block)
    mid_cut = max(1, len(filler) // 2)
    parts = [*begin, *filler[:mid_cut], *middle, *filler[mid_cut:], *end]
    return "\n\n".join(parts)


# ── generator ───────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Workspace:
    seed: int
    scale: int
    units: list[Unit]
    facts: list[Fact]
    entities: list[Entity]
    ks_report: dict[str, float]
    attempts: int


def _stable_id(prefix: str, n: int) -> str:
    return f"{prefix}-{n:05d}"


def _ks_two_sample(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sample KS asymptotic p-value (Kolmogorov series). Enough for a
    generator self-check; avoids a scipy dependency. The statistic is
    evaluated only at the UNIQUE observed values: measuring inside a block
    of ties (heavily present in the discrete facts-per-unit distribution)
    would inflate D with a spurious mid-tie gap."""
    if not a or not b:
        return 1.0
    xs = sorted(a)
    ys = sorted(b)
    d = 0.0
    for v in sorted(set(xs) | set(ys)):
        cdf_x = sum(1 for x in xs if x <= v) / len(xs)
        cdf_y = sum(1 for y in ys if y <= v) / len(ys)
        d = max(d, abs(cdf_x - cdf_y))
    en = math.sqrt(len(xs) * len(ys) / (len(xs) + len(ys)))
    lam = (en + 0.12 + 0.11 / en) * d
    p: float = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * (lam * k) ** 2) for k in range(1, 101))
    return max(0.0, min(1.0, p))


def _tvd(a: Sequence[str], b: Sequence[str], classes: Sequence[str]) -> float:
    if not a or not b:
        return 0.0
    return 0.5 * sum(abs(a.count(c) / len(a) - b.count(c) / len(b)) for c in classes)


def generate_workspace(
    *,
    seed: int,
    scale: int,
    locale_mix: float = 0.5,
    enricher: Enricher | None = None,
) -> Workspace:
    """Generate an alberatura of ~``scale`` units. Fully determined by
    ``seed`` (retries use seed+1000*attempt, recorded in the manifest)."""
    last: Workspace | None = None
    for attempt in range(_MAX_REGEN_ATTEMPTS):
        ws = _generate_once(
            seed=seed + 1000 * attempt, scale=scale, locale_mix=locale_mix, attempt=attempt + 1
        )
        if (
            ws.ks_report["ks_length_p"] >= KS_MIN_P
            and ws.ks_report["ks_density_p"] >= KS_MIN_P
            and ws.ks_report["position_tvd"] <= ws.ks_report["position_tvd_allowed"]
        ):
            last = ws
            break
        last = ws
    if last is None:  # pragma: no cover - _MAX_REGEN_ATTEMPTS >= 1
        raise RuntimeError("workspace generation produced no candidate")
    if enricher is not None:
        facts_by_unit = {
            u.unit_id: [
                f for f in last.facts if u.unit_id in f.gold_unit_ids or f.fact_id in u.fact_ids
            ]
            for u in last.units
        }
        enriched = [enricher.enrich(u, facts_by_unit.get(u.unit_id, [])) for u in last.units]
        last = dataclasses.replace(last, units=enriched)
    return last


def _generate_once(*, seed: int, scale: int, locale_mix: float, attempt: int) -> Workspace:
    rng = random.Random(seed)  # noqa: S311 - deterministic corpus, not crypto
    taken: set[str] = set()

    # World: actors, clients, projects, entities.
    actors = list(rng.sample(_PERSON_NAMES, 4))
    clients = list(_CLIENT_NAMES)
    n_projects = min(len(_PROJECT_NAMES), max(3, scale // 40))
    projects = list(rng.sample(_PROJECT_NAMES, n_projects))

    entities: list[Entity] = []
    n_person = max(4, scale // 30)
    n_system = max(4, scale // 30)
    eid = 0

    def _mk(etype: str, name: str) -> Entity:
        nonlocal eid
        eid += 1
        return Entity(
            entity_id=_stable_id("ent", eid),
            entity_type=etype,
            name=name,
            queryable=rng.random() < QUERYABLE_FRACTION,
        )

    for i in range(n_person):
        first = rng.choice(_PERSON_NAMES).split()[0]
        surname = rng.choice(["Novara", "Tessari", "Ligabue", "Marconi", "Petri", "Sorbi"])
        suffix = "" if i < 30 else f" {i}"
        entities.append(_mk("person", f"{first} {surname}{suffix}"))
    for name in projects:
        entities.append(_mk("project", name))
    for name in clients:
        entities.append(_mk("client", name))
    for _ in range(n_system):
        entities.append(_mk("system", f"{rng.choice(_SYSTEM_NAMES)}-{rng.randint(2, 99)}"))

    # Facts: every entity gets most of its schema attributes; queryable
    # entities produce gold facts, decoys produce decoy facts (SAME pipeline).
    facts: list[Fact] = []
    fid = 0

    def _mk_fact(ent: Entity, attribute: str, value: str, category: str, lang: str) -> Fact:
        nonlocal fid
        fid += 1
        return Fact(
            fact_id=_stable_id("fact", fid),
            entity_id=ent.entity_id,
            entity_name=ent.name,
            entity_type=ent.entity_type,
            attribute=attribute,
            value=value,
            lang=lang,
            category=category,
            queryable=ent.queryable and category == "gold",
        )

    by_type: dict[str, list[Entity]] = {}
    for ent in entities:
        by_type.setdefault(ent.entity_type, []).append(ent)

    for ent in entities:
        schema = _SCHEMA[ent.entity_type]
        attrs = rng.sample(sorted(schema), k=max(2, len(schema) - 1))
        for attribute in attrs:
            _label_it, _label_en, history_able, vkind = schema[attribute]
            lang = "it" if rng.random() < locale_mix else "en"
            category = "gold" if ent.queryable else "decoy"
            fact = _mk_fact(ent, attribute, _value(rng, vkind, taken), category, lang)
            # Temporal history: gold facts (anti-recency pairs) AND a share of
            # decoys (decoy history obligation §2).
            if history_able and (ent.queryable or rng.random() < 0.5):
                fact.old_value = _value(rng, vkind, taken)
            fact.kg = ent.entity_type in ("project", "person") and attribute in (
                "referente",
                "ruolo",
            )
            facts.append(fact)

    gold_facts = [f for f in facts if f.queryable]

    # Collisions (§1.3): sibling same-attribute facts. The sibling fact is a
    # normal fact of another entity (it may itself be gold or decoy) -- what
    # matters is that it EXISTS in the corpus with the same attribute.
    for gf in gold_facts:
        siblings = [e for e in by_type[gf.entity_type] if e.entity_id != gf.entity_id]
        have = sum(
            1 for f in facts if f.attribute == gf.attribute and f.entity_id != gf.entity_id
        ) + (1 if gf.old_value else 0)
        need = MIN_COLLISIONS_PER_GOLD - have
        for k in range(max(0, need)):
            sib = siblings[k % len(siblings)]
            vkind = _SCHEMA[gf.entity_type][gf.attribute][3]
            facts.append(
                _mk_fact(sib, gf.attribute, _value(rng, vkind, taken), "collision_sibling", gf.lang)
            )

    # Distributed + anaphora quotas over gold facts.
    n_distributed = round(len(gold_facts) * DISTRIBUTED_FRACTION)
    for gf in rng.sample(gold_facts, k=n_distributed):
        gf.distributed = True
    single_gold = [f for f in gold_facts if not f.distributed]
    for gf in rng.sample(single_gold, k=round(len(single_gold) * ANAPHORA_FRACTION)):
        gf.anaphoric = True

    # ── unit assembly (single shared path) ─────────────────────────────────
    units: list[Unit] = []
    uid = 0

    def _new_unit(
        lang: str,
        kind: str,
        project: str,
        facts_in: list[Fact],
        positions: list[str],
        archived: bool = False,
    ) -> Unit:
        nonlocal uid
        uid += 1
        unit_id = _stable_id("unit", uid)
        text = _assemble_text(rng, facts_in, positions, lang)
        title_src = facts_in[0].entity_name if facts_in else rng.choice(_FILLER[lang])
        title_pool = (
            ["Note", "Verbale", "Stato", "Log"]
            if lang == "it"
            else [
                "Notes",
                "Minutes",
                "Status",
                "Log",
            ]
        )
        title = f"{rng.choice(title_pool)} {title_src}"
        client = (
            clients[projects.index(project) % len(clients)] if project in projects else clients[0]
        )
        tags = [rng.choice(_GENERIC_TAGS)] if rng.random() < 0.6 else []
        u = Unit(
            unit_id=unit_id,
            unit_kind=kind,
            title=title,
            text=text,
            lang=lang,
            project=project,
            client=client,
            actor=rng.choice(actors),
            tags=tags,
            links=[],
            archived=archived,
            fact_ids=[f.fact_id for f in facts_in],
        )
        for f, pos in zip(facts_in, positions, strict=True):
            f.position_class = pos
        units.append(u)
        return u

    def _positions(n: int) -> list[str]:
        return [rng.choice(_POSITION_CLASSES) for _ in range(n)]

    # Decoy-fact factory: structural and filler slots draw fresh facts on
    # NON-queryable entities so every unit goes through the same fact-bearing
    # assembly (a zero-fact unit would be separable by length/density).
    decoy_entities = [e for e in entities if not e.queryable] or entities

    def _mk_decoy_fact(lang: str) -> Fact:
        ent = rng.choice(decoy_entities)
        attribute = rng.choice(sorted(_SCHEMA[ent.entity_type]))
        vkind = _SCHEMA[ent.entity_type][attribute][3]
        fact = _mk_fact(ent, attribute, _value(rng, vkind, taken), "decoy", lang)
        facts.append(fact)
        return fact

    # Pack facts into units: the unit's fact DENSITY is drawn first and
    # independently of what the unit hosts, then exactly ONE primary fact
    # (gold, collision or decoy) fills the first slot and decoy fillers take
    # the rest. Packing several primaries per unit would make gold-bearing
    # units systematically denser (a unit with more slots is more likely to
    # catch a gold fact -- size-biased sampling), which is precisely the
    # anti-surface leak §2 forbids.
    pending = facts[:]
    rng.shuffle(pending)
    while pending:
        primary_fact = pending.pop()
        density = rng.randint(1, 3)
        batch: list[Fact] = [primary_fact]
        for _ in range(density - 1):
            batch.append(_mk_decoy_fact(primary_fact.lang))
        lang = primary_fact.lang
        kind = "note" if rng.random() < 0.7 else "task"
        project = rng.choice(projects)
        primary = _new_unit(lang, kind, project, batch, _positions(len(batch)))
        for f in batch:
            f.gold_unit_ids.append(primary.unit_id)

    # Distributed facts: replace the value in the primary unit with the
    # A-frame and add a B-side unit (anaphoric value bearer, related-linked).
    unit_by_id = {u.unit_id: u for u in units}
    for f in facts:
        if not f.distributed:
            continue
        primary = unit_by_id[f.gold_unit_ids[0]]
        label = _SCHEMA[f.entity_type][f.attribute][0 if primary.lang == "it" else 1]
        a_frame = rng.choice(_DISTRIBUTED_A_FRAMES[primary.lang]).format(
            label=label, e=f.entity_name
        )
        primary.text = primary.text.replace(f.value, "").strip()
        primary.text = f"{a_frame}\n\n{primary.text}"
        b_lang = f.lang
        b_text_frame = rng.choice(_DISTRIBUTED_B_FRAMES[b_lang]).format(
            label=_SCHEMA[f.entity_type][f.attribute][0 if b_lang == "it" else 1], v=f.value
        )
        b_fillers = [_mk_decoy_fact(b_lang) for _ in range(rng.randint(0, 2))]
        b = _new_unit(b_lang, "note", primary.project, b_fillers, _positions(len(b_fillers)))
        for bf in b_fillers:
            bf.gold_unit_ids.append(b.unit_id)
        b.text = f"{b_text_frame}\n\n{b.text}"
        b.fact_ids.append(f.fact_id)
        b.links.append({"kind": "related", "to": primary.unit_id})
        f.gold_unit_ids.append(b.unit_id)

    # Temporal history: realize the OLD value in its own unit, linked with
    # ``supersedes`` from the fresh one (anti-recency raw material).
    for f in list(facts):
        if not f.old_value:
            continue
        stale = Fact(
            fact_id=f"{f.fact_id}-old",
            entity_id=f.entity_id,
            entity_name=f.entity_name,
            entity_type=f.entity_type,
            attribute=f.attribute,
            value=f.old_value,
            lang=f.lang,
            category="collision_temporal",
            queryable=False,
        )
        facts.append(stale)
        s_batch = [stale] + [_mk_decoy_fact(f.lang) for _ in range(rng.randint(0, 2))]
        su = _new_unit(f.lang, "note", rng.choice(projects), s_batch, _positions(len(s_batch)))
        for sb in s_batch[1:]:
            sb.gold_unit_ids.append(su.unit_id)
        stale.gold_unit_ids.append(su.unit_id)
        f.stale_unit_id = su.unit_id
        fresh_unit = unit_by_id.get(f.gold_unit_ids[0]) if f.gold_unit_ids else None
        if fresh_unit is not None and fresh_unit.unit_kind == "note":
            fresh_unit.links.append({"kind": "supersedes", "to": su.unit_id})

    # Related links inside the same project + hypha_of synthesis chains over
    # archived sources (structure for graph/humus scenarios). Synthesis notes
    # carry decoy facts through the standard density draw: any structural
    # unit with a different density profile would reopen the §2 leak.
    note_units = [u for u in units if u.unit_kind == "note"]
    by_project: dict[str, list[Unit]] = {}
    for u in note_units:
        by_project.setdefault(u.project, []).append(u)
    for group in by_project.values():
        for u in group:
            if len(group) > 1 and rng.random() < 0.4:
                other = rng.choice([g for g in group if g.unit_id != u.unit_id])
                u.links.append({"kind": "related", "to": other.unit_id})
        if len(group) >= 4 and rng.random() < 0.5:
            sources = rng.sample(group, 2)
            sbatch = [_mk_decoy_fact(group[0].lang) for _ in range(rng.randint(1, 3))]
            synth = _new_unit(
                group[0].lang, "note", group[0].project, sbatch, _positions(len(sbatch))
            )
            for sf in sbatch:
                sf.gold_unit_ids.append(synth.unit_id)
            synth.title = f"Sintesi {group[0].project}"
            for s in sources:
                synth.links.append({"kind": "hypha_of", "to": s.unit_id})
                s.archived = True

    # Fill to scale through the same fact-bearing path (decoy facts).
    while len(units) < scale:
        lang = "it" if rng.random() < locale_mix else "en"
        batch = [_mk_decoy_fact(lang) for _ in range(rng.randint(1, 3))]
        fill = _new_unit(
            lang,
            "note" if rng.random() < 0.7 else "task",
            rng.choice(projects),
            batch,
            _positions(len(batch)),
        )
        for bf in batch:
            bf.gold_unit_ids.append(fill.unit_id)

    # ── anti-surface self-check (§2) ────────────────────────────────────────
    gold_units = {uid for f in facts if f.queryable for uid in f.gold_unit_ids}
    gold_lens: list[float] = []
    noise_lens: list[float] = []
    gold_density: list[float] = []
    noise_density: list[float] = []
    gold_pos: list[str] = []
    noise_pos: list[str] = []
    pos_by_fact = {f.fact_id: f.position_class for f in facts}
    for u in units:
        (gold_lens if u.unit_id in gold_units else noise_lens).append(float(len(u.text)))
        (gold_density if u.unit_id in gold_units else noise_density).append(float(len(u.fact_ids)))
        for fidd in u.fact_ids:
            bucket = gold_pos if u.unit_id in gold_units else noise_pos
            bucket.append(pos_by_fact.get(fidd, "middle"))
    # The TVD between two finite samples of the SAME distribution is not
    # zero: its expected order is sqrt(k / 4n). The ceiling is therefore the
    # pre-registered floor OR the sampling error, whichever is larger -- at
    # protocol scales (1k+) the 0.10 floor dominates and the check is strict.
    n_min = max(1, min(len(gold_pos), len(noise_pos)))
    tvd_allowed = max(POSITION_TVD_MAX, math.sqrt(len(_POSITION_CLASSES) / (2.0 * n_min)))
    ks_report = {
        "ks_length_p": round(_ks_two_sample(gold_lens, noise_lens), 4),
        "ks_density_p": round(_ks_two_sample(gold_density, noise_density), 4),
        "position_tvd": round(_tvd(gold_pos, noise_pos, _POSITION_CLASSES), 4),
        "position_tvd_allowed": round(tvd_allowed, 4),
    }

    return Workspace(
        seed=seed,
        scale=scale,
        units=units,
        facts=facts,
        entities=entities,
        ks_report=ks_report,
        attempts=attempt,
    )


# ── artifacts ───────────────────────────────────────────────────────────────


def _blank_unit(u: Unit) -> Unit:
    return dataclasses.replace(u, title=u.unit_id, text="", comments=[])


def write_artifacts(ws: Workspace, out_dir: Path, *, blank_content: bool = False) -> dict[str, Any]:
    """Write corpus.jsonl / registry.jsonl / manifest.json. No wall-clock
    timestamps anywhere: same seed => byte-identical corpus and registry."""
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "corpus.jsonl"
    registry_path = out_dir / "registry.jsonl"
    units = [_blank_unit(u) if blank_content else u for u in ws.units]
    with corpus_path.open("w", encoding="utf-8") as fh:
        for u in units:
            fh.write(json.dumps(dataclasses.asdict(u), ensure_ascii=False, sort_keys=True) + "\n")
    with registry_path.open("w", encoding="utf-8") as fh:
        for f in ws.facts:
            fh.write(json.dumps(dataclasses.asdict(f), ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "generator": "wseval-t1",
        "generation_matrix": GENERATION_MATRIX_V1,
        "seed": ws.seed,
        "scale": ws.scale,
        "attempts": ws.attempts,
        "blank_content": blank_content,
        "counts": {
            "units": len(ws.units),
            "facts": len(ws.facts),
            "gold_facts": sum(1 for f in ws.facts if f.queryable),
            "distributed": sum(1 for f in ws.facts if f.distributed),
            "anaphoric": sum(1 for f in ws.facts if f.anaphoric),
            "collisions": sum(1 for f in ws.facts if f.category.startswith("collision")),
            "decoy_facts": sum(1 for f in ws.facts if f.category == "decoy"),
            "entities": len(ws.entities),
        },
        "quotas": {
            "distributed_fraction": DISTRIBUTED_FRACTION,
            "anaphora_fraction": ANAPHORA_FRACTION,
            "min_collisions_per_gold": MIN_COLLISIONS_PER_GOLD,
        },
        "ks_report": ws.ks_report,
        "sha256": {
            "corpus.jsonl": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            "registry.jsonl": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


# ── ingestor ────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class IngestResult:
    """``units``: unit_id -> {kind, note_id|task_id}. ``project_ids`` matter
    downstream: note blobs are project-scoped, so retrieval MUST pass the
    project id (``memory._project_pred``: None means project IS NULL, not
    'no filter' -- the documented harness trap)."""

    units: dict[str, dict[str, str]]
    project_ids: dict[str, uuid.UUID]
    client_ids: dict[str, uuid.UUID]


async def ingest_workspace(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    ws: Workspace,
) -> IngestResult:
    """Load a generated workspace into a live org through the REAL service
    layer (notes/tasks/links/tags/archive/KG), so scenarios and the retrieval
    harness run against genuinely ingested data. Note parts are indexed by
    the note_search commit hook of ``tenant_session`` -- retrievability
    checks must run in a LATER session."""
    from mycelium_core.services import kg as kg_svc
    from mycelium_core.services import note_links as link_svc
    from mycelium_core.services import notes as notes_svc
    from mycelium_core.services import tasks as tasks_svc
    from mycelium_core.services import taxonomy as tax_svc

    client_ids: dict[str, uuid.UUID] = {}
    project_ids: dict[str, uuid.UUID] = {}
    clients = {u.client for u in ws.units}
    projects = {(u.project, u.client) for u in ws.units}
    for name in sorted(clients):
        tag = await tax_svc.create_tag(
            session, org_id=org_id, actor_id=actor_id, kind=TagKind.client, name=name
        )
        client_ids[name] = tag.id
    for pname, cname in sorted(projects):
        tag = await tax_svc.create_project(
            session, org_id=org_id, actor_id=actor_id, name=pname, client_tag_id=client_ids[cname]
        )
        project_ids[pname] = tag.id

    mapping: dict[str, dict[str, str]] = {}
    note_ids: dict[str, uuid.UUID] = {}
    for u in ws.units:
        if u.unit_kind == "note":
            note = await notes_svc.create_note(
                session,
                org_id=org_id,
                actor_id=actor_id,
                kind=NoteKind.text,
                project_id=project_ids[u.project],
                title=u.title,
                text=u.text or u.title,
            )
            note_ids[u.unit_id] = note.id
            mapping[u.unit_id] = {"kind": "note", "note_id": str(note.id)}
            if u.archived:
                await notes_svc.archive_note(
                    session,
                    org_id=org_id,
                    actor_id=actor_id,
                    note_id=note.id,
                    expected_version=note.version,
                )
        else:
            task = await tasks_svc.create_task(
                session,
                org_id=org_id,
                actor_id=actor_id,
                title=u.title,
                description=u.text or None,
                tag_ids=[project_ids[u.project]],
            )
            for body in u.comments:
                await tasks_svc.add_comment(
                    session, org_id=org_id, actor_id=actor_id, task_id=task.id, body=body
                )
            mapping[u.unit_id] = {"kind": "task", "task_id": str(task.id)}

    # Deterministic dedupe on the canonical edge (undirected kinds collapse
    # (a, b)/(b, a)): real service errors must RAISE, never be swallowed.
    seen_edges: set[tuple[str, str, str]] = set()
    for u in ws.units:
        if u.unit_kind != "note":
            continue
        for link in u.links:
            target = note_ids.get(link["to"])
            if target is None or target == note_ids.get(u.unit_id):
                continue
            # hypha_of: parent = source (origin), child = derived unit.
            parent, child = (target, note_ids[u.unit_id])
            if link["kind"] == "supersedes":
                parent, child = note_ids[u.unit_id], target
            if link["kind"] == "related":
                a, b = sorted((str(parent), str(child)))
                key = ("related", a, b)
            else:
                key = (link["kind"], str(parent), str(child))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            await link_svc.link_notes(
                session,
                org_id=org_id,
                actor_id=actor_id,
                parent_note_id=parent,
                child_note_id=child,
                kind=link["kind"],
            )

    for f in ws.facts:
        if not f.kg or not f.gold_unit_ids:
            continue
        subj = await kg_svc.ensure_entity(
            session, org_id=org_id, name=f.entity_name, entity_type=f.entity_type
        )
        obj = await kg_svc.ensure_entity(session, org_id=org_id, name=f.value, entity_type="other")
        await kg_svc.add_fact(
            session,
            org_id=org_id,
            subject_id=subj.id,
            predicate=f.attribute,
            object_id=obj.id,
            source_note_id=note_ids.get(f.gold_unit_ids[0]),
        )
    return IngestResult(units=mapping, project_ids=project_ids, client_ids=client_ids)


async def resolve_unit_blobs(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> list[uuid.UUID]:
    """Blob ids serving a note unit (via the note_search index pointers)."""
    rows = (
        await session.execute(
            select(NotePartIndexPointer.blob_id).where(
                NotePartIndexPointer.org_id == org_id,
                NotePartIndexPointer.note_id == note_id,
            )
        )
    ).scalars()
    return list(rows)
