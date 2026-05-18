"""API I/O schemas (pydantic v2). No business logic."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from flow_core.models.billing import CostBasis, RateUnit, StorageKind
from flow_core.models.budget import BudgetPeriod
from flow_core.models.dependency import DependencyType
from flow_core.models.email import EmailAccountStatus, EmailProvider
from flow_core.models.invoice import (
    ConservationStatus,
    DocumentType,
    InvoiceKind,
    InvoiceState,
    PaymentStatus,
    SdiStatus,
)
from flow_core.models.note import NoteKind, NoteStatus, TurnRole
from flow_core.models.notification import (
    NotificationChannelKind,
    NotificationStatus,
    RecurrenceFreq,
)
from flow_core.models.tag import TagKind
from flow_core.models.task import ConstraintKind, ExecKind, Necessity, ScheduleMode
from flow_core.models.time_entry import TimeSource


class SignupIn(BaseModel):
    # Personal-first: a personal workspace is auto-provisioned. Naming it
    # is optional (defaults to a personal workspace); the user never has
    # to "create an organization" to sign up.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    workspace_name: str | None = Field(default=None, max_length=200)


class SignupOut(BaseModel):
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    # None when email verification is required: the SPA shows a
    # "check your email" state instead of logging the user in.
    token: str | None = None
    email_verification_required: bool = False


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    token: str


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class EmailIn(BaseModel):
    """Used by resend-verification and forgot-password (enumeration-safe;
    the response never reveals whether the address exists)."""

    email: str = Field(min_length=3, max_length=320)


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=8, max_length=200)


class LoginMfaIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    totp_code: str = Field(min_length=6, max_length=12)


class MfaSetupOut(BaseModel):
    provisioning_uri: str
    qr_png_base64: str
    secret: str = Field(description="Base32 TOTP secret; shown once at setup.")


class MfaActivateIn(BaseModel):
    totp_code: str = Field(min_length=6, max_length=12)


class MfaActivateOut(BaseModel):
    backup_codes: list[str] = Field(description="Shown once; store securely.")
    enabled_at: datetime.datetime


class MfaDisableIn(BaseModel):
    code: str = Field(min_length=6, max_length=12, description="TOTP or a backup code.")


class MfaStatusOut(BaseModel):
    enabled: bool
    pending: bool
    enabled_at: datetime.datetime | None
    backup_codes_remaining: int


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int


class WorkspacePatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceSummaryOut(BaseModel):
    """A workspace the authenticated user belongs to (pre-tenant
    selection, for the in-app switcher)."""

    id: uuid.UUID
    name: str
    role: str


class VersionOut(BaseModel):
    id: uuid.UUID
    version: int


# Alias used by the workspace router PATCH response.
WorkspaceVersionOut = VersionOut


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


class TagBrief(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None


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
    importance: int | None = Field(default=None, ge=1, le=5)
    urgency: int | None = Field(default=None, ge=1, le=5)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    parent_task_id: uuid.UUID | None = None
    executor_kind: ExecKind = ExecKind.human
    executor_user_id: uuid.UUID | None = None
    estimate_effort_h: Decimal | None = None
    monetary_cost: Decimal | None = None
    location: str | None = Field(default=None, max_length=200)
    necessity: Necessity = Necessity.should
    budget_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    assignee_ids: list[uuid.UUID] = Field(default_factory=list)


class TaskPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    importance: int | None = Field(default=None, ge=1, le=5)
    urgency: int | None = Field(default=None, ge=1, le=5)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    estimate_effort_h: Decimal | None = None
    executor_kind: ExecKind | None = None
    executor_user_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    monetary_cost: Decimal | None = None
    location: str | None = Field(default=None, max_length=200)
    necessity: Necessity | None = None
    budget_id: uuid.UUID | None = None


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
    importance: int | None
    urgency: int | None
    start_date: datetime.date | None
    due_date: datetime.date | None
    parent_task_id: uuid.UUID | None
    executor_kind: ExecKind
    estimate_effort_h: Decimal | None
    monetary_cost: Decimal | None
    location: str | None
    necessity: Necessity
    budget_id: uuid.UUID | None
    is_archived: bool
    version: int
    tags: list[TagBrief] = Field(default_factory=list)


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


# --- F4: time tracking (FR-5, docs/adr/0002) ---


class TimeStartIn(BaseModel):
    task_id: uuid.UUID
    billable: bool = True
    note: str | None = None


class TimeStopIn(BaseModel):
    note: str | None = None


class TimeManualIn(BaseModel):
    task_id: uuid.UUID
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = None
    duration_seconds: int | None = Field(default=None, gt=0)
    billable: bool = True
    note: str | None = None


class TimeEntryPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    note: str | None = None
    billable: bool | None = None


class TimeEntryOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    started_at: datetime.datetime
    ended_at: datetime.datetime | None
    duration_seconds: int | None
    source: TimeSource
    executor_kind: ExecKind
    billable: bool
    rate_snapshot: Decimal | None
    currency: str
    note: str | None
    version: int


class ReportRowOut(BaseModel):
    key: str | None
    label: str | None
    seconds: int
    billable_seconds: int
    amount: Decimal
    currency: str


# --- F4b: budgets + advisory (FR-13/FR-14, docs/adr/0013, 0014) ---


class BudgetCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    period_kind: BudgetPeriod
    period_start: datetime.date
    period_end: datetime.date
    amount: Decimal = Field(ge=0)
    currency: str = Field(default="EUR", max_length=3)


class BudgetPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, max_length=120)
    period_kind: BudgetPeriod | None = None
    period_start: datetime.date | None = None
    period_end: datetime.date | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)


class BudgetOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None
    period_kind: BudgetPeriod
    period_start: datetime.date
    period_end: datetime.date
    amount: Decimal
    currency: str
    version: int


class ConsumptionOut(BaseModel):
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    consumed: Decimal
    residual: Decimal
    task_count: int


class WhatNowIn(BaseModel):
    window_start: datetime.datetime
    duration_minutes: int = Field(gt=0)
    location: str | None = None
    context_tags: list[str] = Field(default_factory=list)


class ErrandsIn(BaseModel):
    location: str | None = None
    context: str | None = None


class FeasibleTaskOut(BaseModel):
    task_id: uuid.UUID
    title: str
    necessity: Necessity
    priority: int
    due_date: datetime.date | None
    remaining_minutes: int


class ErrandItemOut(BaseModel):
    task_id: uuid.UUID
    title: str
    location: str | None
    necessity: Necessity
    priority: int


class BudgetPickOut(BaseModel):
    task_id: uuid.UUID
    title: str
    cost: Decimal
    necessity: Necessity
    priority: int
    value: int


class BudgetPlanOut(BaseModel):
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    allocated: Decimal
    residual: Decimal
    selected: list[BudgetPickOut]
    excluded: list[dict[str, str]]


# --- F5: email (FR-7, docs/adr/0023) ---


class EmailAccountCreateIn(BaseModel):
    provider: EmailProvider
    email_address: str = Field(min_length=3, max_length=320)
    secret: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=200)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None


class EmailAccountPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=200)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None
    status: EmailAccountStatus | None = None


class EmailSecretIn(BaseModel):
    expected_version: int = Field(ge=1)
    secret: str = Field(min_length=1)


class EmailAccountOut(BaseModel):
    """No secret is ever returned (ADR-0023)."""

    id: uuid.UUID
    provider: EmailProvider
    email_address: str
    display_name: str | None
    imap_host: str | None
    imap_port: int | None
    smtp_host: str | None
    smtp_port: int | None
    status: EmailAccountStatus
    last_sync_at: datetime.datetime | None
    last_error: str | None
    version: int


class EmailMessageOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    provider_message_id: str
    thread_id: str | None
    message_id: str | None
    in_reply_to: str | None
    from_addr: str
    to_addrs: str
    subject: str | None
    body_text: str | None
    snippet: str | None
    received_at: datetime.datetime
    is_read: bool
    linked_task_id: uuid.UUID | None
    version: int


class SyncResultOut(BaseModel):
    account_id: uuid.UUID
    fetched: int
    created: int
    ok: bool
    error: str | None


class EmailToTaskIn(BaseModel):
    project_tag_id: uuid.UUID | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    assignee_ids: list[uuid.UUID] = Field(default_factory=list)


class EmailSendIn(BaseModel):
    to_addrs: list[str] = Field(min_length=1)
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: str | None = None


class EmailReplyIn(BaseModel):
    body_text: str = Field(min_length=1)


class TaskIdOut(BaseModel):
    task_id: uuid.UUID


class SentOut(BaseModel):
    sent_id: str


# --- F5b: billing / metering (FR-15, docs/adr/0019) ---


class BalanceOut(BaseModel):
    balance: Decimal


class GrantIn(BaseModel):
    amount: Decimal = Field(gt=0)
    reason: str | None = None


class MeterIn(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    op: str = Field(min_length=1, max_length=80)
    model_id: str | None = None
    units_in: Decimal = Decimal(0)
    units_out: Decimal = Decimal(0)
    basis: CostBasis = CostBasis.local


class UsageOut(BaseModel):
    id: uuid.UUID
    operation_id: str
    model_id: str | None
    op: str
    basis: CostBasis
    units_in: Decimal
    units_out: Decimal
    credits: Decimal


class LedgerOut(BaseModel):
    id: uuid.UUID
    kind: str
    amount: Decimal
    operation_id: str | None
    reason: str | None
    balance_after: Decimal


class RateCardUpsertIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=80)
    unit: RateUnit = RateUnit.token
    credits_per_input: Decimal = Decimal(0)
    credits_per_output: Decimal = Decimal(0)
    provider_cost_per_input: Decimal | None = None
    provider_cost_per_output: Decimal | None = None
    markup: Decimal = Decimal(1)
    is_active: bool = True
    tier: str | None = None


class RateCardOut(BaseModel):
    id: uuid.UUID
    model_id: str
    provider: str
    unit: RateUnit
    credits_per_input: Decimal
    credits_per_output: Decimal
    markup: Decimal
    is_active: bool
    tier: str | None
    version: int


class StorageRateIn(BaseModel):
    kind: StorageKind
    credits_per_gb_month: Decimal = Field(ge=0)


class ByokFactorIn(BaseModel):
    factor: Decimal = Field(ge=0)


# --- F6: hierarchical memory (FR-8, docs/adr/0005, 0007, 0016) ---


class MemoryWriteIn(BaseModel):
    project_id: uuid.UUID | None = None
    text: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(default="note", max_length=40)
    sources: list[tuple[str, str]] = Field(default_factory=list)
    importance: Decimal = Decimal(0)


class MemorySearchIn(BaseModel):
    project_id: uuid.UUID | None = None
    query: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, gt=0, le=100)
    grader_min_rrf: float | None = None


class MemoryBlobOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    namespace: str
    tier: str
    text: str | None
    summary: str | None
    model_id: str | None
    dim: int
    access_count: int
    cluster_id: uuid.UUID | None


class MemoryHitOut(BaseModel):
    blob: MemoryBlobOut
    rrf: float


class MemoryEraseIn(BaseModel):
    source_kind: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=255)


class MemoryConsolidateIn(BaseModel):
    project_id: uuid.UUID | None = None
    blob_ids: list[uuid.UUID] = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class TierCountsOut(BaseModel):
    hot: int
    warm: int
    cold: int


class ErasedOut(BaseModel):
    deleted: int


# --- F6b: notes / conversation / intent (FR-16, docs/adr/0020, 0021) ---


class NoteCreateIn(BaseModel):
    kind: NoteKind
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    audio_ref: str | None = Field(default=None, max_length=512)
    audio_seconds: int | None = None


class NoteOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    kind: NoteKind
    status: NoteStatus
    title: str | None
    transcript: str | None
    summary: str | None
    audio_ref: str | None
    version: int


class NoteTranscribeIn(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    embed: bool = True


class ConversationStartIn(BaseModel):
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)


class AppendMessageIn(BaseModel):
    content: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class NoteTurnOut(BaseModel):
    id: uuid.UUID
    role: TurnRole
    content: str
    ord: int


class SynthesizeIn(BaseModel):
    text: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)


class SynthOut(BaseModel):
    audio_ref: str
    model_id: str


class CommandIn(BaseModel):
    text: str = Field(min_length=1)


class NoteEraseOut(BaseModel):
    audio_ref: str | None
    memory_blobs_deleted: int


# --- F7: electronic invoicing (FR-9, docs/adr/0009, 0010, 0011) ---


class FiscalProfileIn(BaseModel):
    denominazione: str = Field(min_length=1, max_length=200)
    piva: str | None = Field(default=None, max_length=28)
    codice_fiscale: str | None = Field(default=None, max_length=16)
    regime_fiscale: str = Field(default="RF01", max_length=4)
    paese: str = Field(default="IT", max_length=2)
    indirizzo: str = Field(default="", max_length=200)
    cap: str = Field(default="", max_length=10)
    comune: str = Field(default="", max_length=120)
    provincia: str | None = Field(default=None, max_length=4)
    nazione: str = Field(default="IT", max_length=2)


class FiscalProfileOut(BaseModel):
    denominazione: str
    piva: str | None
    codice_fiscale: str | None
    regime_fiscale: str
    conservation_adhesion: str
    version: int


class ConservationAdhesionIn(BaseModel):
    adhesion: str = Field(pattern="^(none|requested|active)$")


class InvoiceCreateIn(BaseModel):
    client_tag_id: uuid.UUID
    year: int | None = None
    series: str = Field(default="A", max_length=20)
    causale: str | None = Field(default=None, max_length=200)


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=1000)
    unit_price: Decimal
    quantity: Decimal = Decimal(1)
    vat_rate: Decimal = Decimal(22)
    natura: str | None = Field(default=None, max_length=4)


class InvoiceLineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    natura: str | None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    client_tag_id: uuid.UUID
    kind: InvoiceKind
    document_type: DocumentType
    parent_invoice_id: uuid.UUID | None
    series: str
    year: int
    number: int | None
    state: InvoiceState
    currency: str
    taxable: Decimal
    vat: Decimal
    total: Decimal
    identificativo_sdi: str | None
    sdi_status: SdiStatus
    payment_status: PaymentStatus
    conservation_status: ConservationStatus
    version: int


class TransmitIn(BaseModel):
    progressivo: str | None = None


class CreditNoteIn(BaseModel):
    parent_invoice_id: uuid.UUID
    causale: str | None = Field(default=None, max_length=200)


class ReceiptIn(BaseModel):
    identificativo_sdi: str = Field(min_length=1, max_length=40)
    outcome: str = Field(pattern="^(RC|MC|NS|AT)$")


class InvoiceXmlOut(BaseModel):
    xml: str


# --- F8: notifications, recurrence, reminders (FR-12) ---


class NotificationPrefIn(BaseModel):
    user_id: uuid.UUID
    channel: NotificationChannelKind
    enabled: bool = True
    target: str = Field(default="", max_length=320)


class NotificationPrefOut(BaseModel):
    user_id: uuid.UUID
    channel: NotificationChannelKind
    enabled: bool
    target: str


class NotificationOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    channel: NotificationChannelKind
    kind: str
    title: str
    body: str
    status: NotificationStatus


class DispatchOut(BaseModel):
    sent: int
    failed: int


class RecurrenceIn(BaseModel):
    task_id: uuid.UUID
    freq: RecurrenceFreq
    next_run: datetime.datetime
    interval: int = Field(default=1, ge=1)
    until: datetime.datetime | None = None


class RecurrenceOut(BaseModel):
    task_id: uuid.UUID
    freq: RecurrenceFreq
    interval: int
    next_run: datetime.datetime
    until: datetime.datetime | None
    active: bool


class CountOut(BaseModel):
    count: int
