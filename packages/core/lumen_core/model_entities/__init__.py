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

from .tasks import (
    Generation,
    Completion,
    VideoGeneration,
)

from .media_workflows import (
    Image,
    Video,
    ImageVariant,
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
    "Generation",
    "Completion",
    "VideoGeneration",
    "Image",
    "Video",
    "ImageVariant",
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
]
