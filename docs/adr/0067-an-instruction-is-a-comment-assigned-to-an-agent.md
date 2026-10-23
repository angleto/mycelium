# ADR-0067: An instruction is a comment assigned to an agent

Status: Accepted (2026-10-23)
Relates to: ADR-0066, which keeps accepting a suggestion a person's act; this
keeps closing an instruction a person's act, for the same reason.

## Context

A person revising a text wants to leave instructions on passages for an LLM
to carry out later: "rewrite this paragraph more concisely", "refer to
document xyz", "this element can go". An agent then revises the document over
MCP. The person checks each edit against the instruction that asked for it,
either by reading the diff or by asking an agent to verify the edits
adversarially.

The editor had two annotation kinds, `comment` and `suggestion`. Three
designs were on the table: plain comments, a new `instruction` kind, or a
comment with a comment/instruction toggle.

Two facts decided it. The backend could already assign an annotation to any
workspace identity, agents included (`annotations.assign`, the MCP tools
`assign_annotation` and `list_assigned_annotations`), but no screen exposed
it. And no suggestion could say which comment it answered.

## Decision

**An instruction is a comment whose author is a person and whose assignee is
an assistant.** It is not stored as a kind. It is read off the two identities
(`annotations._is_instruction`). Assigning a comment already says that
someone has to act on it and who that is. A kind would carry only the first
half, and it would duplicate the assignee and the lifecycle a comment already
has.

**A suggestion names the instruction it carries out.** `comments.instruction_id`
(migration 0024) is set by `propose_suggestion`. The server refuses a link
unless it points to a comment that exists, is not deleted, is still open, and
sits on the same document. A note counts as one document across all its
parts. The link does not require the instruction to be assigned to the
caller: it records which instruction an edit answers, and anyone may answer
one. This pairing is what review needs. With it, a verifier gets
(instruction, original, proposed) triples rather than a diff plus a list of
instructions, so an instruction nobody answered and an edit nobody asked for
are both visible.

**The person closes the instruction.** A session that writes as an assistant
(`agent_tokens.session_writes_as_assistant`, the same predicate protected
notes use, which answers on REST as well as MCP) is refused, with
`annotation.instruction_reserved`, when it tries to:

- resolve the instruction;
- reassign or unassign it;
- edit its body (including patch, append, prepend and replace);
- delete it, or restore it or one of its revisions;
- purge it.

If the agent could resolve the instruction, it would be closing its own work.
If it could change or remove the instruction, it could move the yardstick it
is checked against. Reopening stays open to everyone, because it gives the
person their instruction back.

The agent's side of the workflow is written in the MCP docstrings of
`list_assigned_annotations`, `propose_suggestion` and `add_annotation`:

1. read the document and the instruction;
2. answer with suggestions that cite the instruction, never by rewriting the
   document directly;
3. reply on the instruction with what it did, or why it could not;
4. leave the instruction open.

The UI gives the comment composer (inline popover and panel) an optional
assignee that includes agents, and a comment an Assign action. An instruction
shows the suggestions that carry it out, with their status. A suggestion
shows the instruction it carries out.

## Consequences

- Instructions work for people too. A comment assigned to a colleague is a
  request to that colleague; the fence applies only when the assignee is an
  assistant, because that is the case where the executor could close its own
  work.
- An assistant reassigning a person's comment to itself makes it an
  instruction, and from then on cannot close it. That is harmless: the
  person decides when it is done.
- A person working through an assistant is inside the fence on purpose. They
  close the instruction from the app.
- Some instructions cannot be carried out as suggestions. A suggestion that
  changes the block structure (splitting a paragraph, adding a table cell,
  restructuring a list) is refused on accept, and its original text must
  appear exactly once. The agent replies that it could not and why, rather
  than approximating.
- Once an answering suggestion is accepted, the instruction's anchor text is
  usually gone, and the panel shows it as not found. That is the moment for
  the person to resolve it.

## Rejected

- **A new `instruction` kind.** It duplicates the assignee and the comment
  lifecycle, says nothing about who executes, and would add a DB constraint
  value, a client branch on every surface, and a second state machine.
- **A comment/instruction toggle in the composer.** This is the right idea at
  the wrong level. Choosing an assignee in the composer is the toggle, and it
  also says which agent.
- **Plain comments with no link.** The agent would have to guess which
  comments are instructions, and the reviewer could not pair an edit with its
  request.
- **Fencing the admin override for every person's comment.** Through an
  owner's token, an assistant can edit any comment a person wrote, not only an
  instruction (ADR-0066 relies on the activity log for the same reason). The
  broader fence is a separate decision and is not taken here.
