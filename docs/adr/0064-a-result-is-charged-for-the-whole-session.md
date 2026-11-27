# ADR-0064: An MCP result is charged for the whole session, so the default shape is the cheap one

Status: Accepted (2026-09-17)
Relates to: ADR-0038 (the 8-hex short id, whose input half this completes),
ADR-0049 (working memory stays with the caller — this is what makes the
caller's context the scarce resource), ADR-0028 (identities and handles, which
is why a handle can replace the uuid beside it).

## Context

The dynamic-toolset gateway was built on the observation that an MCP client
pays for `tools/list` on every request, and it cut that payload from ~21k
tokens to ~1k. That framing was right about the mechanism and wrong about where
the money was.

Measured on 2026-09-17 over 32 recorded client sessions on this repository
(18,819 assistant turns, 721 gateway calls, tokenized with cl100k):

| | |
|---|---|
| result tokens | 553,547 (17,298 per session) |
| result token-turns (tokens x turns the result then sits in context) | 494.5M |
| static `tools/list` + instructions | 1,356 tokens per request |
| static share of this server's context weight | **4.9%** |

A tool result is not read once. It enters the caller's context and is re-read
on every subsequent turn of the session, so its true cost is its size times its
residency. That is why `whoami` — twelve calls in the whole corpus — carried
10% of the weight: it is called at turn 1 and sits there for a thousand turns.
Four tools carried 77%: `get_task` (30.6%), `search` (23.6%), `list_tasks`
(12.8%), `whoami` (10.1%).

What those payloads were made of is the rest of the context. Long text fields
were 29.1% of all result tokens; raw uuids 21.3%, of which **90% were never
passed back to anything**; JSON indentation 12.3%; fields identical on every
row of a page 15.7% of all row tokens.

The surface already had the right mechanisms and nobody was reaching them.
`memory_search` had capped recall text with a `text_truncated` marker since it
was written. Capability tokens (`get_text_block_capability` and its siblings)
return a `curl` that writes content to a FILE, costing no tokens at all: used
three times in 32 sessions, while `get_task` shipped 147,990 tokens of
description inline. `fields=` projection existed on `list_tasks` and was used
in none of the 721 calls.

That is the pattern this ADR is about. An efficiency that has to be asked for
is an efficiency nobody gets.

## Decision

**The default shape of a result is the cheap one, and the expensive one is
asked for by name.** Five changes, each with the measurement that decided it in
the code beside it.

**1. One compact copy on the wire.** `execute_tool` returns a `TextContent`
holding compact JSON. FastMCP renders a non-`str` return with `indent=2`, and
`execute_tool` was the only meta-tool reaching that path (`search_tools` and
`describe_tools` annotate a `list[dict]` return and therefore travel as compact
`structuredContent`). A `TextContent` also keeps the payload at ONE copy: a
structured return sends it twice and leaves the client to decide which copy it
charges for.

**2. The order is the ranking.** `score` and `scores_by_stage` on `search` and
`memory_search`, and `rrf` in the `whoami` recall, are emitted only under
`explain=True`. They are for auditing recall, not for reading results.

**3. A field nobody can read or reuse is not emitted.** `model_id` is hoisted
to the response `meta` and returns to the rows only when a page genuinely mixes
models. `blob_id` survives only on `kind='blob'` hits, where it is the row's
only handle. `workflow_id` leaves the lean task row: it answers a question
about the page. `assignee_id` / `owner_id` leave it too, since that shape
resolves no handles and emitted a pair of unreadable uuids; on `get_task` the
id survives only where the handle did not resolve. `ts_headline`'s `<b>` markup
is stripped in the MCP adapter, and kept for the SPA that renders it.

**4. A long description is capped and says so.** `get_task` returns the first
1,200 characters with `description_truncated` and the full `description_chars`,
the contract `_blob` has always had. Two ways to the whole text, named in the
tool's own description: `get_text_block_capability`, which writes it to a file
and costs nothing, and `full_description=True`, which inlines it. The
work-handing tools (`task_pull` / `task_claim` / `task_offer` /
`task_decline`) are NOT capped: the description is the brief they exist to
deliver.

**5. Task and note ids travel short, and the gateway owns both halves.** The
ADR-0038 8-hex prefix is what a task or note id looks like on this surface, and
`execute_tool` expands one back to a uuid before dispatch. A uuid costs ~23
tokens and a prefix ~4.

The last one has a rule about WHERE that is the whole of its safety. Both
halves live in `gateway.py`: shortening and expansion are one convention, and
a surface that emits an identifier it cannot consume is broken. The walk is
narrow — a key that names its kind (`task_id`, `note_id`, ...), the bare `id`
at the top level or in the rows of a top-level `items` / `hits` / `open_tasks`
list for the tools in `_ENTITY_ROW_TOOLS`, plus the multiplexer arguments whose
kind is decided by a sibling (`resource_id` under `kind`, `parent_id` under
`parent_kind`). Nothing deeper is touched, which keeps `get_note`'s `parts[].id` and
`memory_search`'s `hits[].blob.id` full: nothing can expand a prefix of a part
or a blob.

An ambiguous prefix is REFUSED, with the candidates' full ids, never resolved
to the freshest match. `resolve_prefix` orders its results, so picking the
first would always succeed and would occasionally write to the wrong entity,
which the caller has no way to notice.

## Consequences

Replaying the same 721 recorded results through the shipped transforms:
**553,547 tokens become 303,259, a 45.3% reduction**, or 17,298 tokens per
session down to 9,476. Per tool: `get_task` -67.9%, `search` -60.6%,
`list_notes` -43.7%, `whoami` -43.1%, `list_tasks` -43.0%. The replay models
the lean row carrying a resolved handle where it used to carry the assignee
uuid, which is why it saves a little less than the uuid was costing.

A byte budget now gates it. `test_mcp_response_budget.py` asserts a lean row
and a `get_task` against recorded ceilings that only move down, because every
individual field here was defensible and the failure mode is accumulation, not
any one decision.

The registry surface (stdio, and most of the test suite) keeps full uuids. It
has no expansion, so it must not hand out a form it cannot take back. The two
surfaces therefore disagree about how an id is spelled, and that is the
intended reading of ARC-01: shape is the domain's, spelling is the adapter's.

The bytes/token constant used by the `mcp_io` fee was 4, in three files.
Measured here it is 2.87, so the fee was under-counting by 28%; it is now one
`BYTES_PER_TOKEN` in `billing` and the metered fee moves up accordingly.

**Which arguments take a task or note id had to be ENUMERATED, not guessed,
and the guessing failed twice.** A short id is usable only where the
dispatcher expands it, and reading argument names is not enough:
`resource_id` is the capability tools' id, and `get_task` points a caller
holding a short id straight at it; `seed` is the note a graph walk starts
from and reads like a parameter. Both surfaced as `ValueError: badly formed
hexadecimal UUID string` from inside unrelated tests. The tables are now
derived from every `@mcp.tool()` argument the body passes to `uuid.UUID(...)`,
and `test_every_id_argument_is_classified` fails on a new one until somebody
classifies it as expandable or explicitly not an entity. The reverse guard
rejected two entries on its first run.

**A PROGRAMMATIC gateway client that parses an id as a uuid breaks, and that
is the sharp edge of decision 5.** An LLM caller is unaffected: it holds the id
as a string and hands it straight back, and the surface expands it. A client
that converts -- `uuid.UUID(note["id"])` -- gets a `ValueError`. The evaluation
harness in `mcp/eval_scenarios.py` is exactly that client, because it takes ids
from the gateway to the DATABASE, which keys on the whole uuid; it now expands
through `lookup.resolve_prefix` at the two places that cross that boundary
(`_entity_uuid`). Any other automation reading this surface needs the same
treatment, and there is no deprecation window: the short form arrived in one
release.

An 8-hex prefix is not unique. Two entities can share one (~1% somewhere in an
org of 10k entities) and nothing checks at emission; the refusal on the way
back in is the guarantee. This is the exposure ADR-0038 already created by
putting short ids into note bodies, widened to every read.

The saving on the description cap is realised only when the offloaded text is
worked as a file. A caller that fetches it back into context saves nothing.
11% of `get_task` calls in the corpus refetched a task already fetched in the
same session, which a cap makes cheap; the modal next action after a `get_task`
was `Bash`, which is consistent with the design without proving it.

## Alternatives rejected

**Prune the tool schemas further.** The reflex, and measurement kills it: the
whole static payload is 4.9% of this server's context weight, against the
12.3% that indentation alone was costing on the execute path.

**Leave the capability tokens as an opt-in and document them better.** They had
been documented and were used three times in 32 sessions. Defaults move
measurements; advice does not.

**Let the row-list key be `items` and `hits` alone.** `whoami` puts its task
rows under `open_tasks`, so the first version of the walk skipped the single
payload with the longest residency in the whole corpus. Found by reading a real
payload after the unit tests were green, which is the argument for reading one.

**Shorten ids in the serializers, where the entity kind is known.** Tried
first, and it is where the kind IS known, which is what made it attractive.
Those serializers are shared with the stdio registry, which has no expansion,
so that surface handed out an id its own `get_task` then refused to parse. The
test suite caught it. The two halves of a convention live together.

**Resolve an ambiguous prefix to the best match.** It would always succeed,
which is exactly what makes it wrong.

**Guarantee prefix uniqueness at emission, as git does.** It costs a query per
response to buy what a refusal on the way back in already provides, and git's
short hashes go into commit messages and scripts, where nothing can refuse them
later.

**Hoist per-response constants into a `common` envelope key.** It is the
largest remaining item — the two tag objects are ~250 bytes of a 472-byte row,
identical on every row — but the paginated envelope is one shared contract
across every listing, pinned by a test on purpose. It is a decision about that
contract, not a drive-by, and it is recorded as open.

**Cap `get_note`'s part bodies by default.** A caller that asks to read a note
is asking for the note; the outline escape hatch (`include_part_bodies=False`)
already exists and is documented.

## Amendment (2026-11-27, task dae6858d): note text is read by windows

The last rejection above did not survive contact with a single-part note.
On 2026-11-25 three notes of 95 to 137 kB could not be read from Claude Code
at all: each was one part, so the outline led to a `get_note_part` that
returned the same 120 kB, and the client refused the result. The outline is
an escape from many parts, not from one long one, and a part may be up to
`note_body_max_bytes` (1 MiB). `garden_review_read` had the same hole one
level down: it budgeted its sources and returned the proposal and the
original it revises whole.

**Decision.** Every note text an MCP answer carries is spent from one budget
of 24,000 UTF-8 bytes (`mycelium_core/text_window.py`), in the order the
answer lists the texts. A text the budget does not hold whole is cut at the
end of a line (on a character boundary when a single line is longer than
the budget) and says so: `body_truncated`, the whole `body_bytes`, and a
`next_cursor`. `get_note_part` with that cursor returns the next window, and
the windows joined are the body exactly. A cursor carries a prefix of the
body's SHA-256; asked of a body that changed since, it is
`concurrency.stale_version` with the current version, so two revisions are
never spliced. `garden_review_read` shares the same budget among the
proposal, the original and the sources, and `garden_review_read_source`
continues any of the three from a cursor; its `full_text` is no longer
offered on MCP.

This is not the cap rejected above, and the reason given there is why.
That cap would have answered a request for the note with part of it and no
way on. A window answers with the note, a piece at a time, and nothing is
out of reach. The operations still return whole bodies; the window is the
MCP surface's (`mycelium_mcp/read_budget.py`), and the web reads whole.

**Limits.** The budget counts the text's bytes, not its JSON: a newline or
a quote is two bytes on the wire and a control character six, which the
margin to the client's limit absorbs for markdown and not for text made of
nothing else. The checks attached to a proposal are outside the budget: each
is capped where it is written (a 2,000-character summary, 50 findings), but
their number is not. Task text is not covered yet: `work_digest`, `get_task` with
`full_description` and the brief that `task_pull` delivers are still whole
(task 0fa73a5c).

**And a read capability is single-use** (same task, 2026-11-27). Point 4
above offered `get_text_block_capability` as the way to a whole text. Its
token rides in the conversation that runs the `curl`: an MCP server cannot
hand a credential to the client's shell any other way, and moving it to an
environment variable or a file only moves where it is written. Multi-use
for its TTL, it was a working key to that text for anyone who could read
the transcript. A text read capability (note part, task description,
comment) is now consumed by the download that serves it; one GET with
`-D -` already carried the body and the base-gate headers, so nothing
legitimate needed a second. With the windows above, reading a note needs
no capability at all; the capability remains for putting a body in a file
to edit and patch.

What it does not close: the window between minting a token and its first
use, for reads and for the single-use write and patch tokens alike, and
`attachment:read`, which stays multi-use for its TTL because one token
downloads every file of a parent.
