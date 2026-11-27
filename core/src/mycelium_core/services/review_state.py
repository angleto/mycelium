"""What a review verb answers when the proposal is not where it acts.

Every human-gated proposal (a generated note, an extracted fact) moves
through a small set of review states, and every verb on it (approve,
decline, reopen, revise, withdraw, remove) acts on some of them. This module
is the one place that decides how a verb refuses, so the verbs cannot drift
apart again. They had: each spelled its own checks, all of them ran the
version check first and answered a wrong state with "not found", so a note
approved while an agent was revising it came back as a stale write, and on
the retry as a missing note, neither of which was true (field report,
2026-11-25).

The order the guards run in is the contract:

1. absent (the caller's own loader): the thing does not exist on this
   surface, 404;
2. wrong state, :func:`require_state`: it exists, it is visible, and the verb
   does not act on it in that state. 409 ``review.state_conflict``, with the
   state, the states the verb needs and the current version. It runs before
   the version on purpose: re-reading cannot make the verb apply, so sending
   the caller to re-read is sending it round a loop;
3. stale version, :func:`require_version`: the same answer every versioned
   write gives, with the version to re-read at;
4. only then a verb's idempotent short-circuit (approving an approved note is
   a no-op). That ordering is the one the approve and reject verbs documented
   before this module existed, and it is unchanged: a stale view is an error
   even when the end state would coincide.

What it does not cover: link proposals keep their own
``link_proposal.decided``, which predates this and is a published code; a
client branching on review conflicts has two codes to know until the two
are merged, which is a wire change and a separate decision.
"""

from __future__ import annotations

from collections.abc import Collection

from mycelium_core.concurrency import raise_stale_version
from mycelium_core.errors import ConflictError
from mycelium_core.i18n import MessageCode


def require_state(state: str | None, *, acts_on: Collection[str], current_version: int) -> None:
    """Refuse a verb on a proposal whose state it does not act on.

    ``acts_on`` includes the states where the verb is an idempotent no-op:
    declining a declined note is not a conflict, it is a retry."""
    if state not in acts_on:
        raise ConflictError(
            MessageCode.REVIEW_STATE_CONFLICT,
            state=state,
            allowed=sorted(acts_on),
            current_version=int(current_version),
        )


def require_version(current: int, expected: int | None) -> None:
    """The optimistic check, answered the way every versioned write answers
    it. ``expected=None`` is the legacy unguarded call and passes."""
    if expected is not None and current != expected:
        raise_stale_version(current)
