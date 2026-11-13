# ADR-0066: A person decides a suggestion; an agent may carry the decision out

Status: Accepted (2026-10-22)
Relates to: ADR-0028 (identities: a person and an assistant author symmetrically,
which is why authorship alone cannot tell them apart here). Amends the property introduced on 2026-09-15 that
accepting and rejecting a suggestion are `HUMAN_ONLY` on both surfaces: that
property stands, and this adds one way past it that does not weaken it.

## Context

Accepting a suggestion splices its text into a note part or a task description.
It is the only whole-document decision primitive the platform has, and since
2026-09-15 no credential carrying a scope list can reach it, on MCP or REST.
That fence is what makes "agents propose, never impose" true rather than
advisory.

It left one case without a path. A person decides, in conversation with an
agent, to accept several suggestions, and wants the agent to carry the decision
out rather than open the UI. The first request was for a tool that records the
person's instruction next to the accept: who decided, who executed, and the
text of the instruction.

That does not work, and the reason decides the design. The instruction would be
written by the agent. The server cannot tell an instruction a person gave from
one an agent invented, so recording it would make every accept carry a
plausible sentence and change nothing about who can dispose. The fence would
become a formality with an audit trail.

## Decision

An agent may accept a suggestion only by citing a **comment** in which a person
accepted it, and the server verifies that comment before anything is spliced
(`annotations.accept_suggestion_on_decision`, MCP tool of the same name, scope
`comments:write`). The plain `accept_suggestion` stays `HUMAN_ONLY`.

The comment is a decision when all of these hold. Each one closes a way an agent
could otherwise approve itself.

1. **Its author identity is a person.** An agent's own comment is not a decision.
2. **Every write to it was made with a person's own credential.** Its creation
   and every later edit or restore appear in the activity log as `human_api`
   with no subject, which is what a person's bearer over REST publishes. An
   agent token over REST carries its token id as subject; every MCP call over
   HTTP is `mcp_token`; the Telegram assistant's writes are its tool loop's and
   are excluded. The author column is not enough on its own: an agent running on
   an owner's token passes the admin override on `edit`, so it can rewrite the
   owner's comment while the author stays the owner. The activity log is
   append-only and never pruned, so it is the record of who wrote what. A comment
   with no creation row in it is refused.
3. **The person is still a member** with the role the UI's accept requires.
4. **It is on the same document**, a note across all its parts or a task's
   description, so one comment can decide several suggestions on one note.
5. **It was written after the suggestion.** A later edit that adds the id does
   not move its creation.
6. **It names the suggestion on an `accept` line**, by full id or a prefix of at
   least eight hex characters, and the name matches no other suggestion on the
   document.

Then the accept runs through the same code as the person's: the same stale
check, the same requirement that the caller can write the document body. Two
columns record the roles separately (migration 0023). `resolved_by` is the
executor, `decided_by_identity_id` is the person, and `decision_comment_id` is
where they decided. The activity log entry for the accept carries all three.

A refusal is `annotation.suggestion_decision_invalid` with a `reason` naming the
clause that failed. Nothing is changed.

## Consequences

The cost to the person is one comment instead of one click per suggestion. That
is the point rather than a defect: the act that carries authority is still the
person's, written with the person's login, and the agent only removes the
mechanical part.

**Limits, stated rather than discovered.**

- A credential that is a person's own bearer can write a decision. An agent
  handed a person's session token over the stdio surface publishes `human_api`
  with no subject and is indistinguishable from the person. Until 2026-11-13
  the same was true of an agent token with no assistant bound, which is what
  `mycelium auth login` stores on disk; the amendment below closes that case
  and leaves only the session token handed over by hand.
- The `accept` line is matched on its words, not on its meaning. A person who
  writes `accept` followed by an id has accepted it. The grammar is strict, so
  "do not accept X" does not match, but it is not natural-language
  understanding and does not try to be.
- A protected note still refuses every write made through an assistant
  credential, this one included. Whether a verified decision should pass that
  guard is a separate question, and it is left as it is here.

## Rejected

- **Recording the instruction the agent reports.** Unverifiable, see Context.
- **Refusing only when the proposer and the executor are the same identity.**
  Every agent session shares one identity per assistant, so a second session
  could accept the first one's proposal. Even across assistants, two agents
  approving each other is still no person deciding.
- **A pending-approval object that the person confirms with a click.** It is
  the UI accept again, with a second state machine to keep in step with it.

## Amendment (2026-11-13, task f34a0292): a stored token is never a person

`HUMAN_ONLY` used to be decided on whether the credential carried a scope
list. A token with no assistant bound carries none, so it was treated like a
person's session and reached every route and tool reserved to one: minting
tokens and assistants, approving devices, changing roles and MFA, accepting a
suggestion. That token is not a leftover. `mycelium auth login` mints it and
stores it in a file every agent session on the machine can read, so "this
request comes from a person" cannot be read off it, for the same reason this
ADR already gives for not deciding on identity: every agent session shares it.

The decision is now taken on the kind of credential.

- A person's session (the login JWT, on REST or on the stdio transport)
  reaches everything, as before.
- Every agent credential, with or without an assistant, and a capability
  minted by one, is refused every `HUMAN_ONLY` route and tool and does not see
  the tools in the catalogue. Without an assistant it still holds every scope
  key, so ordinary work through the CLI is unchanged.
- One exception: a token may revoke itself, which only removes authority and is
  what `mycelium auth logout --revoke` does. Revoking another token stays a
  person's act.
- The CLI's person-only commands (`auth mfa *`, `workspace list`) ask for the
  password, run on the session it opens and close it afterwards. No flag or
  environment variable supplies the password, because a password stored once
  is readable by every agent session too.

ADR-0045 kept bare tokens at full access to limit the blast radius while the
scope gate covered only the invoice tools. The gate has covered every surface
since 2026-09-15, so that reason no longer holds.

Not covered here, and left as follow-ups: `session_writes_as_assistant` still
reads a bare token on REST as a person for protected notes and instructions,
and `mycelium auth login` and the SPA's MCP panel still mint bare tokens rather
than binding them to an assistant with a scope list.
