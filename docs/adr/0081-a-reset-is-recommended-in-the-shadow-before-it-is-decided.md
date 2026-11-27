# ADR-0081: A reset is recommended in the shadow before anything decides it

Status: Proposed (2026-11-27; Angelo chose option 1 of the analysis in note
ac43fe59, project engineering-handbook: an instrument that only observes)
Relates to: ADR-0078 (the session hooks this runs inside, and their two
properties), ADR-0049 (working memory stays with the caller: the log is
local), ADR-0063 (possession, which is what stops two sessions resuming the
same work and is not this record's subject).

This record decides how Mycelium learns when a coding-agent session should
continue, compact its history, restart clean or delegate. At every prompt it
records a decision rule's recommendation beside what the session then did,
and it acts on nothing. It sets out the rule and where each of its numbers
comes from, what is recorded and what never is, who allows it, and what the
record cannot conclude.

## Context

Angelo, 2026-11-27: deciding when to start a new session should not be his
call. It should come out of a computation, and the computation should rest on
the information-theoretic model of memory being built in the essay on the use
and harm of history for language models, not on a classifier trained on past
decisions.

The model (thinktank, `utitlita_e_danno/compromesso_informazione.md` and
`formal/T4_sufficient_summary.md`) turns the choice into three terms:

- **anchoring**: the history is worth `g` on work it fits and costs `l` on
  work it does not fit. Only a clean restart changes this term. A summary that
  keeps what the history says about the work keeps the decision on every
  history, and so keeps the loss as well (T4a). A summary that drops something
  loses at least as much on the work the history fits (T4c). Restart against
  continue is a two-hypothesis test whose irreducible price is
  `G = sum_o min(phi g P_F(o), (1 - phi) l P_N(o)) <= (1 - phi) l` (Theorem 4,
  `LECAM_VALUE` in symbolum);
- **dilution**: what length alone costs a reader with limits. For an ideal
  reader no restart improves the average (Theorem 1, `BLACKWELL_FILTER`), so
  any average gain from a restart is a limit of the reader;
- **cost**: what the remaining calls pay to read the context again.

What is measured, 2026-11-27:

| term | measurement | value |
|---|---|---|
| anchoring, Opus 5.5 | memoria_v7 hardened pilot, Df `cont` | 35/36; 95% upper bound of the failure rate 0.122 |
| dilution, Opus 5.5 | same, `far` at about 375k | 3/3 per arm; upper bound 0.35 |
| history value | same, S `fresh_clean` and one family of Dn | positive, by construction in S |
| model of the responses | 199 sessions (memoria_v7 DESIGN section 1) | Opus 5.5 in 132,713 of 132,894 |
| cache lifetime written by Claude Code | every `cache_creation` in this machine's transcripts | one hour, priced at 2x input |
| input cost split, Opus 5.5 prices | 9,663 main-thread calls on this machine | cache reads 76%, cache writes 24% |
| model calls still to come in a session | 60 sessions, 9,763 calls | median 99 to 144 from any point measured; 124 used |
| context after a compaction | 11 compactions | median 68,420 |

Both quality terms are indistinguishable from zero on the one model measured,
on a synthetic bench, and neither is measured on real sessions. The cost term
is measured, and it is not small. At 300k of context, continuing reads about
$7.44 of cache over the calls to come. Compacting costs about $2.66 including
the compaction. On these numbers alone compaction pays from about 105k.

## Decision

**A rule, recorded, never obeyed.** `mycelium_cli.reset_rule.recommend` takes
what the hook can see at a prompt (model, context length, whether something
failed since the previous prompt) and returns `continue` or `compact` with
the cost margin. It also returns the basis of the recommendation:

- `cost_quality_null`: the quality terms were measured for this model and did
  not separate from the ceiling;
- `cost_quality_unmeasured`: they were never measured for this model.

A model whose quality terms are measured and not null is refused with an
error. Deciding it on cost alone would be the silent default this record
exists to avoid. A model with no price gets no recommendation.

Two actions are never recommended:

- **A clean restart.** Its cost is close to a compaction's and it drops the
  history's value. The theory gives it one advantage over a compaction, the
  anchoring term, and that term is null or unmeasured for every model in the
  table.
- **A delegation.** Whether work splits into independent pieces is not
  visible to a hook.

Both are recorded when they happen.

**One table of numbers.** Every value the rule reads is in
`reset_rule.PARAMETERS`, next to the measurement it comes from, under a
`version` that every record carries. A change of table is a new version, so
a log that spans two can be split.

**The recorder** (`mycelium_cli.reset_shadow`) runs inside `mycelium-hook`,
before the attention report, in its own `try`. What it records per event:

- `UserPromptSubmit`: it reads the transcript from the byte offset where the
  previous prompt stopped. Main-thread calls are counted once per message id,
  and a half-written last line is left for next time. It records the context
  of the last call, the call and prompt counts, whether `HEAD` moved, whether
  something failed, and the recommendation.
- `PreCompact`: the compaction and its trigger (manual or automatic).
- `SessionStart`: its source (`clear` is a clean restart).
- `PostToolUse`: an `Agent` or `Task` call, recorded as a delegation.
- `PostToolUseFailure` and `StopFailure`: mark the next prompt as after a
  failure.
- `SessionEnd`: ends the session.

`install-hooks` now also adds `PreCompact`, async, because only its payload
tells a manual compaction from an automatic one.

**What is never recorded.** Prompt text, file content and paths. The project
is named the way `agent_host.project_name` names it for the sessions page.

**Where it lives.** `<state dir>/reset_shadow/`, mode 0700, on the machine
only: ADR-0049, and the log describes a person's work in projects some of
which are a client's. Nothing is sent to the server. The log rotates past
40 MB, keeping one previous file, which is roughly the last 100,000 prompts.

**Who allows it.** Nothing is recorded for a directory that is not under a
root added with `mycelium session reset-shadow allow PATH`. Without the
allow file the recorder costs one `stat`. `deny` removes a root. `list`
shows them. `summary` pairs each prompt's recommendation with what the
session did before the next prompt (continue, compaction by trigger, clean
restart, delegation, end), per basis. It prints the quality upper bounds
beside the table, so a cost recommendation is never read as a quality one.

## Consequences

- The data needed to estimate `g`, `l` and the dilution term on real sessions
  start to exist, which memoria_v7 B0 required and the bench cannot give.
  Joining a prompt to whether the person then corrected the session needs
  the person's labels (memoria_v7 DESIGN section 6). The log records the
  numbers that label is joined to, and contains no text.
- The first summary will show how far the rule's cost answer is from what is
  done. Automatic compaction triggers between 200k and 300k, and the cost
  break-even is about 105k, so most `compact` recommendations will be paired
  with `continue`. That gap is a cost the person is paying today, or a
  quality cost of compaction the rule does not see. Telling the two apart is
  the analysis this record makes possible. The record itself does not do it.
- The rule becomes a rule of the engineering handbook only after that
  analysis, with the measurements in its rationale.

## Alternatives rejected

- **A trained decider** (for example a hosted classification API). A rule
  that learned its threshold on past sessions is optimal only for the past
  frequency of new work. Over 20,000 exact-fraction instances with that
  frequency shifted, the frozen rule lost in 10,978, and a calibrated
  posterior corrected for the shift lost in none (note ac43fe59, section 3).
  So a model is useful here at most as an estimator of the probability that
  the work is new. That term is multiplied by `l`, which is null where
  measured. Sending session content to a third party is also a publication
  that each project would have to allow.
- **Acting on the recommendation now.** The quality cost of compaction on
  real sessions is unmeasured. Literature reports constraints stated only in
  conversation being lost about six times more often than specifications.
  Acting on cost alone would trade a measured saving for an unmeasured harm.
- **Recording on the server.** Every member of the workspace could read it,
  and ADR-0049 keeps working memory with the caller.
- **Reading the whole transcript at each prompt.** Transcripts reach 6 MB.
  The offset makes a prompt cost what was written since the previous one.

## What this does not cover

- Two sessions resuming the same work after a crash. That is identity and
  possession: ADR-0080 (proposed, task d5f258b2) and the leases of ADR-0063.
- The remaining-calls constant is a median of 60 sessions on one machine. A
  session that ends early makes every `compact` recommendation an
  overestimate. The summary does not correct for it.
- The prices are list prices. Under a subscription the cost is a share of a
  rate limit, not dollars. The ordering of the options is the same, but the
  margins are not money paid.
- Sessions running on other agent hosts, and subagents' own contexts (their
  records are sidechains, and are skipped).
