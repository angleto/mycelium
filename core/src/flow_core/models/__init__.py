"""ORM models. Import all so Alembic sees the metadata."""

from __future__ import annotations

from flow_core.models.activity_log import ActivityLog
from flow_core.models.agent_run import AgentRun, AgentRunStatus
from flow_core.models.agent_token import AgentToken
from flow_core.models.attachment import Attachment
from flow_core.models.auth_tokens import (
    EmailVerificationToken,
    PasswordResetToken,
    RevokedToken,
)
from flow_core.models.base import Base
from flow_core.models.billing import (
    BillingConfig,
    CostBasis,
    CreditLedger,
    LedgerEntryKind,
    RateCard,
    RateUnit,
    StorageKind,
    StorageRate,
    UsageRecord,
    Wallet,
)
from flow_core.models.budget import Budget, BudgetPeriod
from flow_core.models.calendar import (
    CalendarHoliday,
    UserCalendar,
    WorkingCalendar,
)
from flow_core.models.client_profile import ClientProfile
from flow_core.models.comment import Comment
from flow_core.models.dependency import DependencyType, TaskDependency
from flow_core.models.dispatch_request import (
    ACTIVE_DISPATCH_STATUSES,
    DEFAULT_AUTONOMOUS_DISPATCH,
    AutonomousDispatch,
    DispatchRequest,
    DispatchStatus,
)
from flow_core.models.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailMessage,
    EmailProvider,
)
from flow_core.models.event import Event, EventParticipant
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.google_calendar import (
    CalendarSubscription,
    GoogleCalendarStatus,
)
from flow_core.models.invoice import (
    ConservationAdhesion,
    ConservationStatus,
    DocumentType,
    Invoice,
    InvoiceCounter,
    InvoiceKind,
    InvoiceLine,
    InvoiceState,
    IssuerProfile,
    PaymentStatus,
    SdiStatus,
    SdiTransmissionCounter,
)
from flow_core.models.membership import Membership, Role
from flow_core.models.memory_blob import BlobSource, MemoryBlob, Tier
from flow_core.models.note import Note, NoteKind, NoteStatus, NoteTurn, TurnRole
from flow_core.models.note_tag import NoteTag
from flow_core.models.notification import (
    Notification,
    NotificationChannelKind,
    NotificationPref,
    NotificationStatus,
    RecurrenceFreq,
    TaskRecurrence,
)
from flow_core.models.organization import Organization
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.schedule import Schedule
from flow_core.models.sdi_mandate import SdiMandate, SdiMandateStatus
from flow_core.models.tag import Tag, TagKind
from flow_core.models.tag_scope import TagScope
from flow_core.models.task import (
    ConstraintKind,
    ExecKind,
    Necessity,
    ScheduleMode,
    Task,
)
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_handoff import HandoffStatus, TaskHandoff
from flow_core.models.task_tag import TaskTag
from flow_core.models.telegram import TelegramLink, TelegramLinkCode, TelegramUpdate
from flow_core.models.time_entry import TimeEntry, TimeSource
from flow_core.models.user import User
from flow_core.models.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
)

__all__ = [
    "ACTIVE_DISPATCH_STATUSES",
    "DEFAULT_AUTONOMOUS_DISPATCH",
    "ActivityLog",
    "AgentRun",
    "AgentRunStatus",
    "AgentToken",
    "Attachment",
    "AutonomousDispatch",
    "Base",
    "BillingConfig",
    "BlobSource",
    "Budget",
    "BudgetPeriod",
    "CalendarHoliday",
    "CalendarSubscription",
    "ClientProfile",
    "Comment",
    "ConservationAdhesion",
    "ConservationStatus",
    "ConstraintKind",
    "CostBasis",
    "CreditLedger",
    "DependencyType",
    "DispatchRequest",
    "DispatchStatus",
    "DocumentType",
    "EmailAccount",
    "EmailAccountStatus",
    "EmailMessage",
    "EmailProvider",
    "EmailVerificationToken",
    "Event",
    "EventParticipant",
    "ExecKind",
    "Executor",
    "ExecutorKind",
    "GoogleCalendarStatus",
    "HandoffStatus",
    "Invoice",
    "InvoiceCounter",
    "InvoiceKind",
    "InvoiceLine",
    "InvoiceState",
    "IssuerProfile",
    "LedgerEntryKind",
    "Membership",
    "MemoryBlob",
    "Necessity",
    "Note",
    "NoteKind",
    "NoteStatus",
    "NoteTag",
    "NoteTurn",
    "Notification",
    "NotificationChannelKind",
    "NotificationPref",
    "NotificationStatus",
    "Organization",
    "PasswordResetToken",
    "PaymentStatus",
    "ProjectProfile",
    "RateCard",
    "RateUnit",
    "RecurrenceFreq",
    "RevokedToken",
    "Role",
    "Schedule",
    "ScheduleMode",
    "SdiMandate",
    "SdiMandateStatus",
    "SdiStatus",
    "SdiTransmissionCounter",
    "StorageKind",
    "StorageRate",
    "Tag",
    "TagKind",
    "TagScope",
    "Task",
    "TaskAssignee",
    "TaskDependency",
    "TaskHandoff",
    "TaskRecurrence",
    "TaskTag",
    "TelegramLink",
    "TelegramLinkCode",
    "TelegramUpdate",
    "Tier",
    "TimeEntry",
    "TimeSource",
    "TurnRole",
    "UsageRecord",
    "User",
    "UserCalendar",
    "Wallet",
    "WorkflowDefinition",
    "WorkflowState",
    "WorkflowTransition",
    "WorkingCalendar",
]
