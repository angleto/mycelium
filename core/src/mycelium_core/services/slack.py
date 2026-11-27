"""What "slack" means, for every surface that reports one.

Slack is how far a task's finish can move before it breaks the latest
finish it is held to: ``latest_finish - earliest_finish``, in whole
minutes of wall-clock time, signed. Negative slack is a miss by that much.

Before this module there were two definitions that disagreed (measured
2026-11-26): the scheduler measured ``LS - ES`` against the end of the
whole workspace's plan and clamped it at 0, while the advisory layer
measured the distance to the task's own deadline. They now share this
one, and they differ only in the finish they feed it:

- the scheduler passes the latest finish from its backward pass, which
  is bounded by the task's due date, by its successors, and by the end
  of the plan, and its earliest finish from the forward pass over the
  working calendar;
- the advisory layer passes the due date and the finish of working on
  the task without a break from the start of the free window the person
  declared. That finish ignores the working calendar on purpose: the
  person said they are working now.

Wall-clock minutes rather than working minutes because the figure is
read by a person as "late by two days", and a calendar-free figure is
what both callers can state without guessing a calendar for the other.
What it does not cover: a finish that is itself a lower bound (a task
with no estimate) makes the slack an upper bound, and the caller says so.
"""

from __future__ import annotations

import datetime as dt


def slack_minutes(latest_finish: dt.datetime, earliest_finish: dt.datetime) -> int:
    """Signed whole minutes from ``earliest_finish`` to ``latest_finish``,
    rounded toward the earlier instant."""
    return int((latest_finish - earliest_finish).total_seconds() // 60)
