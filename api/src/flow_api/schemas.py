"""API I/O schemas (pydantic v2). No business logic."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from flow_core.models.agent_run import AgentRunStatus
from flow_core.models.billing import CostBasis, RateUnit, StorageKind
from flow_core.models.budget import BudgetPeriod
from flow_core.models.dependency import DependencyType
from flow_core.models.dispatch_request import AutonomousDispatch, DispatchStatus
from flow_core.models.email import EmailAccountStatus, EmailProvider
from flow_core.models.executor import ExecutorKind
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
from flow_core.models.task import (
    ConstraintKind,
    ExecKind,
    Necessity,
    ScheduleMode,
    SchedulePolicy,
)
from flow_core.models.task_handoff import HandoffStatus
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


class MeOut(BaseModel):
    """Canonical identity for the SPA (hydrated on load). is_admin is
    server-checked here; the JWT claim is only a render hint."""

    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    is_admin: bool


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None
    is_admin: bool
    is_active: bool
    email_verified: bool
    mfa_enabled: bool
    created_at: datetime.datetime


class AdminUserPatchIn(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


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


DEFAULT_ESTIMATE_PRESETS: list[Decimal] = [
    Decimal("0.5"),
    Decimal("1"),
    Decimal("4"),
    Decimal("8"),
]


class WorkspaceSettings(BaseModel):
    # Task-estimate dropdown values, in hours. Configurable per
    # workspace; the task form adds a "custom value" beyond these.
    estimate_presets: list[Decimal] = Field(default_factory=lambda: list(DEFAULT_ESTIMATE_PRESETS))
    default_client_tag_id: uuid.UUID | None = None
    # The autonomous-dispatch policy (docs/adr/0025 P5). Governance
    # default is human-in-the-loop: an unset value is ``approval_required``
    # (the loop creates pending requests a human must approve), never
    # ``auto`` (no silent auto-spend).
    autonomous_dispatch: AutonomousDispatch = AutonomousDispatch.approval_required


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    # The caller's *raw* membership role (the entitlement ceiling): the
    # SPA uses it to know which roles its "act as" switch may offer. A
    # freshly created workspace's caller is always its owner; /me sets
    # this explicitly from the membership (or "owner" for a global
    # admin acting without one).
    my_role: str = "owner"


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None = None
    role: str
    created_at: datetime.datetime


class MemberAddIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(min_length=1, max_length=16)


class MemberRoleIn(BaseModel):
    role: str = Field(min_length=1, max_length=16)


class WorkspacePatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class WorkspaceSettingsIn(BaseModel):
    expected_version: int = Field(ge=1)
    estimate_presets: list[Decimal] = Field(min_length=1, max_length=20)
    # The autonomous-dispatch policy (docs/adr/0025 P5). Optional so an
    # estimate-presets save does not have to restate it; only written
    # when present (owner-gated, like the rest of the namespace).
    autonomous_dispatch: AutonomousDispatch | None = None

    @field_validator("estimate_presets")
    @classmethod
    def _positive_sorted_unique(cls, v: list[Decimal]) -> list[Decimal]:
        if any(x <= 0 for x in v):
            raise ValueError("estimate presets must be positive")
        return sorted(set(v))


class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspaceSummaryOut(BaseModel):
    """A workspace the authenticated user belongs to (pre-tenant
    selection, for the in-app switcher)."""

    id: uuid.UUID
    name: str
    role: str
    status: str


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
    # Scope: project/client tag ids this tag is restricted to. Empty =
    # global (available everywhere).
    scope_target_ids: list[uuid.UUID] = []
    version: int


class TagScopeIn(BaseModel):
    target_ids: list[uuid.UUID] = []


class TagBrief(BaseModel):
    id: uuid.UUID
    kind: TagKind
    name: str
    color: str | None


class ClientCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ragione_sociale: str = Field(min_length=1, max_length=200)
    nome: str | None = Field(default=None, max_length=60)
    cognome: str | None = Field(default=None, max_length=60)
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
    # Per-client invoice sezionale (series prefix). None -> auto-derived from
    # the name on the first invoice. Optional override here.
    invoice_series: str | None = Field(default=None, max_length=20)
    # Client-specific payment IBAN (precedence: invoice > client >
    # issuer). Optional.
    payment_iban: str | None = Field(default=None, max_length=34)
    description: str | None = None
    default_billable: bool = True
    tariffa: Decimal | None = None
    valuta: str = Field(default="EUR", max_length=3)
    # IANA timezone name (e.g. "Europe/Rome"); optional.
    timezone: str | None = Field(default=None, max_length=64)
    # Per-client payment defaults (FatturaPA TPxx / MPxx). NULL = inherit
    # from the issuer (then system default TP02 / MP05).
    default_condizioni_pagamento: str | None = Field(default=None, max_length=4)
    default_modalita_pagamento: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    # Locale for the courtesy PDF when the client is foreign (BCP47 tag,
    # e.g. "it", "en", "de"). NULL -> "it". The FatturaPA XML stays
    # untouched (SdI ignores this field; legal causale/dicitura remain
    # in Italian regardless).
    invoice_language: str | None = Field(default=None, max_length=8)


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    client_tag_id: uuid.UUID | None = None
    budget: Decimal | None = None
    color: str | None = Field(default=None, max_length=16)
    description: str | None = None


class ClientPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    ragione_sociale: str | None = Field(default=None, min_length=1, max_length=200)
    nome: str | None = Field(default=None, max_length=60)
    cognome: str | None = Field(default=None, max_length=60)
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
    invoice_series: str | None = Field(default=None, max_length=20)
    payment_iban: str | None = Field(default=None, max_length=34)
    description: str | None = None
    default_billable: bool | None = None
    tariffa: Decimal | None = None
    valuta: str | None = Field(default=None, max_length=3)
    timezone: str | None = Field(default=None, max_length=64)
    default_condizioni_pagamento: str | None = Field(default=None, max_length=4)
    default_modalita_pagamento: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    invoice_language: str | None = Field(default=None, max_length=8)


class ClientOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    version: int
    ragione_sociale: str
    nome: str | None
    cognome: str | None
    id_paese: str | None
    id_codice: str | None
    codice_fiscale: str | None
    indirizzo: str | None
    cap: str | None
    comune: str | None
    provincia: str | None
    nazione: str | None
    codice_destinatario: str | None
    pec: str | None
    invoice_series: str | None
    payment_iban: str | None
    description: str | None
    default_billable: bool
    tariffa: Decimal | None
    valuta: str
    timezone: str | None
    default_condizioni_pagamento: str | None
    default_modalita_pagamento: str | None
    default_payment_terms_days: int | None
    invoice_language: str | None


class ProjectPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    client_tag_id: uuid.UUID | None = None
    budget: Decimal | None = None
    color: str | None = Field(default=None, max_length=16)
    description: str | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    version: int
    client_tag_id: uuid.UUID | None
    budget: Decimal | None
    color: str | None
    description: str | None


class TaskCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    priority: int = Field(default=3, ge=1, le=25)
    importance: int | None = Field(default=None, ge=1, le=5)
    urgency: int | None = Field(default=None, ge=1, le=5)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    billable: bool | None = None
    parent_task_id: uuid.UUID | None = None
    # docs/adr/0028: ``executor_kind`` kept as an optional input for
    # API consumers that did not migrate yet; it is ignored by the
    # service (the routing kind comes from the resolved identity).
    # ``executor_user_id`` removed.
    executor_kind: ExecKind = ExecKind.human
    assignee_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    # Handle-based assignee (#21 Stage B). When set, the service
    # resolves the handle to a user / ai_assistant and writes the
    # legacy executor_kind / executor_user_id mirror columns too.
    assignee_handle: str | None = Field(default=None, max_length=40)
    estimate_effort_h: Decimal | None = None
    # Capabilities the task needs from its executor (docs/adr/0025 P2).
    # Empty = any enabled agent. Additive, default [].
    required_capabilities: list[str] = Field(default_factory=list)
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
    priority: int | None = Field(default=None, ge=1, le=25)
    importance: int | None = Field(default=None, ge=1, le=5)
    urgency: int | None = Field(default=None, ge=1, le=5)
    start_date: datetime.date | None = None
    due_date: datetime.date | None = None
    billable: bool | None = None
    estimate_effort_h: Decimal | None = None
    # docs/adr/0028: ``executor_kind`` is no longer persisted (read
    # via identity), ``executor_user_id`` is removed. Updates set
    # ``assignee_id`` (or ``assignee_handle`` which is resolved
    # service-side) and optionally ``owner_id`` to reassign
    # accountability. Empty assignee_handle clears the assignment.
    assignee_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    assignee_handle: str | None = Field(default=None, max_length=40)
    required_capabilities: list[str] | None = None
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
    is_hidden: bool = False
    description: str | None = None


class TransitionOut(BaseModel):
    from_state_id: uuid.UUID
    to_state_id: uuid.UUID


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
    # docs/adr/0028 Stage C: identity-based addressing.
    # ``assignee_id`` is the FK into ``identities``; ``assignee_handle``
    # is the denormalised display handle for the SPA's task card
    # (resolved at serialisation, NULL when unassigned). ``owner_id``
    # is the accountability axis (always a real user). The legacy
    # ``executor_kind`` is no longer carried; the SPA can derive it
    # via the identity payload when needed.
    assignee_id: uuid.UUID | None = None
    assignee_handle: str | None = None
    # docs/adr/0028 Punto 4: kind of the assignee identity
    # (user / ai_assistant) resolved at serialisation. NULL when the
    # task is unassigned. The SPA renders an IdentityBadge from this
    # without re-querying ``/identities`` per row.
    assignee_kind: str | None = None
    owner_id: uuid.UUID
    # ``executor_kind`` is re-exposed for SPA backward compat
    # (cards/filters/graph still consume it). The serializer fills it
    # from the resolved identity when ``assignee_id`` is set, else
    # from the task's fallback ``executor_kind`` hint (docs/adr/0029
    # SPA P2 follow-up).
    executor_kind: ExecKind
    estimate_effort_h: Decimal | None
    required_capabilities: list[str] = Field(default_factory=list)
    monetary_cost: Decimal | None
    location: str | None
    necessity: Necessity
    budget_id: uuid.UUID | None
    billable: bool | None = None
    is_archived: bool
    # Contract-net (docs/adr/0025, P4): the task is announced and
    # awaiting a member ``claim`` (cleared on claim).
    offered: bool = False
    deleted_at: datetime.datetime | None = None
    # Recency timestamps (TimestampMixin). ``created_at`` is set on insert;
    # ``updated_at`` is bumped on every mutation (onupdate=now()), so the SPA
    # can sort by ``updated_at`` desc to surface the most recently
    # created/modified tasks first (Recent-tasks widget).
    created_at: datetime.datetime
    updated_at: datetime.datetime
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
    is_hidden: bool = False
    description: str | None = None


class TransitionIn(BaseModel):
    from_state: str
    to_state: str


class WorkflowCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    states: list[WorkflowStateSpecIn]
    transitions: list[TransitionIn] = Field(default_factory=list)


class WorkflowStateEditIn(BaseModel):
    id: uuid.UUID | None = None  # None = new state
    name: str = Field(min_length=1, max_length=80)
    ord: int = 0
    is_initial: bool = False
    is_terminal: bool = False
    is_hidden: bool = False
    description: str | None = None


class WorkflowPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    states: list[WorkflowStateEditIn]
    transitions: list[TransitionIn] = Field(default_factory=list)


class WorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
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


class TaskRelationCreateIn(BaseModel):
    task_id: uuid.UUID
    other_id: uuid.UUID


class TaskRelationOut(BaseModel):
    id: uuid.UUID
    task_a_id: uuid.UUID
    task_b_id: uuid.UUID
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
    # Resource-leveling objective (docs/adr/0025, P1).
    policy: SchedulePolicy = SchedulePolicy.balanced


class RecomputeOut(BaseModel):
    count: int
    # Projections so policies are comparable (docs/adr/0025).
    makespan_minutes: int
    projected_credit_cost: Decimal
    policy: SchedulePolicy
    # Count of llm tasks with no admissible executor (P2 dispatch gaps).
    unassignable_count: int = 0


class ScheduleOut(BaseModel):
    task_id: uuid.UUID
    es: datetime.datetime | None
    ef: datetime.datetime | None
    ls: datetime.datetime | None
    lf: datetime.datetime | None
    slack_minutes: int | None
    on_logical_critical_path: bool
    # Resource-aware critical chain + projected LLM credit cost
    # (docs/adr/0025, P1).
    on_critical_chain: bool
    projected_cost: Decimal
    scheduled_start: datetime.datetime | None
    scheduled_end: datetime.datetime | None
    # Admission-control dispatch result (docs/adr/0025, P2): the chosen
    # executor, or a flagged dispatch gap with a short stable reason.
    assigned_executor_id: uuid.UUID | None = None
    unassignable: bool = False
    unassignable_reason: str | None = None
    computed_at: datetime.datetime
    input_fingerprint: str | None


# --- Executor registry (docs/adr/0025, P2) ---
# Reads are member-level; mutations are owner-gated in the service
# (mirrors the rate-card / issuer-profile precedent).


class ExecutorOut(BaseModel):
    id: uuid.UUID
    kind: ExecutorKind
    name: str
    user_id: uuid.UUID | None
    context_switch_cost_minutes: int
    provider: str | None
    model_id: str | None
    max_parallel: int
    credit_budget: Decimal | None
    credit_rate_per_hour: Decimal
    enabled: bool
    capability_tags: list[str] = Field(default_factory=list)
    version: int


class ExecutorCreateIn(BaseModel):
    kind: ExecutorKind = ExecutorKind.llm_agent
    name: str = Field(min_length=1, max_length=120)
    # Required + must be a workspace member iff kind == human (the
    # service rejects an unbound human executor).
    user_id: uuid.UUID | None = None
    context_switch_cost_minutes: int = Field(default=0, ge=0)
    provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=120)
    max_parallel: int = Field(default=4, ge=1)
    credit_budget: Decimal | None = None
    credit_rate_per_hour: Decimal = Field(default=Decimal(0), ge=0)
    enabled: bool = True
    capability_tags: list[str] = Field(default_factory=list)


class ExecutorPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    # kind / user_id are immutable identity (not patchable).
    name: str | None = Field(default=None, min_length=1, max_length=120)
    context_switch_cost_minutes: int | None = Field(default=None, ge=0)
    provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=120)
    max_parallel: int | None = Field(default=None, ge=1)
    credit_budget: Decimal | None = None
    credit_rate_per_hour: Decimal | None = Field(default=None, ge=0)
    enabled: bool | None = None
    capability_tags: list[str] | None = None


# --- Agent execution runtime (docs/adr/0025, P3) ---
# Reads are member-level; start/cancel are owner-gated in the service
# (running an agent spends credits -> mirrors the billing-grant gate;
# effective-role sudo enforced).


class AgentRunOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    executor_id: uuid.UUID | None
    status: AgentRunStatus
    steps: int
    credits_spent: Decimal
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    error: str | None
    artifact_note_id: uuid.UUID | None
    cancel_requested: bool
    blocked_reason: str | None
    version: int


# --- P4: coordination handoffs (docs/adr/0025) ---


class HandoffOut(BaseModel):
    id: uuid.UUID
    predecessor_task_id: uuid.UUID
    successor_task_id: uuid.UUID
    from_executor_id: uuid.UUID | None
    to_executor_id: uuid.UUID | None
    message: str
    artifact_note_id: uuid.UUID | None
    status: HandoffStatus
    delivered_at: datetime.datetime | None
    consumed_at: datetime.datetime | None
    version: int


# --- P5: closed-loop dispatch + approval gates (docs/adr/0025) ---
# Reads are member-level (the queue is visible to the team); approve /
# deny / tick / policy-set are owner-gated in the service (a tick can
# spend credits via the P3 metered path -> mirrors the start_run /
# billing-grant gate; effective-role sudo enforced).


class DispatchRequestOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    # Denormalized for the queue UI (no extra round-trip): the task
    # title and the assigned executor's name. Defaults so the generated
    # TS type marks them optional appropriately.
    task_title: str = ""
    executor_id: uuid.UUID | None = None
    executor_name: str | None = None
    status: DispatchStatus
    projected_credit_cost: Decimal
    agent_run_id: uuid.UUID | None = None
    requested_at: datetime.datetime
    decided_at: datetime.datetime | None = None
    decided_by: uuid.UUID | None = None
    reason: str | None = None
    version: int


class DispatchDecisionIn(BaseModel):
    expected_version: int = Field(ge=1)
    # Optional short cause recorded on deny/skip (never free-form prose
    # to the user; trimmed/clamped server-side).
    reason: str | None = Field(default=None, max_length=200)


class DispatchTickIn(BaseModel):
    # Optional resource-leveling policy for the recompute the tick runs
    # (fastest|cheapest|balanced|throughput; default balanced), matching
    # the /schedule/recompute contract.
    policy: SchedulePolicy = SchedulePolicy.balanced


class DispatchTickOut(BaseModel):
    """The "last tick" summary the UI shows (counts of requests touched
    + the scheduler projections so the loop and schedule view agree)."""

    policy: AutonomousDispatch
    enabled: bool
    created: int
    approved: int
    dispatched: int
    skipped: int
    failed: int
    projected_makespan_minutes: int
    projected_credit_cost: Decimal


# --- F4: time tracking (FR-5, docs/adr/0002) ---


class TimeStartIn(BaseModel):
    # Proposal A: with ``note_id`` the billing task is derived from the
    # note's task (the note must be linked to one); ``task_id`` may be
    # omitted, or must agree if both are given.
    task_id: uuid.UUID | None = None
    # None = inherit (task override -> project default -> billable).
    billable: bool | None = None
    # Free-text memo on the entry (renamed from ``note``: it is NOT the
    # Note entity; use ``note_id`` for the work-note link).
    memo: str | None = None
    note_id: uuid.UUID | None = None
    # Double-play: run alongside other timers instead of replacing the
    # serial one (e.g. parallel LLM tasks).
    parallel: bool = False


class TimeStopIn(BaseModel):
    # Stop a specific task's running timer; omit to stop the serial one.
    task_id: uuid.UUID | None = None
    memo: str | None = None


class TimeManualIn(BaseModel):
    # As TimeStartIn: a ``note_id`` derives the billing task.
    task_id: uuid.UUID | None = None
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = None
    duration_seconds: int | None = Field(default=None, gt=0)
    billable: bool | None = None
    memo: str | None = None
    note_id: uuid.UUID | None = None


class TimeEntryPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    memo: str | None = None
    billable: bool | None = None
    # Reassign the entry to another task (transitively changes its
    # project/client). Adjust the recorded interval if the timer was
    # started late / never stopped; duration is recomputed server-side.
    task_id: uuid.UUID | None = None
    # Set/clear the work note this time was logged in. Sent only when
    # present in the request body (model_fields_set): an explicit null
    # clears the link, omitting it preserves the stored value.
    note_id: uuid.UUID | None = None
    started_at: datetime.datetime | None = None
    ended_at: datetime.datetime | None = None


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
    parallel: bool
    rate_snapshot: Decimal | None
    currency: str
    memo: str | None
    # Provenance: the work note this time was logged in (Proposal A),
    # with its resolved title so the report can show *where* time was
    # logged. Both null when the entry has no linked note.
    note_id: uuid.UUID | None = None
    note_title: str | None = None
    version: int
    # Resolved context (task -> project tag -> client tag -> client
    # profile) so the list / report need no N+1 round-trips.
    task_title: str | None = None
    client_tag_id: uuid.UUID | None = None
    client_name: str | None = None
    project_tag_id: uuid.UUID | None = None
    project_name: str | None = None
    client_timezone: str | None = None


class ReportRowOut(BaseModel):
    key: str | None
    label: str | None
    seconds: int
    billable_seconds: int
    amount: Decimal
    currency: str


class TaskTimeReportOut(BaseModel):
    task_id: uuid.UUID
    task_title: str | None
    client_tag_id: uuid.UUID | None
    client_name: str | None
    project_tag_id: uuid.UUID | None
    project_name: str | None
    client_timezone: str | None
    total_seconds: int
    billable_seconds: int
    entry_count: int


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
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    # Optional memory channel: a tag of kind ``memory_channel`` this
    # blob is filed under. Folded into the attached tag set.
    channel_tag_id: uuid.UUID | None = None
    # Deterministic alternative: the channel's stable ``system_key``
    # (e.g. "email", "telegram"). What integrations use. If both this
    # and ``channel_tag_id`` are given they must resolve to the same
    # channel. Manual writes stay channel-optional (omit both).
    channel_key: str | None = Field(default=None, max_length=64)


class MemorySearchIn(BaseModel):
    project_id: uuid.UUID | None = None
    query: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, gt=0, le=100)
    grader_min_rrf: float | None = None
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    # Optional memory channel: narrows to blobs filed under this
    # ``memory_channel`` tag (ANDed into the tag facet).
    channel_tag_id: uuid.UUID | None = None
    # Deterministic alternative: narrow by the channel's stable
    # ``system_key``. If both are given they must resolve to the same
    # channel.
    channel_key: str | None = Field(default=None, max_length=64)


class MemoryStatusOut(BaseModel):
    # True when semantic (vector) retrieval is available; False when the
    # optional embedding model is missing and memory is keyword-only.
    semantic: bool


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
    tags: list[TagBrief] = Field(default_factory=list)


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


# --- Memory channels (controlled, seeded vocabulary; FR-8) ---------


class MemoryChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    # Stable slug; None for a keyless custom channel. Integrations use
    # this, never the name.
    system_key: str | None
    # Enable/disable maps to the tag soft-state; False = disabled (not a
    # valid write/search target).
    enabled: bool
    # True for a canonical seeded channel: renamable and disable-able
    # but its key is immutable and it is not deletable.
    seeded: bool
    # Short read-only English copy keyed by ``system_key`` (manual/
    # agent/note); None for a keyless/custom channel.
    description: str | None = None
    version: int


class MemoryChannelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    system_key: str | None = Field(default=None, min_length=1, max_length=64)


class MemoryChannelPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # Enable/disable the channel (maps to the tag soft-state).
    enabled: bool | None = None
    # Only meaningful for a custom channel; changing a seeded channel's
    # key is rejected (channel.key_immutable).
    system_key: str | None = Field(default=None, min_length=1, max_length=64)


# --- F6b: notes / conversation / intent (FR-16, docs/adr/0020, 0021) ---


class NoteCreateIn(BaseModel):
    kind: NoteKind
    project_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    audio_ref: str | None = Field(default=None, max_length=512)
    audio_seconds: int | None = None


class NoteTagIn(BaseModel):
    tag_id: uuid.UUID


class NoteOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    # Set when the note is a task's "work note" (the SPA detects it to
    # open it from the task and bill its timer to the task).
    task_id: uuid.UUID | None = None
    kind: NoteKind
    status: NoteStatus
    title: str | None
    transcript: str | None
    summary: str | None
    audio_ref: str | None
    is_archived: bool = False
    deleted_at: datetime.datetime | None = None
    tags: list[TagBrief] = []
    version: int
    # docs/adr/0029 P1: garden lifecycle. ``maturity`` defaults to
    # ``seed`` (the migration backfilled every existing note). When
    # ``promoted_at`` is set the note is read-only at the service
    # layer (transplanted to a task).
    maturity: str = "seed"
    promoted_at: datetime.datetime | None = None


class NoteSetMaturityIn(BaseModel):
    maturity: str = Field(pattern="^(seed|growing|mature|dormant)$")


class NotePromoteIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)


class NoteDeriveTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    estimate_effort_h: Decimal | None = None
    # Optional tag inheritance: the SPA passes the note's tags (client
    # + project) so the derived task lands under the same project /
    # client as its parent note instead of the workspace default.
    extra_tag_ids: list[uuid.UUID] = Field(default_factory=list)


class NoteLinkIn(BaseModel):
    parent_note_id: uuid.UUID
    child_note_id: uuid.UUID
    kind: str = Field(pattern="^(atom_of|references|replies_to|supersedes)$")


class NoteLinkOut(BaseModel):
    id: uuid.UUID
    parent_note_id: uuid.UUID
    child_note_id: uuid.UUID
    kind: str
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime


class NoteTaskLinkOut(BaseModel):
    id: uuid.UUID
    note_id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    created_by: uuid.UUID | None = None
    created_at: datetime.datetime


class NoteWithLinksOut(BaseModel):
    note: NoteOut
    outgoing: list[NoteLinkOut] = []
    incoming: list[NoteLinkOut] = []
    task_links: list[NoteTaskLinkOut] = []


class DerivedTaskOut(BaseModel):
    task_id: uuid.UUID
    link: NoteTaskLinkOut


class NotePatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None
    # Bidirectional Proposal A link (NOTE side): set OR clear
    # notes.task_id. Honoured only when the key is present in the
    # request body (model_fields_set): an explicit null unlinks,
    # omitting it preserves the existing link.
    task_id: uuid.UUID | None = None
    # Voice-note audio binding (#46): set after the attachment upload
    # completes ("attachment:<id>"). Same model_fields_set semantics
    # as task_id (omit = no change, ``None`` = clear).
    audio_ref: str | None = Field(default=None, max_length=512)


class TaskNoteCreateIn(BaseModel):
    # POST /tasks/{task_id}/notes: title optional (defaults to the task
    # title); a fresh work note pre-linked to the task.
    title: str | None = Field(default=None, max_length=300)
    text: str | None = None


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


class AttachmentOut(BaseModel):
    # Metadata only: the binary ``data`` is NEVER serialised here (it is
    # streamed by GET /attachments/{id}/download). Exactly one of
    # note_id / task_id is set (the file's single parent).
    id: uuid.UUID
    note_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime.datetime


# --- F7: electronic invoicing (FR-9, docs/adr/0009, 0010, 0011) ---


class IssuerProfileIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
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
    rea: str | None = Field(default=None, max_length=40)
    # Fallback payment IBAN (precedence: invoice > client > issuer).
    default_iban: str | None = Field(default=None, max_length=34)
    riferimento_normativo: str | None = Field(default=None, max_length=100)
    nome: str | None = Field(default=None, max_length=60)
    cognome: str | None = Field(default=None, max_length=60)
    # Optional contact channels. PEC prints on the PDF; the rest go in
    # CedentePrestatore/Contatti (Telefono/Fax/Email).
    pec: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    telefono: str | None = Field(default=None, max_length=20)
    fax: str | None = Field(default=None, max_length=20)
    # Issuer-level fallbacks for payment metadata (used only when the
    # client carries no own default). Closed-enum codes (TPxx / MPxx);
    # validated server-side.
    default_condizioni_pagamento: str | None = Field(default=None, max_length=4)
    default_modalita_pagamento: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_default: bool = False


class IssuerProfilePatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    denominazione: str | None = Field(default=None, min_length=1, max_length=200)
    piva: str | None = Field(default=None, max_length=28)
    codice_fiscale: str | None = Field(default=None, max_length=16)
    regime_fiscale: str | None = Field(default=None, max_length=4)
    paese: str | None = Field(default=None, max_length=2)
    indirizzo: str | None = Field(default=None, max_length=200)
    cap: str | None = Field(default=None, max_length=10)
    comune: str | None = Field(default=None, max_length=120)
    provincia: str | None = Field(default=None, max_length=4)
    nazione: str | None = Field(default=None, max_length=2)
    rea: str | None = Field(default=None, max_length=40)
    default_iban: str | None = Field(default=None, max_length=34)
    riferimento_normativo: str | None = Field(default=None, max_length=100)
    nome: str | None = Field(default=None, max_length=60)
    cognome: str | None = Field(default=None, max_length=60)
    pec: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    telefono: str | None = Field(default=None, max_length=20)
    fax: str | None = Field(default=None, max_length=20)
    default_condizioni_pagamento: str | None = Field(default=None, max_length=4)
    default_modalita_pagamento: str | None = Field(default=None, max_length=4)
    default_payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_default: bool | None = None


class IssuerProfileOut(BaseModel):
    id: uuid.UUID
    label: str
    denominazione: str
    piva: str | None
    codice_fiscale: str | None
    regime_fiscale: str
    paese: str
    indirizzo: str
    cap: str
    comune: str
    provincia: str | None
    nazione: str
    rea: str | None
    default_iban: str | None
    riferimento_normativo: str | None
    nome: str | None
    cognome: str | None
    pec: str | None
    email: str | None
    telefono: str | None
    fax: str | None
    default_condizioni_pagamento: str | None
    default_modalita_pagamento: str | None
    default_payment_terms_days: int | None
    is_default: bool
    conservation_adhesion: str
    version: int


class InvoiceCounterOut(BaseModel):
    """A counter row for the admin override UI. ``max_emitted`` is the
    floor (the highest number already on an invoice under the same key);
    the new ``last_number`` must be >= ``max_emitted`` or the override is
    rejected."""

    issuer_profile_id: uuid.UUID
    series: str
    year: int
    last_number: int
    max_emitted: int


class InvoiceCounterPatchIn(BaseModel):
    last_number: int = Field(ge=0)


class ConservationAdhesionIn(BaseModel):
    adhesion: str = Field(pattern="^(none|requested|active)$")


class SdiMandateIn(BaseModel):
    reference: str | None = Field(default=None, max_length=200)


class SdiMandateOut(BaseModel):
    id: uuid.UUID
    issuer_profile_id: uuid.UUID
    status: str
    scope: str
    reference: str | None
    granted_at: datetime.datetime
    revoked_at: datetime.datetime | None
    version: int


class InvoiceCreateIn(BaseModel):
    client_tag_id: uuid.UUID
    issuer_profile_id: uuid.UUID | None = None
    year: int | None = None
    # None -> the service defaults to the client's own sezionale (per-client
    # numbering). An explicit value pins a custom series.
    series: str | None = Field(default=None, max_length=20)
    causale: str | None = Field(default=None, max_length=200)


class InvoicePatchIn(BaseModel):
    client_tag_id: uuid.UUID | None = None
    issuer_profile_id: uuid.UUID | None = None
    series: str | None = Field(default=None, max_length=20)
    currency: str | None = Field(default=None, max_length=3)
    causale: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    payment_iban: str | None = Field(default=None, max_length=34)
    payment_due_date: datetime.date | None = None
    # Per-document overrides of the client/issuer payment defaults
    # (FatturaPA TPxx / MPxx + net days). NULL = inherit (and the XML
    # falls through to the client, then issuer, then TP02 / MP05).
    condizioni_pagamento: str | None = Field(default=None, max_length=4)
    modalita_pagamento: str | None = Field(default=None, max_length=4)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=1000)
    unit_price: Decimal
    quantity: Decimal = Decimal(1)
    # None = unset: the service resolves it from the issuer's regime
    # (forfettario RF19 -> 0% + Natura N2.2; ordinary regime -> 22%).
    # An explicit value is always honoured.
    vat_rate: Decimal | None = None
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
    issuer_profile_id: uuid.UUID | None
    kind: InvoiceKind
    document_type: DocumentType
    parent_invoice_id: uuid.UUID | None
    series: str
    year: int
    number: int | None
    state: InvoiceState
    currency: str
    causale: str | None
    notes: str | None
    payment_iban: str | None
    payment_due_date: datetime.date | None
    condizioni_pagamento: str | None
    modalita_pagamento: str | None
    payment_terms_days: int | None
    taxable: Decimal
    vat: Decimal
    bollo: Decimal
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


class InvoicePreviewParty(BaseModel):
    """Resolved issuer or client identity for the preview. None when the
    draft has no profile resolved yet."""

    denominazione: str
    piva: str | None = None
    codice_fiscale: str | None = None
    regime_fiscale: str | None = None
    indirizzo: str | None = None
    cap: str | None = None
    comune: str | None = None
    provincia: str | None = None
    nazione: str | None = None
    # Client only (the SdI recipient address); None on the issuer side.
    codice_destinatario: str | None = None
    pec: str | None = None


class InvoicePreviewLine(BaseModel):
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    vat_rate: Decimal
    natura: str | None = None


class InvoicePreviewTotals(BaseModel):
    taxable: Decimal
    vat: Decimal
    bollo: Decimal
    total: Decimal


class InvoicePreviewOut(BaseModel):
    """Full resolved document for the SPA: it renders this without
    re-deriving anything. Tolerant of an incomplete draft (issuer/client
    may be null)."""

    number: str
    series: str
    year: int
    document_type: DocumentType
    date: datetime.date
    payment_due_date: datetime.date | None
    issuer: InvoicePreviewParty | None
    client: InvoicePreviewParty | None
    lines: list[InvoicePreviewLine]
    totals: InvoicePreviewTotals
    effective_iban: str | None
    # "invoice" | "client" | "issuer" | None.
    iban_source: str | None
    causale: str | None
    notes: str | None
    is_forfettario: bool
    state: InvoiceState
    # SdI transmission lifecycle, read-only (ADR-0011). Scalar status
    # fields on the invoice row -- there is no per-receipt history table
    # yet; richer notifiche need the SdI cooperative channel (out of
    # scope here, see the F7 report). ``identificativo_sdi`` is the
    # correlation id assigned at transmit (None until then / for manual
    # export); ``sdi_status`` is the latest outcome (none/RC/MC/NS/AT);
    # ``conservation_status`` is the AdE free-conservation coverage.
    identificativo_sdi: str | None
    sdi_status: SdiStatus
    conservation_status: ConservationStatus


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
    created_at: datetime.datetime
    sent_at: datetime.datetime | None = None


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


class ReminderIn(BaseModel):
    # Minutes before the task due date (0 = at due).
    offset_minutes: int = Field(ge=0, le=525600)


class ReminderOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    offset_minutes: int


# --- Telegram bot integration (epic #125 P2) ---


class TelegramLinkRequestOut(BaseModel):
    """The single-use deep-link payload the SPA renders as a button /
    QR. ``code`` is the short token embedded in the URL; ``deep_link``
    is the pre-built ``https://t.me/<bot>?start=<code>``."""

    code: str
    expires_at: datetime.datetime
    bot_username: str
    deep_link: str


class TelegramLinkStatusOut(BaseModel):
    linked: bool
    chat_username: str | None = None
    linked_at: datetime.datetime | None = None


class TelegramWebhookOut(BaseModel):
    ok: bool


class AgentTokenCreateIn(BaseModel):
    """Mint a long-lived bearer credential for MCP / external automation."""

    name: str = Field(min_length=1, max_length=120)
    scope: str = Field(default="mcp", min_length=1, max_length=32)
    # 1-year TTL by default (matches the service constant). ``None`` =
    # never expires; pass ``0`` to be explicit about "never". Capped at
    # 5 years so a forgotten secret has a bounded lifetime even when
    # the operator picks a long-lived rotation.
    ttl_days: int | None = Field(default=365, ge=0, le=365 * 5)


class AgentTokenOut(BaseModel):
    """Persisted token metadata. The raw value is never on this shape;
    it appears only on :class:`AgentTokenCreateOut`."""

    id: uuid.UUID
    name: str
    scope: str
    prefix: str
    expires_at: datetime.datetime | None
    last_used_at: datetime.datetime | None
    revoked_at: datetime.datetime | None
    created_at: datetime.datetime


class AgentTokenCreateOut(BaseModel):
    """The mint response. ``raw`` is the only place the plaintext token
    ever leaves the server; the operator is told to copy it now or
    rotate."""

    id: uuid.UUID
    name: str
    scope: str
    prefix: str
    expires_at: datetime.datetime | None
    created_at: datetime.datetime
    raw: str


# ---------- AI assistants (ADR-0XX, replaces /settings/mcp manual setup)
class ScopeCatalogEntry(BaseModel):
    """One row of the scope catalog returned by ``GET /ai-assistants/
    scope-catalog`` — drives the SPA's permission picker."""

    key: str
    category: str  # 'read' | 'write' | 'danger'
    label: str
    description: str


class ConnectorInfoOut(BaseModel):
    """Where to point an MCP client. The SPA shows ``mcp_url`` in the
    connector card and the operator pastes it into Claude / Cursor."""

    mcp_url: str
    instructions_md: str


class AiAssistantCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    scope: list[str] | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class AiAssistantPatchIn(BaseModel):
    expected_version: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=255)
    scope: list[str] | None = Field(default=None)
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = Field(default=None)


class AiAssistantOut(BaseModel):
    """Metadata; no secret ever ships in this shape."""

    id: uuid.UUID
    label: str
    provider: str | None
    model_id: str | None
    notes: str | None
    scope: list[str]
    is_active: bool
    version: int
    created_at: datetime.datetime
    updated_at: datetime.datetime
    # First chars of the latest secret (for UI disambiguation across
    # rotations). NULL when no token has been minted yet (shouldn't
    # happen via this flow but defensive).
    token_prefix: str | None = None


class AiAssistantCreatedOut(BaseModel):
    """Returned exactly once at create / rotate time. ``raw_secret`` is
    plaintext — the SPA copies it to the clipboard and shows a
    "credentials" card; after the user acknowledges, it cannot be
    recovered."""

    assistant: AiAssistantOut
    raw_secret: str
