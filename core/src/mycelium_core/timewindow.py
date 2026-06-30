"""Pure helpers for the date-only due-date convention and reminder
anchoring, shared by every entry point so they agree.

A due date can be *timed* (carries a real time-of-day) or *date-only*
("due that day, no specific time"). Date-only is the source of a whole
class of off-by-one-day notification bugs because each adapter used to
pick its own time-of-day in its own timezone (the SPA baked 23:59:59 in
the browser zone, the MCP baked 23:59:59 in UTC, the HTTP API let a bare
``YYYY-MM-DD`` coerce to 00:00). The fix: adapters only decide
*date-only vs timed* (``split_due``); the core service promotes a
date-only value to end-of-day in the OWNER's configured timezone
(``end_of_day``); the reminder scanner anchors the firing instant to the
START of that day (``day_start_anchor``), decoupled from the end-of-day
expiry sentinel so a "due today" reminder lands in the morning, not at
23:59 (which reads as a day late).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Minutes after local midnight that a date-only task's reminders anchor
# to when the user has not configured ``users.day_start_minute``. 0 =
# local midnight (the start of the day), matching "no time = start of
# day" rather than the old end-of-day behaviour.
DEFAULT_DAY_START_MINUTE = 0


def resolve_tz(name: str | None) -> dt.tzinfo:
    """An IANA timezone name -> tzinfo, falling back to UTC for an unset
    or unrecognised value (a stored ``users.timezone`` should be valid,
    but never let a bad string break a reminder sweep or a task write)."""
    if not name:
        return dt.UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.UTC


def split_due(raw: str) -> dt.date | dt.datetime:
    """Parse a due-date string into a ``date`` (date-only intent) or an
    aware ``datetime`` (an explicit instant), WITHOUT applying any
    timezone. A bare ``YYYY-MM-DD`` is date-only; anything carrying a
    time component (``T``/space separator or ``HH:MM``) is a real
    datetime. The core service later promotes the date-only case to
    end-of-day in the owner's configured timezone."""
    s = raw.strip()
    if "t" in s.lower() or " " in s or ":" in s:
        return dt.datetime.fromisoformat(s)
    return dt.date.fromisoformat(s)


def end_of_day(d: dt.date, tz: dt.tzinfo) -> dt.datetime:
    """The 23:59:59 "no time specified" sentinel for calendar day ``d``
    in ``tz``, as an aware datetime. Stored on ``tasks.due_date`` so a
    date-only deadline EXPIRES at the end of the user's calendar day (a
    "due today" task is not overdue until the day actually ends) and is
    recognised as date-only by the reminder scanner, which checks for
    23:59:59 in the recipient's own timezone."""
    return dt.datetime.combine(d, dt.time(23, 59, 59), tzinfo=tz)


def day_start_anchor(d: dt.date, tz: dt.tzinfo, day_start_minute: int) -> dt.datetime:
    """The instant a date-only task's reminders anchor to:
    ``day_start_minute`` minutes after local midnight on day ``d`` in
    ``tz`` (an aware datetime). Decoupled from the end-of-day expiry
    sentinel (``end_of_day``) so the "at due" reminder for a date-only
    task fires at the START of the day (user-configurable, e.g. 06:00),
    not at 23:59 -- which the user perceives as a day late."""
    minute = max(0, min(int(day_start_minute), 1439))
    h, m = divmod(minute, 60)
    return dt.datetime.combine(d, dt.time(h, m), tzinfo=tz)
