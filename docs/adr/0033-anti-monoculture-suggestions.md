# ADR-0033 — Anti-monoculture safeguards in `garden_classify`

Status: Proposed
Date: 2026-05-27
Tracks: task `073ec753-994f-4d47-a092-606a25aa7495`
Depends on: ADR-0032 (`garden_classify`), ADR-0037 (online learning loop)

## Context

A classifier that learns from its own accepted suggestions tends to
collapse the distribution. The user picks the dominant tag because
it's offered first; the model raises its prior; the next call offers
it even more aggressively. After enough iterations the garden
sediments into a few super-tags and the link graph turns into a
star. The vision manifesto calls this the "humus turning into
cement" failure mode — it is the single biggest risk of the Phase 3
adaptive loop.

The safeguards must live *inside* the suggester, not as an opt-in
toggle: by the time the user notices the monoculture, the prior has
already shifted hard.

## Decision

Four mechanisms compose, each with a default that can be tuned but
never disabled completely.

### M1. Adamic-Adar in the *suggestion* score, not only the edge weight

The per-pair edge weight already discounts shared common tags via
`1 / log(2 + deg(tag))` (ADR-0031). The same discount extends to the
suggestion score: a candidate tag that already covers half the
workspace contributes proportionally less to its own confidence than
a rare candidate would.

```
conf_tag(c) = base_signal(c) * 1 / log(2 + deg(c))
```

### M2. Diversity bonus on top-K (MMR-style)

Maximal Marginal Relevance over the top-K candidates: each next pick
is rescored against the already-picked set so suggestions are
similar to the *seed* but diverse from *each other*.

```
score'(c, picked) = (1 - lambda) * score(c) - lambda * max_{p in picked} sim(c, p)
```

Default lambda = 0.3, capped K = 5. Tunable per workspace.

### M3. Saturating accept update

Inside ADR-0037, the learning rule applies a logistic saturating
factor: the first 10 accepts of a candidate move its prior fast; the
50th accept barely moves it. This is the local fix to feedback
runaway.

```
prior_{t+1} = prior_t + eta * (1 - 2 * sigmoid(prior_t)) * accept_signal
```

### M4. Epsilon-greedy cross-cluster exploration

With probability ε (default 0.10), one of the top-K slots is replaced
with a candidate from a *different* Leiden cluster than the dominant
one in the seed's neighbourhood. The candidate must still clear the
absolute confidence floor; we trade ranking position, not legitimacy.

Surfaced visually with a "trying something different" chip so the
user knows this slot came from exploration, not exploitation.

### M5. Biodiversity thermostat

The dashboard (ADR-0035) tracks Shannon entropy of tag distribution
in a 7-day rolling window. If it drops below a floor (`H < 1.2`) the
classifier auto-raises ε to 0.20 for the next 7 days; the user is
notified, not asked. The dashboard exposes a manual override.

## Consequences

- Suggestions are *less aggressive* about the obvious winner. Users
  who only want "give me the safe answer" can crank lambda down or
  raise the confidence floor.
- The exploration slot creates UX friction (occasional "what is
  this?" reactions). Mitigation: the rationale string is mandatory
  for exploration picks.
- Telemetry: the system must measure accept rate stratified by
  exploration vs exploitation; otherwise we can't tell whether ε is
  too high.
- Implementation cost is small (post-processing over the candidate
  list from ADR-0032); risk is mostly tuning, not engineering.

## Alternatives rejected

- **Opt-in diversity toggle.** Silent monoculture would have set in
  before the user knew to turn it on. Rejected.
- **Hard cap on a tag's frequency in suggestions.** Hard caps create
  cliffs (a popular tag suddenly invisible) that are worse UX than a
  soft decay.
- **Defer to the learning loop alone.** The loop's saturation (M3)
  is necessary but not sufficient: nothing forces the suggester to
  *propose* diverse candidates in the first place. Both layers are
  needed.

## Open question

Where to expose lambda / ε / thermostat thresholds: per-workspace
settings page, or buried under a "garden tuning" advanced panel? Lean
advanced panel — these are knobs the everyday user should never see.
