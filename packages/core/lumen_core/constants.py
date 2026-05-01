"""DESIGN §1 / §5 / §6 / §22 里反复出现的枚举与常量——放一处避免字符串散落。"""

from __future__ import annotations

from enum import StrEnum


# --- request limits ---

# 单条用户 prompt 的产品级上限。前端、API schema、silent generation 共用这个数值；
# 不是数据库字段限制，而是为了避免误粘超长文本撑爆请求体 / 上游上下文。
MAX_PROMPT_CHARS = 10_000


# --- intent / messages ---

class Intent(StrEnum):
    CHAT = "chat"
    VISION_QA = "vision_qa"
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    PARTIAL = "partial"


# --- tasks ---

class GenerationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class GenerationStage(StrEnum):
    """生图任务进度阶段。

    分两层：
    - 粗阶段（QUEUED / UNDERSTANDING / RENDERING / FINALIZING）—— 持久化在
      `Generation.progress_stage` 字段；用户断线重连看到的"上次到了哪一步"。
    - 细子阶段（PROVIDER_SELECTED / STREAM_STARTED / PARTIAL_RECEIVED /
      FINAL_RECEIVED / PROCESSING / STORING）—— 仅用于 SSE 实时事件 payload，
      让 DevelopingCard 显影动画能跟到具体里程碑。前端不识别细阶段时按粗阶段降级。
    """

    # --- 粗阶段（持久化） ---
    QUEUED = "queued"
    UNDERSTANDING = "understanding"
    RENDERING = "rendering"
    FINALIZING = "finalizing"

    # --- 细子阶段（仅 SSE 进度事件） ---
    PROVIDER_SELECTED = "provider_selected"
    STREAM_STARTED = "stream_started"
    PARTIAL_RECEIVED = "partial_received"
    FINAL_RECEIVED = "final_received"
    PROCESSING = "processing"
    STORING = "storing"


# 粗 → 细的归并。前端不识别细阶段时降级到对应粗值。
GENERATION_STAGE_FALLBACK: dict[str, str] = {
    "provider_selected": "rendering",
    "stream_started": "rendering",
    "partial_received": "rendering",
    "final_received": "finalizing",
    "processing": "finalizing",
    "storing": "finalizing",
}


class GenerationErrorCode(StrEnum):
    """生图任务的失败原因。

    `Generation.error_code` 是 String(64)，与 StrEnum 的字符串值兼容；
    既覆盖我们自己抛的内部码，也囊括从上游 `error.code/error.type` 透传的常见码。
    上游可能出现 enum 之外的新 code——`upstream.py` 应当原样保留字符串落库，
    后续观察到稳定的码再补充到 enum 中。

    分类与重试规则参考 `apps/worker/app/retry.py`。
    """

    # --- terminal: 重试也不会变好 ---
    INVALID_VALUE = "invalid_value"
    INVALID_REQUEST_ERROR = "invalid_request_error"
    INVALID_PARAM = "invalid_param"
    IMAGE_GENERATION_USER_ERROR = "image_generation_user_error"
    AUTHENTICATION_ERROR = "authentication_error"
    PERMISSION_ERROR = "permission_error"
    UNAUTHORIZED = "unauthorized"
    BAD_REFERENCE_IMAGE = "bad_reference_image"
    REFERENCE_MISSING = "reference_missing"
    MISSING_INPUT_IMAGES = "missing_input_images"
    REFERENCE_IMAGE_TOO_LARGE = "reference_image_too_large"
    MODERATION_BLOCKED = "moderation_blocked"
    CONTENT_POLICY_VIOLATION = "content_policy_violation"
    SAFETY_VIOLATION = "safety_violation"

    # --- retriable: backoff 后再试 ---
    RATE_LIMIT_ERROR = "rate_limit_error"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    TIMEOUT = "timeout"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    SERVER_ERROR = "server_error"
    INTERNAL_ERROR = "internal_error"
    BAD_GATEWAY = "bad_gateway"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_ERROR_EVENT = "upstream_error_event"
    TEXT_STREAM_INTERRUPTED = "text_stream_interrupted"
    RESPONSE_FAILED = "response_failed"
    ALL_PROVIDERS_FAILED = "all_providers_failed"
    RESPONSES_FALLBACK_FAILED = "responses_fallback_failed"
    FALLBACK_LANES_FAILED = "fallback_lanes_failed"
    IMAGE_GENERATION_FAILED = "image_generation_failed"
    ALL_ACCOUNTS_FAILED = "all_accounts_failed"
    ACCOUNT_IMAGE_QUOTA_EXCEEDED = "account_image_quota_exceeded"
    LOCAL_QUEUE_FULL = "local_queue_full"
    DISK_FULL = "disk_full"
    DIRECT_IMAGE_REQUEST_FAILED = "direct_image_request_failed"

    # --- 条件 retriable: 取决于 has_partial / http_status ---
    NO_IMAGE_RETURNED = "no_image_returned"
    TOOL_CHOICE_DOWNGRADE = "tool_choice_downgrade"
    STREAM_INTERRUPTED = "stream_interrupted"
    SSE_CURL_FAILED = "sse_curl_failed"
    BAD_RESPONSE = "bad_response"
    STREAM_TOO_LARGE = "stream_too_large"

    # --- worker 内部 ---
    PROVIDER_EXHAUSTED = "provider_exhausted"
    NETWORK_TRANSIENT = "network_transient"
    NO_PROVIDERS = "no_providers"
    ALL_DIRECT_IMAGE_PROVIDERS_FAILED = "all_direct_image_providers_failed"
    REFERENCE_TIMEOUT = "reference_timeout"
    SHA_ECHO = "sha_echo"
    CANCELLED = "cancelled"

    # --- chat / completion 任务 ---
    RATE_LIMITED = "rate_limited"
    EMPTY_OUTPUT = "empty_output"
    NO_TEXT_RETURNED = "no_text_returned"

    # --- upstream 分类码（V1.0 新增，配合 classify_upstream_error） ---
    # 用于把上游 OpenAI-compatible error 体的 `error.type` / status code 收敛成稳定的内部码，
    # 用户层（apps/web errorMessages.ts）按这些码渲染中文文案，避免直接展示英文 message。
    # 命名规则：`upstream_*` 前缀；与已有的 INVALID_REQUEST_ERROR / RATE_LIMIT_ERROR /
    # SERVER_ERROR / AUTHENTICATION_ERROR / UPSTREAM_TIMEOUT / CANCELLED 共存，
    # 既保留旧 retry.py 行为，也给前端文案提供新粒度。
    UPSTREAM_INVALID_REQUEST = "upstream_invalid_request"  # 上游 400 schema 错误
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"  # 上游 429
    UPSTREAM_SERVER_ERROR = "upstream_server_error"  # 上游 5xx
    UPSTREAM_AUTH_ERROR = "upstream_auth_error"  # 上游 401/403
    UPSTREAM_CANCELLED = "upstream_cancelled"  # 用户主动取消（区分 worker 内部 CANCELLED）
    UPSTREAM_NETWORK_ERROR = "upstream_network_error"  # 连接失败 / DNS / TLS
    UPSTREAM_PAYLOAD_TOO_LARGE = "upstream_payload_too_large"  # 上游 413（罕见但要兜）
    UPSTREAM_CONTEXT_TOO_LONG = "upstream_context_too_long"  # context_length_exceeded
    UPSTREAM_UNKNOWN = "upstream_unknown"  # 兜底：识别不出 type / status


class CompletionStatus(StrEnum):
    QUEUED = "queued"
    STREAMING = "streaming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class CompletionStage(StrEnum):
    QUEUED = "queued"
    READING = "reading"
    THINKING = "thinking"
    STREAMING = "streaming"
    FINALIZING = "finalizing"


class GenerationAction(StrEnum):
    GENERATE = "generate"
    EDIT = "edit"


class ImageSource(StrEnum):
    GENERATED = "generated"
    UPLOADED = "uploaded"


class ImageVisibility(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"


class UserRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


# --- queues / streams / events (DESIGN §6.1 / §5.7) ---

QUEUE_GENERATIONS = "queue:generations"
QUEUE_COMPLETIONS = "queue:completions"

# SSE 回放 stream（每用户）：events:user:{uid}，MAXLEN≈24h
EVENTS_STREAM_PREFIX = "events:user:"

# PubSub 通道（实时推送）
def task_channel(task_id: str) -> str:
    return f"task:{task_id}"

def user_channel(user_id: str) -> str:
    return f"user:{user_id}"

def conv_channel(conv_id: str) -> str:
    return f"conv:{conv_id}"


# --- SSE 事件名（严格对齐 DESIGN §5.7） ---

EV_GEN_QUEUED = "generation.queued"
EV_GEN_STARTED = "generation.started"
EV_GEN_PROGRESS = "generation.progress"
EV_GEN_PARTIAL_IMAGE = "generation.partial_image"
EV_GEN_SUCCEEDED = "generation.succeeded"
EV_GEN_FAILED = "generation.failed"
EV_GEN_RETRYING = "generation.retrying"
# dual_race bonus image：winner 完成后 loser 也成功 → 把 bonus generation_id
# attach 到原 assistant message，前端 store push 进 message.generation_ids。
EV_GEN_ATTACHED = "generation.attached"
EV_COMP_QUEUED = "completion.queued"
EV_COMP_STARTED = "completion.started"
EV_COMP_PROGRESS = "completion.progress"
EV_COMP_DELTA = "completion.delta"
EV_COMP_THINKING_DELTA = "completion.thinking_delta"
EV_COMP_IMAGE = "completion.image"
EV_COMP_SUCCEEDED = "completion.succeeded"
EV_COMP_FAILED = "completion.failed"
EV_COMP_RESTARTED = "completion.restarted"
EV_MSG_INTENT_RESOLVED = "message.intent_resolved"
EV_CONV_MSG_APPENDED = "conv.message.appended"
EV_CONV_RENAMED = "conv.renamed"
EV_USER_NOTICE = "user.notice"


# --- upstream 硬约束（DESIGN §7 + upstream guide） ---

# size_mode=auto / 无 fixed_size 时 preset 回退使用的"默认预算"。
# 普通请求仍保守走 ~1.57M 以兼顾速度与成本；显式 4K 等大图请通过 fixed_size 透传。
PIXEL_BUDGET = 1_572_864
DEFAULT_PIXEL_BUDGET = PIXEL_BUDGET  # 语义别名，与 MAX_EXPLICIT_PIXELS 对照阅读

# 显式 fixed_size 的合法边界（对应 gpt-image-2 的真实能力；2026-04-23 已实测 3840x2160 可用）：
# - 最长边 ≤ MAX_EXPLICIT_SIDE
# - 宽高均为 EXPLICIT_ALIGN 的倍数
# - 总像素在 [MIN_EXPLICIT_PIXELS, MAX_EXPLICIT_PIXELS] 之间
# - 长宽比 ≤ MAX_EXPLICIT_ASPECT
# 注意：这组常量只在 size_mode=fixed 时生效，不参与 PIXEL_BUDGET 的 auto/preset 推导。
MAX_EXPLICIT_SIDE = 3840
MIN_EXPLICIT_PIXELS = 655_360
MAX_EXPLICIT_PIXELS = 8_294_400  # = 3840 * 2160
MAX_EXPLICIT_ASPECT = 3.0
EXPLICIT_ALIGN = 16

# 上游模型标识（/v1/images/* 系列接口使用）
UPSTREAM_MODEL = "gpt-image-2"
# Chat / vision completion 默认模型（/v1/responses 系列接口使用）。
DEFAULT_CHAT_MODEL = "gpt-5.5"
# 生图（/v1/responses + image_generation 工具）的 reasoning 主模型。
# 聊天走 5.5，但图像链路实测 5.4 更稳；默认同时带 reasoning.effort=high 与 service_tier=priority。
DEFAULT_IMAGE_RESPONSES_MODEL = "gpt-5.4"
DEFAULT_IMAGE_RESPONSES_MODEL_FAST = "gpt-5.4-mini"
# 重试 backoff（秒）— DESIGN §6.4
# 10/30s 对付网络瞬断；后续拉到分钟级是为了给 OpenAI
# rate_limit / codex-window 冷却时间，避免短 backoff 连打在 rate_limit 下白打。
# 最长的调用方目前会进行 5 次 attempt（4 次 retry），因此 4 个间隔都可能用上。
RETRY_BACKOFF_SECONDS = (10, 30, 60, 120)

# 多张图任务（n>=2）入队 stagger（秒）：第 N 张图延迟 N*STAGGER 秒入队。
# 实测：同 prompt 同账号同时打 ChatGPT codex 端会触发 OpenAI 内部 race，稳定一败一成。
# 错开 5s 让第二条流到达 OpenAI 时第一条已经分配好 image_generation slot，避免内部碰撞。
# Cap 30s 防止 N 张时最后一张等太久；i=0 不延迟保留 lowest-latency 首张。
IMAGE_MULTI_GEN_STAGGER_S = 5
IMAGE_MULTI_GEN_STAGGER_CAP_S = 30

# 上游 /v1/responses 必填 `instructions` 字段（system 指令）。
# 无 system_prompt 时回退到这两条默认文案。对应 completion / generation 两条路径。
DEFAULT_CHAT_INSTRUCTIONS = "You are a helpful assistant."
# 图像路径对齐 Codex CLI 标准模板：instructions 字段必须存在但内容为空串。
# 字面值改动会触发一次 prompt cache miss，之后稳定。
DEFAULT_IMAGE_INSTRUCTIONS = ""


# --- upstream error 分类（V1.0 新增） ---
# 上游 OpenAI-compatible error 体形如 {"error":{"message":"...","type":"...","code":"..."}}。
# 这里按 error.type 优先映射到内部 GenerationErrorCode；upstream.py 仍负责原样落库 error.code，
# 前端 errorMessages.ts 用这里收敛后的码取中文文案。

UPSTREAM_TYPE_TO_CODE: dict[str, GenerationErrorCode] = {
    # OpenAI 官方约定的 error.type 字面量
    "invalid_request_error": GenerationErrorCode.UPSTREAM_INVALID_REQUEST,
    "rate_limit_error": GenerationErrorCode.UPSTREAM_RATE_LIMITED,
    "server_error": GenerationErrorCode.UPSTREAM_SERVER_ERROR,
    "authentication_error": GenerationErrorCode.UPSTREAM_AUTH_ERROR,
    "permission_error": GenerationErrorCode.UPSTREAM_AUTH_ERROR,
    # 网关 / 反代偶尔会回这两种
    "timeout": GenerationErrorCode.UPSTREAM_TIMEOUT,
    "timeout_error": GenerationErrorCode.UPSTREAM_TIMEOUT,
    # 上游可能直接回 context_length_exceeded（部分 vendor 把它挂在 type 上）
    "context_length_exceeded": GenerationErrorCode.UPSTREAM_CONTEXT_TOO_LONG,
}

# 部分上游会把语义放在 error.code 而不是 error.type；这里同时给一份 code 级别的兜底映射。
UPSTREAM_CODE_TO_CODE: dict[str, GenerationErrorCode] = {
    "context_length_exceeded": GenerationErrorCode.UPSTREAM_CONTEXT_TOO_LONG,
    "rate_limit_exceeded": GenerationErrorCode.UPSTREAM_RATE_LIMITED,
    "insufficient_quota": GenerationErrorCode.UPSTREAM_RATE_LIMITED,
    "invalid_api_key": GenerationErrorCode.UPSTREAM_AUTH_ERROR,
    "request_canceled": GenerationErrorCode.UPSTREAM_CANCELLED,
    "request_cancelled": GenerationErrorCode.UPSTREAM_CANCELLED,
}


def classify_upstream_error(
    error_type: str | None,
    status_code: int | None,
    *,
    error_code: str | None = None,
) -> GenerationErrorCode:
    """把上游 error 体收敛到内部 GenerationErrorCode。

    优先级：
    1. error.type 命中 UPSTREAM_TYPE_TO_CODE
    2. error.code 命中 UPSTREAM_CODE_TO_CODE（部分 vendor 把语义放这里）
    3. status_code 兜底（401/403/413/429/4xx/5xx/超时类）
    4. 全部 miss → UPSTREAM_UNKNOWN

    注意：本函数只负责"分类"，不负责重试决策（重试看 retry.py）。
    新增上游 type 时优先扩 UPSTREAM_TYPE_TO_CODE，避免在调用点散开 if/elif。
    """
    if error_type:
        normalized_type = error_type.strip().lower()
        if normalized_type in UPSTREAM_TYPE_TO_CODE:
            return UPSTREAM_TYPE_TO_CODE[normalized_type]
    if error_code:
        normalized_code = error_code.strip().lower()
        if normalized_code in UPSTREAM_CODE_TO_CODE:
            return UPSTREAM_CODE_TO_CODE[normalized_code]
    if status_code is not None:
        # 401/403 → 认证；413 → 体积过大；429 → 限流；408/504 → 超时；5xx → 服务端
        if status_code in (401, 403):
            return GenerationErrorCode.UPSTREAM_AUTH_ERROR
        if status_code == 413:
            return GenerationErrorCode.UPSTREAM_PAYLOAD_TOO_LARGE
        if status_code == 429:
            return GenerationErrorCode.UPSTREAM_RATE_LIMITED
        if status_code in (408, 504):
            return GenerationErrorCode.UPSTREAM_TIMEOUT
        if 500 <= status_code < 600:
            return GenerationErrorCode.UPSTREAM_SERVER_ERROR
        if 400 <= status_code < 500:
            return GenerationErrorCode.UPSTREAM_INVALID_REQUEST
    return GenerationErrorCode.UPSTREAM_UNKNOWN
