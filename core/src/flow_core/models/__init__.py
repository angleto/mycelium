"""ORM models. Import all so Alembic sees the metadata."""

from __future__ import annotations

from flow_core.models.activity_log import ActivityLog
from flow_core.models.adjudication import (
    Adjudication,
    AdjudicationStatus,
    AdjudicationStep,
    AdjudicationStepKind,
)
from flow_core.models.agent_run import AgentRun, AgentRunStatus
from flow_core.models.agent_token import AgentToken
from flow_core.models.annotation import (
    ANNOTATION_DOC_KINDS,
    ANNOTATION_KINDS,
    ANNOTATION_STATUSES,
    Annotation,
)
from flow_core.models.attachment import Attachment
from flow_core.models.auth_tokens import (
    EmailVerificationToken,
    PasswordResetToken,
    RefreshToken,
    RevokedToken,
)
from flow_core.models.base import Base
from flow_core.models.billing import (
    BillingConfig,
    CostBasis,
    CreditLedger,
    DefaultRateCard,
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
from flow_core.models.capability_token import CapabilityToken
from flow_core.models.classification_feedback import ClassificationFeedback
from flow_core.models.classification_personal_prior import ClassificationPersonalPrior
from flow_core.models.classification_personal_prior_snapshot import (
    ClassificationPersonalPriorSnapshot,
)
from flow_core.models.client_profile import ClientProfile
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
from flow_core.models.entity_revision import EntityRevision
from flow_core.models.event_outbox import EventOutbox
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.garden_graph_snapshot import GardenGraphSnapshot
from flow_core.models.garden_health import GardenHealthDaily
from flow_core.models.google_calendar import (
    CalendarSubscription,
    GoogleCalendarStatus,
)
from flow_core.models.invoice import (
    BuyerVerdict,
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
from flow_core.models.note import (
    Note,
    NoteKind,
    NoteMaturity,
    NoteStatus,
    NoteTurn,
    TurnRole,
)
from flow_core.models.note_coactivity import NoteCoactivity
from flow_core.models.note_link import (
    NOTE_NOTE_LINK_KINDS,
    NOTE_TASK_LINK_KINDS,
    NoteNoteLink,
    NoteTaskLink,
)
from flow_core.models.note_part import NotePart, NotePartUIState
from flow_core.models.note_part_index_pointer import NotePartIndexPointer
from flow_core.models.note_tag import NoteTag
from flow_core.models.notification import (
    Notification,
    NotificationChannelKind,
    NotificationPref,
    NotificationStatus,
    RecurrenceFreq,
    TaskRecurrence,
)
from flow_core.models.org_embedder_provider import EmbedderProviderKind, OrgEmbedderProvider
from flow_core.models.org_llm_provider import LLMProviderKind, OrgLLMProvider
from flow_core.models.organization import Organization
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.push_subscription import PushSubscription
from flow_core.models.schedule import Schedule
from flow_core.models.sdi_mandate import SdiMandate, SdiMandateStatus
from flow_core.models.sdi_notification import InvoiceNotification, ReceivedInvoiceNotification
from flow_core.models.sdi_received import ReceivedInvoice
from flow_core.models.search_click import (
    SEARCH_CLICK_KINDS,
    SEARCH_CLICK_QUERY_MAX,
    SearchClick,
)
from flow_core.models.tag import Tag, TagKind
from flow_core.models.tag_scope import TagScope
from flow_core.models.task import (
    ConstraintKind,
    ExecKind,
    Necessity,
    ScheduleMode,
    Task,
)
from flow_core.models.task_checklist_item import TaskChecklistItem
from flow_core.models.task_collaborator import TaskCollaborator
from flow_core.models.task_handoff import HandoffStatus, TaskHandoff
from flow_core.models.task_index_pointer import TaskIndexPointer
from flow_core.models.task_participant import TaskParticipant
from flow_core.models.task_relation import TaskRelation
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
    "ANNOTATION_DOC_KINDS",
    "ANNOTATION_KINDS",
    "ANNOTATION_STATUSES",
    "DEFAULT_AUTONOMOUS_DISPATCH",
    "NOTE_NOTE_LINK_KINDS",
    "NOTE_TASK_LINK_KINDS",
    "SEARCH_CLICK_KINDS",
    "SEARCH_CLICK_QUERY_MAX",
    "ActivityLog",
    "Adjudication",
    "AdjudicationStatus",
    "AdjudicationStep",
    "AdjudicationStepKind",
    "AgentRun",
    "AgentRunStatus",
    "AgentToken",
    "Annotation",
    "Attachment",
    "AutonomousDispatch",
    "Base",
    "BillingConfig",
    "BlobSource",
    "Budget",
    "BudgetPeriod",
    "BuyerVerdict",
    "CalendarHoliday",
    "CalendarSubscription",
    "CapabilityToken",
    "ClassificationFeedback",
    "ClassificationPersonalPrior",
    "ClassificationPersonalPriorSnapshot",
    "ClientProfile",
    "ConservationAdhesion",
    "ConservationStatus",
    "ConstraintKind",
    "CostBasis",
    "CreditLedger",
    "DefaultRateCard",
    "DependencyType",
    "DispatchRequest",
    "DispatchStatus",
    "DocumentType",
    "EmailAccount",
    "EmailAccountStatus",
    "EmailMessage",
    "EmailProvider",
    "EmailVerificationToken",
    "EmbedderProviderKind",
    "EntityRevision",
    "EventOutbox",
    "ExecKind",
    "Executor",
    "ExecutorKind",
    "GardenGraphSnapshot",
    "GardenHealthDaily",
    "GoogleCalendarStatus",
    "HandoffStatus",
    "Identity",
    "IdentityKind",
    "Invoice",
    "InvoiceCounter",
    "InvoiceKind",
    "InvoiceLine",
    "InvoiceNotification",
    "InvoiceState",
    "IssuerProfile",
    "LLMProviderKind",
    "LedgerEntryKind",
    "Membership",
    "MemoryBlob",
    "Necessity",
    "Note",
    "NoteCoactivity",
    "NoteKind",
    "NoteMaturity",
    "NoteNoteLink",
    "NotePart",
    "NotePartIndexPointer",
    "NotePartUIState",
    "NoteStatus",
    "NoteTag",
    "NoteTaskLink",
    "NoteTurn",
    "Notification",
    "NotificationChannelKind",
    "NotificationPref",
    "NotificationStatus",
    "OrgEmbedderProvider",
    "OrgLLMProvider",
    "Organization",
    "PasswordResetToken",
    "PaymentStatus",
    "ProjectProfile",
    "PushSubscription",
    "RateCard",
    "RateUnit",
    "ReceivedInvoice",
    "ReceivedInvoiceNotification",
    "RecurrenceFreq",
    "RefreshToken",
    "RevokedToken",
    "Role",
    "Schedule",
    "ScheduleMode",
    "SdiMandate",
    "SdiMandateStatus",
    "SdiStatus",
    "SdiTransmissionCounter",
    "SearchClick",
    "StorageKind",
    "StorageRate",
    "Tag",
    "TagKind",
    "TagScope",
    "Task",
    "TaskChecklistItem",
    "TaskCollaborator",
    "TaskDependency",
    "TaskHandoff",
    "TaskIndexPointer",
    "TaskParticipant",
    "TaskRecurrence",
    "TaskRelation",
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
