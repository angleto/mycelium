# ADR-0008 No-ubiquity: events entity

Status: accepted. Explicit user requirement.

## Context

The system arranges appointments on the user's behalf. The user does
not have the gift of ubiquity: they cannot have two engagements with
different clients at the same time. WorkingCalendar alone does not
model fixed-time appointments.

## Decision

New `events` entity (appointment): org-scoped, client/project tag,
participants, time-pinned interval, location. Constraint: no
overlapping intervals for the same participant; creating or moving an
appointment that overlaps for the same person is **rejected**.
Appointments are exclusive fixed reservations on a person's timeline
and the scheduler (ADR-0004) places non-delegated human tasks around
them.

## Consequences

- The scheduler treats events as hard constraints; the same person's
  human tasks do not overlap each other nor the appointments.
- A notification is raised on a double-booking attempt.

## Alternatives rejected

- Modeling appointments as tasks: a task is flexible and schedulable,
  an appointment is fixed-time and exclusive; different semantics.
- A non-blocking overlap warning only: the requirement is that the
  system not set concurrent engagements, hence rejection.
