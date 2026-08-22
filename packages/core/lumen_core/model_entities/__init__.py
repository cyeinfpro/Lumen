"""Domain-grouped SQLAlchemy entities used by the legacy models facade."""

from .accounts import (
    USER_API_CREDENTIAL_STATUSES,
    User,
    AllowedEmail,
    AuthSession,
    SystemPrompt,
    ApiSupplierTemplate,
    UserApiCredential,
    PendingApiKeyVerification,
)

from .conversations import (
    Conversation,
    Message,
    UserMemoryScope,
    UserMemory,
    UserMemoryStaging,
    MemoryAudit,
)

from .agents import (
    AgentCapabilityGrant,
    AgentSession,
    AgentRun,
    AgentRunReference,
    AgentToolCall,
)

from .tasks import (
    Generation,
    Completion,
    VideoGeneration,
)

from .media_workflows import (
    Image,
    Video,
    ImageVariant,
    ImageVariantClaim,
    WorkflowRun,
    WorkflowStep,
    ModelCandidate,
    QualityReport,
    Share,
    OutboxEvent,
)

from .billing_operations import (
    UserWallet,
    WalletTransaction,
    BillingWindowUsageEvent,
    PricingRule,
    RedemptionBatch,
    RedemptionCode,
    RedemptionCodeUsage,
    InviteLink,
    SystemSetting,
    AuditLog,
    TelegramBinding,
)

from .libraries import (
    ModelLibraryItem,
    ModelLibraryHiddenPreset,
    PosterStyleItem,
    PosterStyleHiddenPreset,
    PosterMaster,
    PosterRender,
    OutboxDeadLetter,
)

from .storage_operations import StorageApplyOperation

__all__ = [
    "USER_API_CREDENTIAL_STATUSES",
    "User",
    "AllowedEmail",
    "AuthSession",
    "SystemPrompt",
    "ApiSupplierTemplate",
    "UserApiCredential",
    "PendingApiKeyVerification",
    "Conversation",
    "Message",
    "UserMemoryScope",
    "UserMemory",
    "UserMemoryStaging",
    "MemoryAudit",
    "AgentCapabilityGrant",
    "AgentSession",
    "AgentRun",
    "AgentRunReference",
    "AgentToolCall",
    "Generation",
    "Completion",
    "VideoGeneration",
    "Image",
    "Video",
    "ImageVariant",
    "ImageVariantClaim",
    "WorkflowRun",
    "WorkflowStep",
    "ModelCandidate",
    "QualityReport",
    "Share",
    "OutboxEvent",
    "UserWallet",
    "WalletTransaction",
    "BillingWindowUsageEvent",
    "PricingRule",
    "RedemptionBatch",
    "RedemptionCode",
    "RedemptionCodeUsage",
    "InviteLink",
    "SystemSetting",
    "AuditLog",
    "TelegramBinding",
    "ModelLibraryItem",
    "ModelLibraryHiddenPreset",
    "PosterStyleItem",
    "PosterStyleHiddenPreset",
    "PosterMaster",
    "PosterRender",
    "OutboxDeadLetter",
    "StorageApplyOperation",
]
