"""API I/O schemas (pydantic v2). No business logic."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from flow_core.models.dependency import DependencyType
from flow_core.models.tag import TagKind
from flow_core.models.task import ConstraintKind, ExecKind, ScheduleMode


class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    org_name: str = Field(min_length=1, max_length=200)


class SignupOut(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID
    token: str


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    token: str


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int


class OrgPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class VersionOut(BaseModel):
    id: uuid.UUID
    version: int


# Backward-compatible alias used by the org router.
OrgVersionOut = VersionOut


class TagCreateIn(BaseModel):
    kind: TagKind
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)


class TagPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, max_length=16)


class TagOut(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None
    status: str
    version: int


class ClientCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ragione_sociale: str = Field(min_length=1, max_length=200)
    id_paese: str | None = Field(default=None, max_length=2)
    id_codice: str | None = Field(default=None, max_length=30)
    codice_fiscale: str | None = Field(default=None, max_length=30)
    indirizzo: str | None = Field(default=None, max_length=200)
    cap: str | None = Field(default=None, max_length=10)
    comune: str | None = Field(default=None, max_length=120)
    provincia: str | None = Field(default=None, max_length=4)
    nazione: str | None = Field(default=None, max_length=2)
    codice_destinatario: str | None = Field(default=None, max_length=7)
    pec: str | None = Field(default=None, max_length=320)


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    client_tag_id: uuid.UUID | None = None
    tariffa: Decimal | None = None
    valuta: str = Field(default="EUR", max_length=3)
    budget: Decimal | None = None


class TaskCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: int = Field(default=3, ge=1, le=4)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    parent_task_id: uuid.UUID | None = None
    executor_kind: ExecKind = ExecKind.human
    executor_user_id: uuid.UUID | None = None
    estimate_effort_h: Decimal | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    assignee_ids: list[uuid.UUID] = Field(default_factory=list)


class TaskPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    estimate_effort_h: Decimal | None = None
    executor_kind: ExecKind | None = None
    executor_user_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None


class TaskStateIn(BaseModel):
    expected_version: int = Field(ge=1)
    state_id: uuid.UUID


class StateOut(BaseModel):
    id: uuid.UUID
    name: str
    ord: int
    is_initial: bool
    is_terminal: bool


class ExpectedVersionIn(BaseModel):
    expected_version: int = Field(ge=1)


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    state_id: uuid.UUID
    state: str
    priority: int
    start_date: datetime.date | None
    due_date: datetime.date | None
    parent_task_id: uuid.UUID | None
    executor_kind: ExecKind
    is_archived: bool
    version: int


class CommentCreateIn(BaseModel):
    body: str = Field(min_length=1)


class CommentOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID | None
    body: str
    version: int


class TagRefIn(BaseModel):
    tag_id: uuid.UUID


class AssigneeIn(BaseModel):
    user_id: uuid.UUID


class WorkflowStateSpecIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ord: int = 0
    is_initial: bool = False
    is_terminal: bool = False


class TransitionIn(BaseModel):
    from_state: str
    to_state: str


class WorkflowCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    states: list[WorkflowStateSpecIn]
    transitions: list[TransitionIn] = Field(default_factory=list)


class WorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    is_default: bool
    version: int


class ProjectWorkflowIn(BaseModel):
    expected_version: int = Field(ge=1)
    workflow_id: uuid.UUID | None = None


class DependencyCreateIn(BaseModel):
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    type: DependencyType
    lag_working_minutes: int = 0


class DependencyOut(BaseModel):
    id: uuid.UUID
    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    type: DependencyType
    lag_working_minutes: int
    version: int


class GraphNode(BaseModel):
    id: str
    title: str
    state: str
    blocked: bool


class GraphEdge(BaseModel):
    predecessor: str
    successor: str
    type: str
    lag_working_minutes: int


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --- F3: calendars, events, schedule (FR-4, docs/adr/0004, 0008) ---


class CalendarCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="Europe/Rome", max_length=64)
    # weekday -> ordered [start, end] local "HH:MM" windows
    weekly_hours: dict[str, list[list[str]]]


class CalendarOut(BaseModel):
    id: uuid.UUID
    name: str
    is_default: bool
    timezone: str
    version: int


class HolidayIn(BaseModel):
    day: datetime.date


class HolidayOut(BaseModel):
    day: datetime.date


class UserCalendarIn(BaseModel):
    user_id: uuid.UUID
    calendar_id: uuid.UUID
    daily_capacity_h: Decimal = Field(gt=0, le=24)


class EventCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start_at: datetime.datetime
    end_at: datetime.datetime
    participant_ids: list[uuid.UUID] = Field(default_factory=list)
    project_tag_id: uuid.UUID | None = None
    client_tag_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=200)


class EventRescheduleIn(BaseModel):
    expected_version: int = Field(ge=1)
    start_at: datetime.datetime
    end_at: datetime.datetime


class EventOut(BaseModel):
    id: uuid.UUID
    title: str
    start_at: datetime.datetime
    end_at: datetime.datetime
    location: str | None
    project_tag_id: uuid.UUID | None
    client_tag_id: uuid.UUID | None
    version: int


class TaskScheduleIn(BaseModel):
    """Drag/write-back of scheduler fields (FR-4). Only provided fields
    are changed; recompute then respects manual/constraint pins."""

    expected_version: int = Field(ge=1)
    schedule_mode: ScheduleMode | None = None
    constraint_kind: ConstraintKind | None = None
    constraint_date: datetime.datetime | None = None
    remaining_effort_h: Decimal | None = None
    actual_start: datetime.datetime | None = None
    is_milestone: bool | None = None


class RecomputeIn(BaseModel):
    project_tag_id: uuid.UUID | None = None
    as_of: datetime.datetime | None = None


class RecomputeOut(BaseModel):
    count: int


class ScheduleOut(BaseModel):
    task_id: uuid.UUID
    es: datetime.datetime | None
    ef: datetime.datetime | None
    ls: datetime.datetime | None
    lf: datetime.datetime | None
    slack_minutes: int | None
    on_logical_critical_path: bool
    scheduled_start: datetime.datetime | None
    scheduled_end: datetime.datetime | None
    computed_at: datetime.datetime
    input_fingerprint: str | None
