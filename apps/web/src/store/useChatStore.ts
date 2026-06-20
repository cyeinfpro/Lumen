"use client";

// Lumen 会话 store（后端接入版）
// 对齐 DESIGN.md §13.1 / §22.9（消息 → 任务 → 图像 三层状态机）。
// 乐观插入用户 msg + pending 助手 msg → POST /conversations/:id/messages →
// 用返回的 user_message / assistant_message / generation_ids 校正 → SSE 流式更新。
//
// 本文件不直接调用上游网关；所有网络交互走 apiClient。

import { create } from "zustand";
import { z } from "zod";
import { uuid } from "@/lib/utils";
import { logWarn } from "@/lib/logger";
import { MAX_COMPOSER_ATTACHMENTS } from "@/lib/attachmentLimits";
import {
  MAX_PROMPT_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  appendPromptWithinLimit,
  clampPromptForRequest,
  isPromptTooLong,
} from "@/lib/promptLimits";
import {
  findInvalidImageMentionLabels,
  remapPromptImageMentions,
  serializePromptImageMentionsForRequest,
} from "@/lib/promptImageMentions";
import {
  MAX_UPLOAD_SOURCE_BYTES,
  maxUploadSourceMessage,
  setMaxUploadSourceBytes,
} from "@/lib/uploadLimits";
import type {
  AspectRatio,
  AssistantMessage,
  AttachmentImage,
  CompletionToolCall,
  Generation,
  GeneratedImage,
  ImageGenerationDiagnostics,
  ImageProviderAttempt,
  ImageParams,
  Intent,
  MaskState,
  MemoryWrite,
  Message,
  Quality,
  RenderQualityChoice,
  SizeMode,
  StructuredAttachment,
  UserMessage,
  UsedMemorySummary,
  RecommendedErrorAction,
} from "@/lib/types";
import {
  PRESET,
  defaultOutputCompression,
  nearestAspectRatio,
  qualityToFixedSize,
} from "@/lib/sizing";
import {
  ApiError,
  apiFetch,
  createConversation as apiCreateConversation,
  createSilentGeneration,
  getTask as apiGetTask,
  imageBinaryUrl,
  imageVariantUrl,
  listMessages as apiListMessages,
  listMyActiveTasks,
  postMessage as apiPostMessage,
  retryTask,
  uploadImage as apiUploadImage,
  type BackendCompletion,
  type BackendGeneration,
  type BackendImageMeta,
  type BackendMessage,
  type MessageListResponse,
  type PostMessageIn,
} from "@/lib/apiClient";
import { errorCodeToFullText, recommendedActionsForError } from "@/lib/errors";

type ComposerMode = "image" | "chat";

export type ReasoningEffort =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh";

interface ComposerState {
  text: string;
  attachments: AttachmentImage[];
  mode: ComposerMode;
  params: ImageParams;
  // 强制 intent（由斜杠命令 /ask /image 设置），非空时覆盖 mode 启发式
  forceIntent?: "chat" | "image";
  // 推理强度（chat / vision_qa 有效；"none" = 不思考；"minimal" 兼容旧消息）
  reasoningEffort?: ReasoningEffort;
  fast: boolean;
  webSearch: boolean;
  fileSearch: boolean;
  codeInterpreter: boolean;
  imageGeneration: boolean;
  // 局部修改 (inpaint) mask；仅 image_to_image 单参考图场景生效。
  // 任何会让"主参考图"漂移的操作（删第一张、加第二张、清空）都应顺手 clearMask。
  mask: MaskState | null;
}

interface ChatState {
  // 会话上下文
  currentUserId: string | null;
  currentConvId: string | null;
  setCurrentUser: (id: string | null) => void;
  setCurrentConv: (id: string | null) => void;
  applyRuntimeDefaults: (defaults: {
    fast?: boolean;
    upload_max_source_bytes?: number;
  }) => void;

  // 数据
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  messagesCursor: string | null;
  messagesHasMore: boolean;
  messagesLoading: boolean;
  messagesError: string | null;

  // Composer 层面向用户暴露的最近一次错误（如 sendMessage 失败、会话创建失败）。
  // 由桌面/移动 composer 渲染到红色提示条，避免错误被静默吞掉。
  composerError: string | null;
  setComposerError: (e: string | null) => void;

  composer: ComposerState;
  // —— composer actions ——
  setText: (text: string) => void;
  setMode: (mode: ComposerMode) => void;
  setForceIntent: (v: ComposerState["forceIntent"]) => void;
  setAspectRatio: (aspect: AspectRatio) => void;
  setSizeMode: (mode: SizeMode) => void;
  setFixedSize: (size: string | undefined) => void;
  setQuality: (q: Quality) => void;
  setRenderQuality: (q: RenderQualityChoice) => void;
  setImageCount: (count: number) => void;
  setReasoningEffort: (v: ReasoningEffort | undefined) => void;
  setFast: (v: boolean) => void;
  setWebSearch: (v: boolean) => void;
  setFileSearch: (v: boolean) => void;
  setCodeInterpreter: (v: boolean) => void;
  setImageGeneration: (v: boolean) => void;
  addAttachment: (att: AttachmentImage) => void;
  removeAttachment: (id: string) => void;
  moveAttachment: (id: string, targetId: string) => void;
  setMask: (mask: MaskState) => void;
  clearMask: () => void;
  clearComposer: () => void;
  promoteImageToReference: (imageId: string) => void;

  // —— async actions ——
  // 把本地 File 上传到后端 → 返回 AttachmentImage（含后端 image_id）
  uploadAttachment: (
    file: File,
    opts?: { signal?: AbortSignal },
  ) => Promise<AttachmentImage>;
  // 把 composer 当前状态作为一次发送：乐观插入 + POST → 校正
  sendMessage: (opts?: {
    intentOverride?: Exclude<Intent, "auto">;
    restoreComposerOnFailure?: boolean;
  }) => Promise<void>;
  // 切换 conv 后载入历史文本消息（不含历史 generations/images；继续新发消息可补全）
  loadHistoricalMessages: (convId: string, loadMore?: boolean) => Promise<void>;
  // 文本失败后 retry：复用历史 user msg
  retryAssistant: (assistantMsgId: string) => Promise<void>;
  // 生图失败后原位 retry：复用原 Generation 行，保留尺寸、比例和上游参数。
  retryGeneration: (generationId: string) => Promise<void>;
  // 意图纠偏重跑：用户切换 intent 后，让后端用新 intent 重新生成同一轮的助手消息（DESIGN §22.1）
  regenerateAssistant: (
    assistantMsgId: string,
    newIntent: Exclude<Intent, "auto">,
  ) => Promise<void>;
  // 放大图片：以原图为参考，原始 prompt + 放大指令，按最大预设尺寸重新生成
  upscaleImage: (imageId: string) => Promise<void>;
  // 重画：完全复用原 generation 参数，再生成一张新图
  rerollImage: (imageId: string) => Promise<void>;
  // 独立局部修改提交：从 Lightbox / 卡片等浏览态入口直接发起一次 image_to_image + mask 生成。
  // 不污染当前 composer 草稿（提交完成后会还原 text/attachments/mask）。
  submitInpaintTask: (input: {
    sourceImageId: string;
    sourceSrc: string;
    sourceWidth?: number;
    sourceHeight?: number;
    maskBlob: Blob;
    maskPreviewDataUrl: string;
    prompt: string;
  }) => Promise<void>;

  // —— 内部 / SSE ——
  appendUserMessage: (msg: UserMessage) => void;
  appendAssistantMessage: (msg: AssistantMessage) => void;
  upsertGeneration: (gen: Generation) => void;
  attachImageToGeneration: (generationId: string, img: GeneratedImage) => void;
  applySSEEvent: (eventName: string, data: unknown) => void;

  // —— 自愈：扫描在途任务，发现服务端已 terminal 但本地仍 running 时主动 refetch ——
  // 用途：刷新瞬间 worker 完成 → SSE 事件已发但浏览器还没订上 → 错过事件 → 永远卡 running
  pollInflightTasks: (opts?: {
    signal?: AbortSignal;
    generationIds?: string[];
    completionIds?: string[];
    maxChecks?: number;
  }) => Promise<void>;
  // —— 用户级中心任务列表：从 /tasks/mine/active 拉取当前用户全部进行中任务，
  //     一次性 merge 到 store.generations，让 GlobalTaskTray 显示**所有会话**的任务（即便
  //     当前会话没访问过也能看到）。SSE onOpen / 在线恢复时调用。
  hydrateActiveTasks: (opts?: { signal?: AbortSignal }) => Promise<void>;
  refreshCompletionText: (
    completionId: string,
    opts?: { signal?: AbortSignal },
  ) => Promise<void>;
  reset: () => void;
}

const DEFAULT_PARAMS: ImageParams = {
  aspect_ratio: "16:9",
  size_mode: "fixed",
  quality: "4k",
  render_quality: "high",
  count: 1,
};

const IMAGE_COUNT_MIN = 1;
const IMAGE_COUNT_MAX = 10;
const MESSAGE_PAGE_LIMIT = 50;
const BASE64_EVICTION_DELAY_MS = 60_000;
const BASE64_EVICTION_MIN_CHARS = 1024;
const COMPLETION_STREAM_FLUSH_MS = 64;
const COMPLETION_PENDING_DELTA_TTL_MS = 10_000;
const COMPLETION_PENDING_DELTA_MAX_ENTRIES = 1_000;
const CONVERSATION_INDEX_LIMIT = 5_000;
const CONVERSATION_HISTORY_CACHE_LIMIT = 32;
const CONVERSATION_HISTORY_CACHE_TTL_MS = 90_000;
const OPTIMISTIC_ALIAS_TTL_MS = 120_000;
const COMPLETION_MESSAGE_ID_TTL_MS = 60 * 60 * 1000;

function clampImageCount(count: number | undefined): number {
  if (typeof count !== "number" || !Number.isFinite(count))
    return IMAGE_COUNT_MIN;
  return Math.max(
    IMAGE_COUNT_MIN,
    Math.min(IMAGE_COUNT_MAX, Math.trunc(count)),
  );
}

function normalizeImageParams(params: ImageParams): ImageParams {
  const outputCompression =
    typeof params.output_compression === "number" &&
    Number.isFinite(params.output_compression)
      ? Math.max(0, Math.min(100, Math.trunc(params.output_compression)))
      : undefined;
  return {
    ...params,
    count: clampImageCount(params.count),
    ...(outputCompression === undefined
      ? { output_compression: undefined }
      : { output_compression: outputCompression }),
  };
}

function normalizeRenderQuality(
  value: ImageParams["render_quality"] | undefined,
): RenderQualityChoice {
  return value === "low" || value === "medium" || value === "high"
    ? value
    : "high";
}

function qualityFromFixedSize(
  sizeRequested: string,
  aspectRatio: AspectRatio,
): Quality | undefined {
  if (!sizeRequested.includes("x")) return undefined;
  const qualities: Quality[] = ["1k", "2k", "4k"];
  return qualities.find(
    (quality) =>
      qualityToFixedSize(quality, aspectRatio).fixed_size === sizeRequested,
  );
}

function generationIdsOfMessage(msg: AssistantMessage): string[] {
  if (msg.generation_ids && msg.generation_ids.length > 0)
    return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
}

function assistantHasGeneration(
  msg: AssistantMessage,
  generationId: string,
): boolean {
  return generationIdsOfMessage(msg).includes(generationId);
}

function aggregateGenerationStatus(
  generationIds: string[],
  generations: Record<string, Generation>,
  fallback: AssistantMessage["status"],
): AssistantMessage["status"] {
  const items = generationIds.map((id) => generations[id]).filter(Boolean);
  if (items.length === 0) return fallback;
  if (items.some((g) => g.status === "queued" || g.status === "running")) {
    return "pending";
  }
  if (items.every((g) => g.status === "canceled")) return "canceled";
  if (items.every((g) => g.status === "failed")) return "failed";
  if (items.some((g) => g.status === "succeeded")) return "succeeded";
  return fallback;
}

const DEFAULT_COMPOSER: ComposerState = {
  text: "",
  attachments: [],
  // Auto 模式已删；默认进 chat，写作/对话是最常用路径。
  mode: "chat",
  params: DEFAULT_PARAMS,
  forceIntent: undefined,
  reasoningEffort: "high",
  fast: true,
  webSearch: true,
  fileSearch: false,
  codeInterpreter: false,
  imageGeneration: false,
  mask: null,
};

let _runtimeFastDefault: boolean | null = null;
let _fastTouchedByUser = false;

type ChatDataSlice = Pick<
  ChatState,
  | "currentUserId"
  | "currentConvId"
  | "messages"
  | "generations"
  | "imagesById"
  | "messagesCursor"
  | "messagesHasMore"
  | "messagesLoading"
  | "messagesError"
  | "composerError"
  | "composer"
>;

function createInitialComposer(): ComposerState {
  const fast =
    _runtimeFastDefault == null ? DEFAULT_COMPOSER.fast : _runtimeFastDefault;
  return {
    ...DEFAULT_COMPOSER,
    fast,
    attachments: [],
    params: { ...DEFAULT_PARAMS },
    mask: null,
  };
}

function clonePlainValue<T>(value: T): T {
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(value);
    } catch {
      // Fall through to the manual plain-object clone below.
    }
  }
  if (Array.isArray(value)) {
    return value.map((item) => clonePlainValue(item)) as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, nested] of Object.entries(value)) {
      out[key] = clonePlainValue(nested);
    }
    return out as T;
  }
  return value;
}

function cloneComposerState(composer: ComposerState): ComposerState {
  const attachments = clonePlainValue(composer.attachments);
  const mask =
    composer.mask &&
    attachments.some((attachment) => attachment.id === composer.mask?.target_attachment_id)
      ? clonePlainValue(composer.mask)
      : null;
  return {
    ...composer,
    attachments,
    params: clonePlainValue(composer.params),
    mask,
  };
}

function isResetComposerDraft(composer: ComposerState): boolean {
  return (
    composer.text === "" &&
    composer.attachments.length === 0 &&
    composer.mask === null &&
    composer.forceIntent === undefined
  );
}

function createInitialChatData(): ChatDataSlice {
  return {
    currentUserId: null,
    currentConvId: null,
    messages: [],
    generations: {},
    imagesById: {},
    messagesCursor: null,
    messagesHasMore: false,
    messagesLoading: false,
    messagesError: null,
    composerError: null,
    composer: createInitialComposer(),
  };
}

function lastUserMessageId(messages: Message[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.role === "user") return msg.id;
  }
  return undefined;
}

function isKnownAbortMessage(message: string): boolean {
  switch (message.trim().toLowerCase()) {
    case "aborted":
    case "aborterror":
    case "the operation was aborted.":
    case "the user aborted a request.":
    case "signal is aborted without reason":
    case "this operation was aborted":
      return true;
    default:
      return false;
  }
}

function isAbortRequest(err: unknown, signal: AbortSignal): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (!(err instanceof ApiError) || err.code !== "network_error") return false;
  if (!signal.aborted) return false;

  const reason = signal.reason;
  if (
    reason instanceof DOMException &&
    reason.name === "AbortError" &&
    err.message === reason.message
  ) {
    return true;
  }

  return isKnownAbortMessage(err.message);
}

const isHistoryRequestAbort = isAbortRequest;

// —————————— 工具函数 ——————————

// 错误码 → 用户友好文案。未命中时返回 null，由调用方决定是否回退到原始 message。
//
// 实际映射委托给 `lib/errors.ts` 的 CODE_TITLE / CODE_DESC 表，避免两处文案漂移。
// 仅一个特例：`prompt_too_long` 走本地 PROMPT_TOO_LONG_MESSAGE 常量（含动态字符上限）。
export function errorCodeToMessage(code: string): string | null {
  if (code === "prompt_too_long") return PROMPT_TOO_LONG_MESSAGE;
  return errorCodeToFullText(code);
}

export function resolveIntent(
  mode: ComposerMode,
  hasAttachments: boolean,
  force?: ComposerState["forceIntent"],
): Exclude<Intent, "auto"> {
  // force（/ask /image 斜杠命令）优先覆盖 mode
  if (force === "chat") return hasAttachments ? "vision_qa" : "chat";
  if (force === "image")
    return hasAttachments ? "image_to_image" : "text_to_image";
  if (mode === "image")
    return hasAttachments ? "image_to_image" : "text_to_image";
  return hasAttachments ? "vision_qa" : "chat";
}

const ASPECT_RATIOS = new Set<AspectRatio>([
  "1:1",
  "16:9",
  "9:16",
  "21:9",
  "9:21",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
]);

function coerceAspectRatio(
  value: unknown,
  fallback: AspectRatio = DEFAULT_PARAMS.aspect_ratio,
): AspectRatio {
  return typeof value === "string" && ASPECT_RATIOS.has(value as AspectRatio)
    ? (value as AspectRatio)
    : fallback;
}

const GENERATION_STATUSES = new Set<Generation["status"]>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "canceled",
]);

function coerceGenerationStatus(
  value: unknown,
  fallback: Generation["status"],
): Generation["status"] {
  return typeof value === "string" &&
    GENERATION_STATUSES.has(value as Generation["status"])
    ? (value as Generation["status"])
    : fallback;
}

const GENERATION_STAGES = new Set<Generation["stage"]>([
  "queued",
  "understanding",
  "rendering",
  "finalizing",
]);

function coerceGenerationStage(
  value: unknown,
  fallback: Generation["stage"],
): Generation["stage"] {
  return typeof value === "string" &&
    GENERATION_STAGES.has(value as Generation["stage"])
    ? (value as Generation["stage"])
    : fallback;
}

const GENERATION_SUBSTAGES = new Set<NonNullable<Generation["substage"]>>([
  "waiting_queue",
  "waiting_provider",
  "preparing_refs",
  "upstream_started",
  "upstream_retrying",
  "postprocessing",
  "display_ready",
  "retryable",
  "terminal",
  "cancelled",
  "completed",
  "provider_selected",
  "stream_started",
  "partial_received",
  "final_received",
  "processing",
  "storing",
]);

function coerceGenerationSubstage(value: unknown): Generation["substage"] | undefined {
  return typeof value === "string" &&
    GENERATION_SUBSTAGES.has(value as NonNullable<Generation["substage"]>)
    ? (value as Generation["substage"])
    : undefined;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function optionalString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function optionalRecord(value: unknown): Record<string, unknown> | undefined {
  if (Array.isArray(value)) {
    logWarn("optional record payload dropped an array", {
      scope: "chat",
      code: "optional_record_array",
      extra: { length: value.length },
    });
    return undefined;
  }
  if (!value || typeof value !== "object") {
    return undefined;
  }
  return value as Record<string, unknown>;
}

const SsePayloadSchema = z.object({}).catchall(z.unknown());

function ssePayloadRecord(
  eventName: string,
  data: unknown,
): Record<string, unknown> | null {
  const parsed = SsePayloadSchema.safeParse(data);
  if (parsed.success) return parsed.data;
  logWarn("dropped SSE event with invalid payload", {
    scope: "chat-sse",
    extra: {
      event: eventName,
      payloadType: Array.isArray(data) ? "array" : typeof data,
      validation: "zod",
    },
  });
  return null;
}

function optionalRecordArray(value: unknown): Array<Record<string, unknown>> | undefined {
  if (!Array.isArray(value)) return undefined;
  const records = value.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
  return records.length > 0 ? records : undefined;
}

function recordString(
  record: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = record[key];
  return typeof value === "string" && value ? value : undefined;
}

function recordBoolean(
  record: Record<string, unknown>,
  key: string,
): boolean | undefined {
  const value = record[key];
  return typeof value === "boolean" ? value : undefined;
}

function recordNumber(
  record: Record<string, unknown>,
  key: string,
): number | undefined {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function recommendedActionsFromUnknown(
  value: unknown,
): RecommendedErrorAction[] | undefined {
  const items = optionalRecordArray(value);
  if (!items) return undefined;
  const actions: RecommendedErrorAction[] = [];
  for (const item of items) {
    const id = recordString(item, "id");
    const label = recordString(item, "label");
    if (!id || !label) continue;
    const action: RecommendedErrorAction = {
      id,
      label,
    };
    const kind = recordString(item, "kind");
    if (kind) action.kind = kind;
    const href = recordNullableString(item, "href");
    if (href) action.href = href;
    actions.push(action);
  }
  return actions.length > 0 ? actions : undefined;
}

function recordNullableString(
  record: Record<string, unknown>,
  key: string,
): string | null | undefined {
  const value = record[key];
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

const ATTACHMENT_ROLES = new Set<StructuredAttachment["role"]>([
  "reference",
  "subject",
  "product",
  "style",
  "edit_target",
  "ask_target",
  "background",
  "mask",
  "other",
]);

function defaultAttachmentRole(
  intent: Intent,
  index: number,
  hasMask: boolean,
): StructuredAttachment["role"] {
  if (intent === "vision_qa") return "ask_target";
  if (hasMask && index === 0) return "edit_target";
  return "reference";
}

function attachmentRole(value: unknown): StructuredAttachment["role"] | undefined {
  return typeof value === "string" &&
    ATTACHMENT_ROLES.has(value as StructuredAttachment["role"])
    ? (value as StructuredAttachment["role"])
    : undefined;
}

function structuredAttachmentsFromComposer(
  attachments: AttachmentImage[],
  intent: Intent,
  hasMask: boolean,
): StructuredAttachment[] {
  return attachments.map((attachment, index) => {
    const role =
      attachmentRole(attachment.role) ??
      defaultAttachmentRole(intent, index, hasMask);
    return {
      image_id: attachment.source_image_id ?? attachment.id,
      role,
      ...(attachment.label ? { label: attachment.label } : {}),
      ...(typeof attachment.weight === "number"
        ? { weight: attachment.weight }
        : {}),
    };
  });
}

function structuredAttachmentsFromUnknown(
  value: unknown,
): StructuredAttachment[] | undefined {
  const records = optionalRecordArray(value);
  if (!records) return undefined;
  const attachments = records
    .map((record) => {
      const imageId = recordString(record, "image_id");
      if (!imageId) return null;
      return {
        image_id: imageId,
        role: attachmentRole(record.role) ?? "reference",
        ...(recordString(record, "label")
          ? { label: recordString(record, "label") }
          : {}),
        ...(typeof record.weight === "number" ? { weight: record.weight } : {}),
      } satisfies StructuredAttachment;
    })
    .filter((item): item is StructuredAttachment => item !== null);
  return attachments.length > 0 ? attachments : undefined;
}

function parseSizeString(value: unknown): { width: number; height: number } {
  if (typeof value !== "string") return { width: 0, height: 0 };
  const match = value.match(/^(\d+)x(\d+)$/);
  if (!match) return { width: 0, height: 0 };
  return { width: Number(match[1]), height: Number(match[2]) };
}

function billingMetaFromPayload(
  payload: {
    is_dual_race_bonus?: unknown;
    billing_free?: unknown;
    billing_label?: unknown;
    billing_exempt_reason?: unknown;
  },
  metadata?: Record<string, unknown> | null,
): Pick<
  GeneratedImage,
  "is_dual_race_bonus" | "billing_free" | "billing_label" | "billing_exempt_reason"
> {
  const isDualRaceBonus =
    payload.is_dual_race_bonus === true ||
    metadata?.is_dual_race_bonus === true;
  const billingLabel =
    optionalString(payload.billing_label) ??
    optionalString(metadata?.billing_label);
  const billingFree =
    payload.billing_free === true ||
    metadata?.billing_free === true ||
    isDualRaceBonus ||
    billingLabel === "free";
  return {
    is_dual_race_bonus: isDualRaceBonus || undefined,
    billing_free: billingFree || undefined,
    billing_label: billingLabel ?? (billingFree ? "free" : undefined),
    billing_exempt_reason:
      optionalString(payload.billing_exempt_reason) ??
      optionalString(metadata?.billing_exempt_reason),
  };
}

type GenerationExplainabilityMeta = Pick<
  Generation,
  | "diagnostics"
  | "revised_prompt"
  | "requested_params"
  | "effective_params"
  | "provider_attempts"
  | "source"
  | "action_source"
  | "trace_id"
  | "attachment_roles"
  | "queue_lane"
  | "workflow_type"
  | "workflow_step_key"
  | "pixel_count"
  | "size_bucket"
  | "cost_class"
  | "queue_wait_ms"
>;

function generationExplainabilityFromBackend(
  generation: BackendGeneration,
): GenerationExplainabilityMeta {
  const diagnostics = optionalRecord(
    generation.diagnostics,
  ) as ImageGenerationDiagnostics | undefined;
  const providerAttempts =
    optionalRecordArray(generation.provider_attempts) ??
    optionalRecordArray(diagnostics?.provider_attempts);
  return {
    diagnostics: diagnostics ?? undefined,
    revised_prompt:
      generation.revised_prompt ?? diagnostics?.revised_prompt ?? undefined,
    requested_params:
      optionalRecord(generation.requested_params) ??
      optionalRecord(diagnostics?.requested_params) ??
      undefined,
    effective_params:
      optionalRecord(generation.effective_params) ??
      optionalRecord(diagnostics?.effective_params) ??
      undefined,
    provider_attempts: providerAttempts as ImageProviderAttempt[] | undefined,
    source: generation.source ?? undefined,
    action_source: generation.action_source ?? undefined,
    trace_id: generation.trace_id ?? optionalString(diagnostics?.trace_id),
    attachment_roles:
      structuredAttachmentsFromUnknown(generation.attachment_roles) ?? undefined,
    queue_lane: generation.queue_lane ?? undefined,
    workflow_type: generation.workflow_type ?? undefined,
    workflow_step_key: generation.workflow_step_key ?? undefined,
    pixel_count:
      typeof generation.pixel_count === "number" ? generation.pixel_count : undefined,
    size_bucket: generation.size_bucket ?? undefined,
    cost_class: generation.cost_class ?? undefined,
    queue_wait_ms:
      typeof generation.queue_wait_ms === "number"
        ? generation.queue_wait_ms
        : undefined,
  };
}

function generationTaskMetaFromBackend(
  generation: BackendGeneration,
): Pick<
  Generation,
  | "substage"
  | "queue_position"
  | "retrying"
  | "waiting_provider"
  | "cancelled"
  | "retryable"
  | "recommended_actions"
  | "source"
  | "conversation_id"
  | "project_id"
  | "thumb_url"
> {
  return {
    substage: coerceGenerationSubstage(generation.substage),
    queue_position:
      typeof generation.queue_position === "number" &&
      Number.isFinite(generation.queue_position)
        ? generation.queue_position
        : null,
    retrying: generation.retrying === true || undefined,
    waiting_provider: generation.waiting_provider === true || undefined,
    cancelled:
      generation.cancelled === true || generation.status === "canceled" || undefined,
    retryable: generation.retryable === true || undefined,
    recommended_actions:
      recommendedActionsFromUnknown(generation.recommended_actions) ??
      recommendedActionsForError(generation.error_code, {
        retryable: generation.retryable === true,
        status: generation.status,
      }),
    source: generation.source ?? undefined,
    conversation_id: generation.conversation_id ?? null,
    project_id: generation.project_id ?? null,
    thumb_url: generation.thumb_url ?? null,
  };
}

function generationExplainabilityFromPayload(
  payload: Record<string, unknown>,
): GenerationExplainabilityMeta {
  const diagnostics = optionalRecord(payload.diagnostics) as
    | ImageGenerationDiagnostics
    | undefined;
  const providerAttempts =
    optionalRecordArray(payload.provider_attempts) ??
    optionalRecordArray(diagnostics?.provider_attempts);
  return {
    diagnostics: diagnostics ?? undefined,
    revised_prompt: optionalString(payload.revised_prompt) ?? diagnostics?.revised_prompt,
    requested_params:
      optionalRecord(payload.requested_params) ??
      optionalRecord(diagnostics?.requested_params) ??
      undefined,
    effective_params:
      optionalRecord(payload.effective_params) ??
      optionalRecord(diagnostics?.effective_params) ??
      undefined,
    provider_attempts: providerAttempts as ImageProviderAttempt[] | undefined,
    source: optionalString(payload.source),
    action_source: optionalString(payload.action_source),
    trace_id: optionalString(payload.trace_id) ?? optionalString(diagnostics?.trace_id),
    attachment_roles: structuredAttachmentsFromUnknown(payload.attachment_roles),
    queue_lane: optionalString(payload.queue_lane),
    workflow_type: optionalString(payload.workflow_type),
    workflow_step_key: optionalString(payload.workflow_step_key),
    pixel_count:
      typeof payload.pixel_count === "number" ? payload.pixel_count : undefined,
    size_bucket: optionalString(payload.size_bucket),
    cost_class: optionalString(payload.cost_class),
    queue_wait_ms:
      typeof payload.queue_wait_ms === "number" ? payload.queue_wait_ms : undefined,
  };
}

function mergeExplainabilityIntoImage(
  image: GeneratedImage | undefined,
  meta: GenerationExplainabilityMeta,
): GeneratedImage | undefined {
  if (!image) return undefined;
  const hasMeta =
    meta.diagnostics ||
    meta.revised_prompt ||
    meta.requested_params ||
    meta.effective_params ||
    meta.provider_attempts ||
    meta.trace_id ||
    meta.action_source ||
    meta.attachment_roles;
  if (!hasMeta) return image;
  const metadata = { ...(image.metadata_jsonb ?? {}) };
  if (meta.diagnostics && metadata.generation_diagnostics == null) {
    metadata.generation_diagnostics = meta.diagnostics;
  }
  if (meta.revised_prompt && metadata.revised_prompt == null) {
    metadata.revised_prompt = meta.revised_prompt;
  }
  if (meta.requested_params && metadata.requested_params == null) {
    metadata.requested_params = meta.requested_params;
  }
  if (meta.effective_params && metadata.effective_params == null) {
    metadata.effective_params = meta.effective_params;
  }
  if (meta.provider_attempts && metadata.provider_attempts == null) {
    metadata.provider_attempts = meta.provider_attempts;
  }
  if (meta.trace_id && metadata.trace_id == null) {
    metadata.trace_id = meta.trace_id;
  }
  if (meta.action_source && metadata.action_source == null) {
    metadata.action_source = meta.action_source;
  }
  if (meta.attachment_roles && metadata.attachment_roles == null) {
    metadata.attachment_roles = meta.attachment_roles;
  }
  return {
    ...image,
    ...meta,
    metadata_jsonb: metadata,
  };
}

// 后端 BackendMessage → 前端 UserMessage / AssistantMessage 适配。
// 后端 content 是 dict：用户消息 {text, attachments:[{image_id}]}；助手初始 {}，succeeded 后可能带 {text}。
// created_at 是后端 datetime 的 ISO 8601 字符串 → 转 ms。字段缺失给出合理默认，避免 UI 崩。
function isoToMs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

function adaptBackendUserMessage(
  m: BackendMessage,
  attachments: AttachmentImage[],
  params: ImageParams,
  intent: Intent,
): UserMessage {
  const content = m.content ?? {};
  const text = typeof content.text === "string" ? content.text : "";
  const webSearch = content.web_search === true;
  const fileSearch = content.file_search === true;
  const codeInterpreter = content.code_interpreter === true;
  const imageGeneration = content.image_generation === true;
  return {
    id: m.id,
    role: "user",
    text,
    attachments,
    intent,
    image_params: params,
    web_search: webSearch,
    file_search: fileSearch,
    code_interpreter: codeInterpreter,
    image_generation: imageGeneration,
    created_at: isoToMs(m.created_at),
  };
}

// 助手意图合法性收敛
const ASSIST_INTENTS = new Set<Exclude<Intent, "auto">>([
  "chat",
  "vision_qa",
  "text_to_image",
  "image_to_image",
]);
function coerceAssistantIntent(
  v: unknown,
  fallback: Exclude<Intent, "auto">,
): Exclude<Intent, "auto"> {
  if (
    typeof v === "string" &&
    ASSIST_INTENTS.has(v as Exclude<Intent, "auto">)
  ) {
    return v as Exclude<Intent, "auto">;
  }
  return fallback;
}

const ASSIST_STATUSES = new Set<AssistantMessage["status"]>([
  "pending",
  "streaming",
  "succeeded",
  "failed",
  "canceled",
]);
function coerceAssistantStatus(v: unknown): AssistantMessage["status"] {
  if (
    typeof v === "string" &&
    ASSIST_STATUSES.has(v as AssistantMessage["status"])
  ) {
    return v as AssistantMessage["status"];
  }
  return "pending";
}

type NormalizedToolStatus = CompletionToolCall["status"];

const TOOL_STATUS_MAP: Record<string, NormalizedToolStatus> = {
  queued: "queued",
  pending: "queued",
  created: "queued",
  running: "running",
  in_progress: "running",
  searching: "running",
  interpreting: "running",
  generating: "running",
  completed: "succeeded",
  complete: "succeeded",
  succeeded: "succeeded",
  success: "succeeded",
  failed: "failed",
  error: "failed",
  incomplete: "failed",
  cancelled: "cancelled",
  canceled: "cancelled",
  timed_out: "timed_out",
  timeout: "timed_out",
};

function normalizeCompletionToolStatus(value: unknown): NormalizedToolStatus {
  if (typeof value !== "string") return "unknown";
  return TOOL_STATUS_MAP[value.trim().toLowerCase()] ?? "unknown";
}

function coerceCompletionToolCalls(value: unknown): CompletionToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): CompletionToolCall[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const id = typeof raw.id === "string" && raw.id ? raw.id : "";
    if (!id) return [];
    const status = normalizeCompletionToolStatus(raw.status);
    const type =
      typeof raw.type === "string" && raw.type ? raw.type : "tool";
    const label =
      typeof raw.label === "string" && raw.label ? raw.label : "调用工具";
    return [
      {
        id,
        type,
        status,
        label,
        name: typeof raw.name === "string" ? raw.name : undefined,
        title: typeof raw.title === "string" ? raw.title : undefined,
        error: typeof raw.error === "string" ? raw.error : undefined,
      },
    ];
  });
}

function coerceMemoryWrites(value: unknown): MemoryWrite[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): MemoryWrite[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const kind = typeof raw.kind === "string" ? raw.kind : "";
    if (
      kind !== "added" &&
      kind !== "updated" &&
      kind !== "merged" &&
      kind !== "superseded" &&
      kind !== "staged" &&
      kind !== "rejected_pii"
    ) {
      return [];
    }
    return [
      {
        id: typeof raw.id === "string" ? raw.id : null,
        kind,
        type:
          raw.type === "profile" ||
          raw.type === "preference" ||
          raw.type === "avoid" ||
          raw.type === "project"
            ? raw.type
            : null,
        content: typeof raw.content === "string" ? raw.content : "",
        source_excerpt:
          typeof raw.source_excerpt === "string" ? raw.source_excerpt : null,
        undo_token:
          typeof raw.undo_token === "string" ? raw.undo_token : null,
        scope_id: typeof raw.scope_id === "string" ? raw.scope_id : null,
        recommended_scope_id:
          typeof raw.recommended_scope_id === "string"
            ? raw.recommended_scope_id
            : null,
      },
    ];
  });
}

function coerceUsedMemorySummary(value: unknown): UsedMemorySummary[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): UsedMemorySummary[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    if (
      typeof raw.id !== "string" ||
      typeof raw.type !== "string" ||
      typeof raw.content !== "string"
    ) {
      return [];
    }
    return [{ id: raw.id, type: raw.type, content: raw.content }];
  });
}

function mergeCompletionToolCall(
  current: CompletionToolCall[] | undefined,
  incoming: CompletionToolCall,
): CompletionToolCall[] {
  const existing = current ?? [];
  const idx = existing.findIndex((item) => item.id === incoming.id);
  if (idx < 0) return [...existing, incoming];
  const next = existing.slice();
  next[idx] = {
    ...next[idx],
    ...incoming,
    name: incoming.name ?? next[idx].name,
    title: incoming.title ?? next[idx].title,
    error: incoming.error ?? next[idx].error,
  };
  return next;
}

function adaptBackendAssistantMessage(
  m: BackendMessage,
  parentUserId: string,
  fallbackIntent: Exclude<Intent, "auto">,
  generationIds: string[] | undefined,
  completionId: string | undefined,
): AssistantMessage {
  const content = m.content ?? {};
  const text = typeof content.text === "string" ? content.text : undefined;
  const thinking =
    typeof content.thinking === "string" ? content.thinking : undefined;
  const toolCalls = coerceCompletionToolCalls(content.tool_calls);
  const memoryWrites = coerceMemoryWrites(content.memory_writes);
  const usedMemorySummary = coerceUsedMemorySummary(content.used_memory_summary);
  const ids = generationIds ?? [];
  return {
    id: m.id,
    role: "assistant",
    parent_user_message_id: m.parent_message_id ?? parentUserId,
    intent_resolved: coerceAssistantIntent(m.intent, fallbackIntent),
    status: coerceAssistantStatus(m.status),
    generation_ids: ids.length > 0 ? ids : undefined,
    generation_id: ids[0],
    completion_id: completionId,
    text,
    thinking,
    tool_calls: toolCalls.length > 0 ? toolCalls : undefined,
    memory_writes: memoryWrites.length > 0 ? memoryWrites : undefined,
    used_memory_ids: stringArray(content.used_memory_ids),
    used_memory_summary:
      usedMemorySummary.length > 0 ? usedMemorySummary : undefined,
    confirmation_candidate_id:
      typeof content.confirmation_candidate_id === "string"
        ? content.confirmation_candidate_id
        : undefined,
    created_at: isoToMs(m.created_at),
  };
}

const MAX_DIM = 2048;
const MIN_COMPRESSED_DIM = 512;
const UPLOAD_TARGET_BYTES = 8 * 1024 * 1024;
const UPLOAD_HARD_MAX_BYTES = 50 * 1024 * 1024;
const UPLOAD_MIME = new Set(["image/png", "image/jpeg", "image/webp"]);
const ENCODE_QUALITIES = [0.9, 0.82, 0.74, 0.66, 0.58];

function imageFilenameForMime(name: string, mime: string): string {
  const ext =
    mime === "image/webp" ? "webp" : mime === "image/png" ? "png" : "jpg";
  const base = name.trim().replace(/\.[^.]*$/, "") || "image";
  return `${base}.${ext}`;
}

function imageEncodeError(): Error {
  const e = new Error("图像压缩失败：浏览器无法编码当前图片，请换张图试试");
  (e as Error & { code?: string }).code = "image_encode_failed";
  return e;
}

function uploadAbortError(signal?: AbortSignal): DOMException {
  const reason = signal?.reason;
  if (reason instanceof DOMException) return reason;
  return new DOMException("上传已取消", "AbortError");
}

function throwIfUploadAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw uploadAbortError(signal);
}

function loadBrowserImage(
  file: File,
  signal?: AbortSignal,
): Promise<{ img: HTMLImageElement; url: string }> {
  throwIfUploadAborted(signal);
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    let settled = false;

    const cleanup = () => {
      signal?.removeEventListener("abort", onAbort);
    };
    const resolveOnce = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ img, url });
    };
    const rejectOnce = (err: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      URL.revokeObjectURL(url);
      reject(err);
    };
    const onAbort = () => rejectOnce(uploadAbortError(signal));

    signal?.addEventListener("abort", onAbort, { once: true });
    img.onload = resolveOnce;
    img.onerror = () => rejectOnce(new Error("读取图片失败"));
    img.src = url;
  });
}

function drawImageToCanvas(
  img: HTMLImageElement,
  maxSide: number,
  background: string | null,
): HTMLCanvasElement {
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  const scale = Math.min(1, maxSide / Math.max(w, h));
  const nw = Math.max(1, Math.round(w * scale));
  const nh = Math.max(1, Math.round(h * scale));
  const canvas = document.createElement("canvas");
  canvas.width = nw;
  canvas.height = nh;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw imageEncodeError();
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, nw, nh);
  }
  ctx.drawImage(img, 0, 0, nw, nh);
  return canvas;
}

function canvasToBlob(
  canvas: HTMLCanvasElement,
  mime: "image/webp" | "image/jpeg",
  quality: number,
): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), mime, quality);
  });
}

async function encodeImageForUpload(
  img: HTMLImageElement,
  maxSide: number,
  signal?: AbortSignal,
): Promise<{ blob: Blob; mime: "image/webp" | "image/jpeg" }> {
  let best: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null = null;

  for (const mime of ["image/webp", "image/jpeg"] as const) {
    throwIfUploadAborted(signal);
    const canvas = drawImageToCanvas(
      img,
      maxSide,
      mime === "image/jpeg" ? "#fff" : null,
    );
    for (const quality of ENCODE_QUALITIES) {
      throwIfUploadAborted(signal);
      const blob = await canvasToBlob(canvas, mime, quality);
      throwIfUploadAborted(signal);
      if (!blob || blob.type !== mime) continue;
      if (!best || blob.size < best.blob.size) best = { blob, mime };
      if (blob.size <= UPLOAD_TARGET_BYTES) return { blob, mime };
    }
  }

  if (!best) throw imageEncodeError();
  return best;
}

function nextCompressedSide(currentSide: number, encodedBytes: number): number {
  const ratio = Math.sqrt(UPLOAD_TARGET_BYTES / Math.max(encodedBytes, 1));
  const shrink = Math.max(0.65, Math.min(0.9, ratio * 0.92));
  return Math.max(MIN_COMPRESSED_DIM, Math.floor(currentSide * shrink));
}

async function compressToMaxDim(
  file: File,
  signal?: AbortSignal,
): Promise<File> {
  if (file.size > MAX_UPLOAD_SOURCE_BYTES) {
    throw new Error(maxUploadSourceMessage());
  }

  const { img, url } = await loadBrowserImage(file, signal);
  try {
    throwIfUploadAborted(signal);
    const { naturalWidth: w, naturalHeight: h } = img;
    if (!w || !h) throw new Error("读取图片失败");

    const supportedOriginal = UPLOAD_MIME.has(file.type);
    const oversizedDimensions = Math.max(w, h) > MAX_DIM;
    const oversizedBytes = file.size > UPLOAD_TARGET_BYTES;
    const shouldNormalizeOriginal = file.type === "image/jpeg";
    if (
      supportedOriginal &&
      !shouldNormalizeOriginal &&
      !oversizedDimensions &&
      !oversizedBytes
    ) {
      return file;
    }

    let maxSide = Math.min(MAX_DIM, Math.max(w, h));
    let encoded: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null =
      null;
    for (let attempt = 0; attempt < 6; attempt++) {
      encoded = await encodeImageForUpload(img, maxSide, signal);
      if (
        encoded.blob.size <= UPLOAD_TARGET_BYTES ||
        maxSide <= MIN_COMPRESSED_DIM
      ) {
        break;
      }
      maxSide = nextCompressedSide(maxSide, encoded.blob.size);
    }

    if (!encoded) throw imageEncodeError();
    throwIfUploadAborted(signal);
    if (encoded.blob.size > UPLOAD_HARD_MAX_BYTES) {
      throw new Error("图片文件过大，请换一张较小的图片或先压缩后再上传");
    }

    return new File(
      [encoded.blob],
      imageFilenameForMime(file.name, encoded.mime),
      {
        type: encoded.mime,
        lastModified: file.lastModified,
      },
    );
  } finally {
    URL.revokeObjectURL(url);
  }
}

function errorToMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message || err.code;
  if (err instanceof Error) return err.message;
  return "未知错误";
}

function compareMessages(a: Message, b: Message): number {
  if (a.created_at !== b.created_at) return a.created_at - b.created_at;
  return a.id.localeCompare(b.id);
}

function mergeMessagesById(
  existing: Message[],
  incoming: Message[],
): Message[] {
  const byId = new Map<string, Message>();
  for (const msg of existing) byId.set(msg.id, msg);
  for (const msg of incoming) byId.set(msg.id, msg);
  return Array.from(byId.values()).sort(compareMessages);
}

function latestPersistedMessageId(messages: Message[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const id = messages[i]?.id;
    if (id && !id.startsWith("opt-")) return id;
  }
  return undefined;
}

function assistantStatusFromCompletion(
  status: string | null | undefined,
  fallback: AssistantMessage["status"],
): AssistantMessage["status"] {
  switch (status) {
    case "queued":
      return "pending";
    case "running":
      return "streaming";
    case "succeeded":
    case "failed":
    case "canceled":
      return status;
    default:
      return fallback;
  }
}

function applyCompletionSnapshot(
  messages: Message[],
  completionId: string,
  fresh: BackendCompletion,
  now?: number,
): Message[] {
  let changed = false;
  const snapshotNow = now ?? Date.now();
  const nextMessages = messages.map((m) => {
    if (
      m.role !== "assistant" ||
      (m as AssistantMessage).completion_id !== completionId
    ) {
      return m;
    }
    const next = { ...m } as AssistantMessage;
    const status = assistantStatusFromCompletion(fresh.status, next.status);
    if (next.status !== status) {
      next.status = status;
      changed = true;
    }
    if (status === "streaming" && !next.stream_started_at) {
      next.stream_started_at = snapshotNow;
      changed = true;
    }
    const currentText = next.text ?? "";
    const freshIsTerminal =
      fresh.status === "succeeded" ||
      fresh.status === "failed" ||
      fresh.status === "canceled";
    if (
      typeof fresh.text === "string" &&
      fresh.text !== next.text &&
      (freshIsTerminal || fresh.text.length >= currentText.length)
    ) {
      next.text = fresh.text;
      next.last_delta_at = snapshotNow;
      changed = true;
    }
    if (fresh.status === "failed" && !next.text) {
      const msg = fresh.error_message ?? "文本生成失败";
      const code = fresh.error_code ?? "completion_failed";
      next.text = `${msg}（${code}）`;
      next.last_delta_at = snapshotNow;
      changed = true;
    }
    return next;
  });
  return changed ? nextMessages : messages;
}

function isEvictableDataUrl(src: string | undefined): boolean {
  return (
    typeof src === "string" &&
    src.startsWith("data:") &&
    src.length >= BASE64_EVICTION_MIN_CHARS
  );
}

function releaseImageBase64(img: GeneratedImage): GeneratedImage {
  if (!isEvictableDataUrl(img.data_url)) return img;
  return {
    ...img,
    data_url:
      img.display_url ??
      img.preview_url ??
      img.thumb_url ??
      imageBinaryUrl(img.id),
  };
}

// —————————— store 本体 ——————————

// 每个会话独立的历史消息请求 abort 控制器；Map<convId, AbortController> 避免并发请求互相 abort。
const _historyAborts = new Map<string, AbortController>();
const _sendMessageAborts = new Set<AbortController>();
let _base64EvictionTimer: ReturnType<typeof setTimeout> | null = null;
let _completionStreamTimer: ReturnType<typeof setTimeout> | null = null;
const _messageConvIds = new Map<string, string>();
const _generationConvIds = new Map<string, string>();
const _imageConvIds = new Map<string, string>();
const _completionMessageIds = new Map<
  string,
  { messageId: string; expiresAt: number }
>();
const _generationIdAliases = new Map<
  string,
  { optimisticId: string; expiresAt: number }
>();
const _completionMessageAliases = new Map<
  string,
  { optimisticMessageId: string; expiresAt: number }
>();

interface ConversationHistoryCacheEntry {
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  messagesCursor: string | null;
  messagesHasMore: boolean;
  updatedAt: number;
}

const _conversationHistoryCache = new Map<
  string,
  ConversationHistoryCacheEntry
>();

interface PendingCompletionStreamPatch {
  msgId?: string;
  compId?: string;
  text: string;
  thinking: string;
  firstQueuedAt: number;
  updatedAt: number;
}

const _completionStreamPatches = new Map<
  string,
  PendingCompletionStreamPatch
>();
const _pendingDeltasByCompletionId = new Map<
  string,
  PendingCompletionStreamPatch
>();

function abortHistoryRequest(convId: string): void {
  const ctl = _historyAborts.get(convId);
  if (!ctl) return;
  ctl.abort();
  _historyAborts.delete(convId);
}

function abortAllHistoryRequests(): void {
  for (const ctl of _historyAborts.values()) {
    ctl.abort();
  }
  _historyAborts.clear();
}

function trackSendRequest(ctl: AbortController): () => void {
  _sendMessageAborts.add(ctl);
  return () => {
    _sendMessageAborts.delete(ctl);
  };
}

function abortAllSendRequests(): void {
  for (const ctl of _sendMessageAborts) {
    ctl.abort();
  }
  _sendMessageAborts.clear();
}

function pruneMapToLimit<K, V>(
  map: Map<K, V>,
  limit = CONVERSATION_INDEX_LIMIT,
): void {
  while (map.size > limit) {
    const first = map.keys().next();
    if (first.done) break;
    map.delete(first.value);
  }
}

function setBounded<K, V>(
  map: Map<K, V>,
  key: K,
  value: V,
  limit = CONVERSATION_INDEX_LIMIT,
): void {
  if (map.has(key)) map.delete(key);
  map.set(key, value);
  pruneMapToLimit(map, limit);
}

function cloneConversationHistoryCacheEntry(
  entry: ConversationHistoryCacheEntry,
): ConversationHistoryCacheEntry {
  return {
    messages: clonePlainValue(entry.messages),
    generations: clonePlainValue(entry.generations),
    imagesById: clonePlainValue(entry.imagesById),
    messagesCursor: entry.messagesCursor,
    messagesHasMore: entry.messagesHasMore,
    updatedAt: entry.updatedAt,
  };
}

function pickConversationImages(
  messages: Message[],
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
): Record<string, GeneratedImage> {
  const imageIds = new Set<string>();
  for (const msg of messages) {
    if (msg.role === "user") {
      for (const att of msg.attachments) {
        if (att.id) imageIds.add(att.id);
        if (att.source_image_id) imageIds.add(att.source_image_id);
      }
    } else {
      for (const genId of generationIdsOfMessage(msg)) {
        const imageId = generations[genId]?.image?.id;
        if (imageId) imageIds.add(imageId);
      }
    }
  }

  const picked: Record<string, GeneratedImage> = {};
  for (const imageId of imageIds) {
    const img = imagesById[imageId];
    if (img) picked[imageId] = img;
  }
  return picked;
}

function makeConversationHistoryCacheEntry(
  messages: Message[],
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
  messagesCursor: string | null,
  messagesHasMore: boolean,
): ConversationHistoryCacheEntry {
  const generationIds = new Set<string>();
  for (const msg of messages) {
    if (msg.role !== "assistant") continue;
    for (const genId of generationIdsOfMessage(msg)) generationIds.add(genId);
  }

  const pickedGenerations: Record<string, Generation> = {};
  for (const genId of generationIds) {
    const gen = generations[genId];
    if (gen) pickedGenerations[genId] = gen;
  }

  return {
    messages: clonePlainValue(messages),
    generations: clonePlainValue(pickedGenerations),
    imagesById: clonePlainValue(
      pickConversationImages(messages, pickedGenerations, imagesById),
    ),
    messagesCursor,
    messagesHasMore,
    updatedAt: Date.now(),
  };
}

function rememberConversationHistoryCache(
  convId: string,
  entry: ConversationHistoryCacheEntry,
): void {
  setBounded(
    _conversationHistoryCache,
    convId,
    cloneConversationHistoryCacheEntry(entry),
    CONVERSATION_HISTORY_CACHE_LIMIT,
  );
}

function readConversationHistoryCache(
  convId: string | null | undefined,
  now = Date.now(),
): ConversationHistoryCacheEntry | null {
  if (!convId) return null;
  const entry = _conversationHistoryCache.get(convId);
  if (!entry) return null;
  if (now - entry.updatedAt > CONVERSATION_HISTORY_CACHE_TTL_MS) {
    _conversationHistoryCache.delete(convId);
    return null;
  }
  rememberConversationHistoryCache(convId, entry);
  return cloneConversationHistoryCacheEntry(entry);
}

function invalidateConversationHistoryCache(
  convId: string | null | undefined,
): void {
  if (convId) _conversationHistoryCache.delete(convId);
}

function pruneAliases(now?: number): void {
  const effectiveNow = now ?? Date.now();
  for (const [id, alias] of _generationIdAliases) {
    if (alias.expiresAt <= effectiveNow) _generationIdAliases.delete(id);
  }
  for (const [id, alias] of _completionMessageAliases) {
    if (alias.expiresAt <= effectiveNow) _completionMessageAliases.delete(id);
  }
  for (const [id, item] of _completionMessageIds) {
    if (item.expiresAt <= effectiveNow) _completionMessageIds.delete(id);
  }
  pruneMapToLimit(_generationIdAliases);
  pruneMapToLimit(_completionMessageAliases);
  pruneMapToLimit(_completionMessageIds);
}

function rememberGenerationAlias(
  realId: string,
  optimisticId: string,
  now?: number,
): void {
  const effectiveNow = now ?? Date.now();
  setBounded(_generationIdAliases, realId, {
    optimisticId,
    expiresAt: effectiveNow + OPTIMISTIC_ALIAS_TTL_MS,
  });
}

function rememberCompletionAlias(
  realId: string,
  optimisticMessageId: string,
  now?: number,
): void {
  const effectiveNow = now ?? Date.now();
  setBounded(_completionMessageAliases, realId, {
    optimisticMessageId,
    expiresAt: effectiveNow + OPTIMISTIC_ALIAS_TTL_MS,
  });
}

function rememberCompletionMessage(
  completionId: string | undefined | null,
  messageId: string | undefined | null,
  now?: number,
): void {
  if (!completionId || !messageId) return;
  const effectiveNow = now ?? Date.now();
  setBounded(_completionMessageIds, completionId, {
    messageId,
    expiresAt: effectiveNow + COMPLETION_MESSAGE_ID_TTL_MS,
  });
  if (_pendingDeltasByCompletionId.has(completionId)) {
    setTimeout(flushCompletionStreamPatches, 0);
  }
}

function generationLookupId(id: string, now?: number): string {
  pruneAliases(now);
  return _generationIdAliases.get(id)?.optimisticId ?? id;
}

function completionMessageLookupId(
  id: string | undefined,
  now?: number,
): string | undefined {
  if (!id) return undefined;
  pruneAliases(now);
  return (
    _completionMessageAliases.get(id)?.optimisticMessageId ??
    _completionMessageIds.get(id)?.messageId
  );
}

function completionStreamPatchKey(
  msgId: string | undefined,
  compId: string | undefined,
): string | null {
  if (compId) return `comp:${compId}`;
  if (msgId) return `msg:${msgId}`;
  return null;
}

function createCompletionStreamPatch(
  msgId: string | undefined,
  compId: string | undefined,
  now: number,
): PendingCompletionStreamPatch {
  return {
    msgId,
    compId,
    text: "",
    thinking: "",
    firstQueuedAt: now,
    updatedAt: now,
  };
}

function mergeCompletionStreamPatch(
  target: PendingCompletionStreamPatch,
  source: PendingCompletionStreamPatch,
): void {
  target.msgId = target.msgId ?? source.msgId;
  target.compId = target.compId ?? source.compId;
  target.text += source.text;
  target.thinking += source.thinking;
  target.updatedAt = Math.max(target.updatedAt, source.updatedAt);
}

function pruneExpiredPendingCompletionDeltas(now = Date.now()): void {
  for (const [completionId, patch] of _pendingDeltasByCompletionId) {
    if (now - patch.firstQueuedAt <= COMPLETION_PENDING_DELTA_TTL_MS) continue;
    _pendingDeltasByCompletionId.delete(completionId);
    logWarn("dropped stale completion delta without assistant message", {
      scope: "chat-sse",
      extra: { completionId },
    });
  }
}

function flushCompletionStreamPatches(): void {
  if (_completionStreamTimer) {
    clearTimeout(_completionStreamTimer);
    _completionStreamTimer = null;
  }
  if (
    _completionStreamPatches.size === 0 &&
    _pendingDeltasByCompletionId.size === 0
  ) {
    return;
  }

  const patchEntries = Array.from(_completionStreamPatches.entries());
  _completionStreamPatches.clear();
  const now = Date.now();
  pruneExpiredPendingCompletionDeltas(now);
  const appliedPatchKeys = new Set<string>();
  const appliedPendingCompletionIds = new Set<string>();

  useChatStore.setState((s) => {
    let changed = false;
    const messages = s.messages.map((m) => {
      if (m.role !== "assistant") return m;
      let next: AssistantMessage | null = null;
      const patches: PendingCompletionStreamPatch[] = [];

      for (const [key, patch] of patchEntries) {
        const matches =
          (patch.msgId != null && m.id === patch.msgId) ||
          (patch.compId != null && m.completion_id === patch.compId);
        if (!matches) continue;
        appliedPatchKeys.add(key);
        patches.push(patch);
      }

      if (m.completion_id) {
        const pending = _pendingDeltasByCompletionId.get(m.completion_id);
        if (pending) {
          appliedPendingCompletionIds.add(m.completion_id);
          patches.push(pending);
        }
      }

      for (const patch of patches) {
        next ??= { ...m };
        const isTerminal =
          next.status === "succeeded" ||
          next.status === "failed" ||
          next.status === "canceled";
        if (patch.text) {
          const text = next.text ?? "";
          if (!isTerminal || !text.endsWith(patch.text)) {
            next.text = text + patch.text;
          }
        }
        if (patch.thinking) {
          const thinking = next.thinking ?? "";
          if (!isTerminal || !thinking.endsWith(patch.thinking)) {
            next.thinking = thinking + patch.thinking;
          }
        }
      }

      if (!next) return m;
      const isTerminal =
        next.status === "succeeded" ||
        next.status === "failed" ||
        next.status === "canceled";
      if (!isTerminal) {
        next.status = "streaming";
        next.stream_started_at ??= now;
      }
      next.last_delta_at = now;
      changed = true;
      return next;
    });

    return changed ? { messages } : s;
  });

  for (const completionId of appliedPendingCompletionIds) {
    _pendingDeltasByCompletionId.delete(completionId);
  }

  for (const [key, patch] of patchEntries) {
    if (appliedPatchKeys.has(key) || !patch.compId) continue;
    const existing = _pendingDeltasByCompletionId.get(patch.compId);
    if (existing) {
      mergeCompletionStreamPatch(existing, patch);
      continue;
    }
    setBounded(
      _pendingDeltasByCompletionId,
      patch.compId,
      { ...patch },
      COMPLETION_PENDING_DELTA_MAX_ENTRIES,
    );
  }
}

function queueCompletionStreamPatch(
  msgId: string | undefined,
  compId: string | undefined,
  kind: "text" | "thinking",
  value: string,
): void {
  if (!value) return;
  const key = completionStreamPatchKey(msgId, compId);
  if (!key) return;
  const now = Date.now();
  const current =
    _completionStreamPatches.get(key) ??
    createCompletionStreamPatch(msgId, compId, now);
  current.msgId = current.msgId ?? msgId;
  current.compId = current.compId ?? compId;
  current.updatedAt = now;
  if (kind === "text") current.text += value;
  else current.thinking += value;
  _completionStreamPatches.set(key, current);

  if (_completionStreamTimer) return;
  _completionStreamTimer = setTimeout(() => {
    _completionStreamTimer = null;
    flushCompletionStreamPatches();
  }, COMPLETION_STREAM_FLUSH_MS);
}

function clearCompletionStreamBuffer(): void {
  if (_completionStreamTimer) {
    clearTimeout(_completionStreamTimer);
    _completionStreamTimer = null;
  }
  _completionStreamPatches.clear();
  _pendingDeltasByCompletionId.clear();
}

function clearConversationIndexes(): void {
  _messageConvIds.clear();
  _generationConvIds.clear();
  _imageConvIds.clear();
  _completionMessageIds.clear();
  _generationIdAliases.clear();
  _completionMessageAliases.clear();
  _conversationHistoryCache.clear();
}

function rememberMessagesForConversation(
  convId: string,
  messages: Message[],
): void {
  for (const msg of messages) {
    setBounded(_messageConvIds, msg.id, convId);
  }
}

function rememberGenerationForConversation(
  convId: string,
  gen: Generation,
): void {
  setBounded(_generationConvIds, gen.id, convId);
  if (gen.image) setBounded(_imageConvIds, gen.image.id, convId);
}

function completionToolGenerationId(completionId: string): string {
  return `completion-tool-${completionId}`;
}

function generationConversationId(
  state: Pick<ChatState, "currentConvId" | "messages">,
  gen: Generation,
): string | null {
  return (
    _generationConvIds.get(gen.id) ??
    _messageConvIds.get(gen.message_id) ??
    (state.messages.some((m) => m.id === gen.message_id)
      ? state.currentConvId
      : null)
  );
}

function scheduleBase64Eviction(): void {
  if (typeof window === "undefined") return;
  if (_base64EvictionTimer) {
    clearTimeout(_base64EvictionTimer);
    _base64EvictionTimer = null;
  }
  _base64EvictionTimer = setTimeout(() => {
    _base64EvictionTimer = null;
    useChatStore.setState((s) => {
      const currentConvId = s.currentConvId;
      let changedGens = false;
      let changedImages = false;
      const nextGenerations: Record<string, Generation> = {};
      const nextImagesById: Record<string, GeneratedImage> = {};

      for (const [id, gen] of Object.entries(s.generations)) {
        const convId = generationConversationId(s, gen);
        const keep =
          gen.status === "queued" ||
          gen.status === "running" ||
          (currentConvId != null && convId === currentConvId);
        if (!keep && gen.image) {
          const released = releaseImageBase64(gen.image);
          nextGenerations[id] =
            released === gen.image ? gen : { ...gen, image: released };
          changedGens = changedGens || released !== gen.image;
        } else {
          nextGenerations[id] = gen;
        }
      }

      for (const [id, img] of Object.entries(s.imagesById)) {
        const genId = img.from_generation_id;
        const gen = genId ? s.generations[genId] : undefined;
        const convId =
          _imageConvIds.get(id) ??
          (gen ? generationConversationId(s, gen) : null);
        const keep =
          (gen != null &&
            (gen.status === "queued" || gen.status === "running")) ||
          (currentConvId != null && convId === currentConvId);
        if (!keep) {
          const released = releaseImageBase64(img);
          nextImagesById[id] = released;
          changedImages = changedImages || released !== img;
        } else {
          nextImagesById[id] = img;
        }
      }

      if (!changedGens && !changedImages) return s;
      return {
        generations: changedGens ? nextGenerations : s.generations,
        imagesById: changedImages ? nextImagesById : s.imagesById,
      };
    });
  }, BASE64_EVICTION_DELAY_MS);
}

function buildMessageListState(
  convId: string,
  resp: MessageListResponse,
  existingGens: Record<string, Generation>,
  existingImgs: Record<string, GeneratedImage>,
): {
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
} {
  const items = resp.items ?? [];

  const newImagesById: Record<string, GeneratedImage> = { ...existingImgs };
  if (resp.images) {
    for (const i of resp.images) {
      const sizeActual = `${i.width}x${i.height}`;
      const meta = i as BackendImageMeta & {
        metadata_jsonb?: Record<string, unknown> | null;
      };
      const billingMeta = billingMetaFromPayload(i, meta.metadata_jsonb);
      const metaCompletionId =
        typeof meta.metadata_jsonb?.completion_id === "string"
          ? meta.metadata_jsonb.completion_id
          : "";
      const fromGenId =
        i.owner_generation_id ??
        (metaCompletionId ? completionToolGenerationId(metaCompletionId) : "");
      const existingImage = newImagesById[i.id];
      if (existingImage) {
        newImagesById[i.id] = {
          ...existingImage,
          data_url: isEvictableDataUrl(existingImage.data_url)
            ? existingImage.data_url
            : existingImage.data_url || i.url,
          display_url: existingImage.display_url ?? i.display_url ?? undefined,
          preview_url: existingImage.preview_url ?? i.preview_url ?? undefined,
          thumb_url: existingImage.thumb_url ?? i.thumb_url ?? undefined,
          mime: existingImage.mime ?? i.mime ?? undefined,
          filename:
            existingImage.filename ??
            (typeof meta.metadata_jsonb?.suggested_filename === "string"
              ? meta.metadata_jsonb.suggested_filename
              : undefined),
          metadata_jsonb: existingImage.metadata_jsonb ?? meta.metadata_jsonb ?? null,
          is_dual_race_bonus:
            existingImage.is_dual_race_bonus ?? billingMeta.is_dual_race_bonus,
          billing_free: existingImage.billing_free ?? billingMeta.billing_free,
          billing_label: existingImage.billing_label ?? billingMeta.billing_label,
          billing_exempt_reason:
            existingImage.billing_exempt_reason ??
            billingMeta.billing_exempt_reason,
        };
      } else {
        newImagesById[i.id] = {
          id: i.id,
          data_url: i.url,
          mime: i.mime ?? undefined,
          display_url: i.display_url ?? undefined,
          preview_url: i.preview_url ?? undefined,
          thumb_url: i.thumb_url ?? undefined,
          width: i.width,
          height: i.height,
          parent_image_id: i.parent_image_id,
          from_generation_id: fromGenId,
          size_requested: sizeActual,
          size_actual: sizeActual,
          filename:
            typeof meta.metadata_jsonb?.suggested_filename === "string"
              ? meta.metadata_jsonb.suggested_filename
              : undefined,
          metadata_jsonb: meta.metadata_jsonb ?? null,
          ...billingMeta,
        };
      }
      setBounded(_imageConvIds, i.id, convId);
    }
  }

  const newGenerations: Record<string, Generation> = { ...existingGens };
  const genIdsByMsgId: Record<string, string[]> = {};
  if (resp.generations) {
    for (const g of resp.generations) {
      const linkedImage = (resp.images ?? []).find(
        (i) => i.owner_generation_id === g.id,
      );
      const builtImage: GeneratedImage | undefined = linkedImage
        ? newImagesById[linkedImage.id]
        : undefined;
      const existing = existingGens[g.id];
      const generationBillingMeta = billingMetaFromPayload(g);
      const generationExplainability = generationExplainabilityFromBackend(g);
      const imageWithExplainability = mergeExplainabilityIntoImage(
        builtImage ?? existing?.image,
        generationExplainability,
      );
      if (imageWithExplainability) {
        newImagesById[imageWithExplainability.id] = imageWithExplainability;
      }
      const merged: Generation = {
        id: g.id,
        message_id: g.message_id,
        parent_generation_id:
          g.parent_generation_id ?? existing?.parent_generation_id ?? null,
        action: g.action === "edit" ? "edit" : "generate",
        prompt:
          typeof g.prompt === "string" ? g.prompt : (existing?.prompt ?? ""),
        size_requested:
          typeof g.size_requested === "string"
            ? g.size_requested
            : (existing?.size_requested ?? "auto"),
        aspect_ratio: coerceAspectRatio(g.aspect_ratio, existing?.aspect_ratio),
        input_image_ids: stringArray(g.input_image_ids),
        primary_input_image_id:
          typeof g.primary_input_image_id === "string"
            ? g.primary_input_image_id
            : null,
        status: coerceGenerationStatus(
          g.status,
          existing?.status ?? "succeeded",
        ),
        stage: coerceGenerationStage(
          g.progress_stage,
          existing?.stage ?? "finalizing",
        ),
        image: imageWithExplainability ?? existing?.image,
        error_code: g.error_code ?? undefined,
        error_message: g.error_message ?? undefined,
        attempt:
          typeof g.attempt === "number" && Number.isFinite(g.attempt)
            ? g.attempt
            : (existing?.attempt ?? 0),
        started_at: isoToMs(g.started_at),
        finished_at: g.finished_at ? isoToMs(g.finished_at) : undefined,
        ...generationExplainability,
        ...generationBillingMeta,
      };
      const useExisting =
        existing &&
        (existing.status === "queued" || existing.status === "running") &&
        merged.status !== "succeeded" &&
        merged.status !== "failed";
      const nextGen = useExisting ? existing : merged;
      newGenerations[g.id] = nextGen;
      rememberGenerationForConversation(convId, nextGen);
      if (!genIdsByMsgId[g.message_id]?.includes(g.id)) {
        genIdsByMsgId[g.message_id] = [
          ...(genIdsByMsgId[g.message_id] ?? []),
          g.id,
        ];
      }
    }
  }
  const compIdByMsgId: Record<string, string> = {};
  if (resp.completions) {
    for (const c of resp.completions) {
      compIdByMsgId[c.message_id] = c.id;
      rememberCompletionMessage(c.id, c.message_id);
    }
  }

  for (const m of items) {
    if (m.role !== "assistant") continue;
    const completionId = compIdByMsgId[m.id];
    if (!completionId) continue;
    const content = m.content ?? {};
    const contentImages = Array.isArray(content.images) ? content.images : [];
    const linkedIds = contentImages
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const imageId = (item as { image_id?: unknown }).image_id;
        return typeof imageId === "string" && imageId ? imageId : null;
      })
      .filter((imageId): imageId is string => Boolean(imageId));
    if (linkedIds.length === 0) continue;
    const genId = completionToolGenerationId(completionId);
    const existing = existingGens[genId];
    const firstImage = linkedIds
      .map((imageId) => newImagesById[imageId])
      .find(Boolean);
    const gen: Generation = {
      id: genId,
      message_id: m.id,
      action: "generate",
      prompt: typeof content.text === "string" ? content.text : "",
      size_requested: firstImage?.size_requested ?? "auto",
      aspect_ratio: "1:1",
      input_image_ids: [],
      primary_input_image_id: null,
      status: "succeeded",
      stage: "finalizing",
      image: firstImage ?? existing?.image,
      attempt: existing?.attempt ?? 0,
      started_at: existing?.started_at ?? isoToMs(m.created_at),
      finished_at: existing?.finished_at ?? isoToMs(m.created_at),
    };
    newGenerations[genId] = gen;
    rememberGenerationForConversation(convId, gen);
    genIdsByMsgId[m.id] = [
      ...(genIdsByMsgId[m.id] ?? []),
      genId,
    ];
    for (const imageId of linkedIds) {
      setBounded(_imageConvIds, imageId, convId);
    }
  }

  for (const g of Object.values(existingGens)) {
    if (g.message_id && !genIdsByMsgId[g.message_id]?.includes(g.id)) {
      genIdsByMsgId[g.message_id] = [
        ...(genIdsByMsgId[g.message_id] ?? []),
        g.id,
      ];
    }
  }

  const messages: Message[] = [];
  for (const m of items) {
    const content = m.content ?? {};
    if (m.role === "user") {
      const text = typeof content.text === "string" ? content.text : "";
      const attList = Array.isArray(content.attachments)
        ? content.attachments
        : [];
      const attachments: AttachmentImage[] = attList
        .flatMap((a) => {
          if (!a || typeof a !== "object") return [];
          const imageId = (a as { image_id?: unknown }).image_id;
          return typeof imageId === "string" && imageId ? [imageId] : [];
        })
        .map((imageId) => ({
          id: imageId,
          kind: "upload",
          data_url: imageBinaryUrl(imageId),
          mime: "",
        }));
      const userMsg: UserMessage = {
        id: m.id,
        role: "user",
        text,
        attachments,
        intent: "auto",
        image_params: DEFAULT_PARAMS,
        web_search: content.web_search === true,
        file_search: content.file_search === true,
        code_interpreter: content.code_interpreter === true,
        image_generation: content.image_generation === true,
        created_at: isoToMs(m.created_at),
      };
      messages.push(userMsg);
    } else if (m.role === "assistant") {
      const text = typeof content.text === "string" ? content.text : undefined;
      const thinking =
        typeof content.thinking === "string" ? content.thinking : undefined;
      const toolCalls = coerceCompletionToolCalls(content.tool_calls);
      const memoryWrites = coerceMemoryWrites(content.memory_writes);
      const usedMemorySummary = coerceUsedMemorySummary(content.used_memory_summary);
      const asstMsg: AssistantMessage = {
        id: m.id,
        role: "assistant",
        parent_user_message_id: m.parent_message_id ?? "",
        intent_resolved: coerceAssistantIntent(m.intent, "chat"),
        status: aggregateGenerationStatus(
          genIdsByMsgId[m.id] ?? [],
          newGenerations,
          coerceAssistantStatus(m.status),
        ),
        generation_ids: genIdsByMsgId[m.id],
        generation_id: genIdsByMsgId[m.id]?.[0],
        completion_id: compIdByMsgId[m.id],
        text,
        thinking,
        tool_calls: toolCalls.length > 0 ? toolCalls : undefined,
        memory_writes: memoryWrites.length > 0 ? memoryWrites : undefined,
        used_memory_ids: stringArray(content.used_memory_ids),
        used_memory_summary:
          usedMemorySummary.length > 0 ? usedMemorySummary : undefined,
        confirmation_candidate_id:
          typeof content.confirmation_candidate_id === "string"
            ? content.confirmation_candidate_id
            : undefined,
        created_at: isoToMs(m.created_at),
      };
      messages.push(asstMsg);
    }
  }

  rememberMessagesForConversation(convId, messages);

  return {
    messages,
    generations: newGenerations,
    imagesById: newImagesById,
  };
}

function createChatStore() {
  return create<ChatState>((set, get) => ({
    ...createInitialChatData(),
    setCurrentUser: (id) => set({ currentUserId: id }),
    applyRuntimeDefaults: (defaults) => {
      setMaxUploadSourceBytes(defaults.upload_max_source_bytes);
      if (typeof defaults.fast !== "boolean") return;
      const fastDefault = defaults.fast;
      _runtimeFastDefault = fastDefault;
      set((s) => {
        if (_fastTouchedByUser || s.composer.fast === fastDefault) return s;
        return { composer: { ...s.composer, fast: fastDefault } };
      });
    },
    // 切换会话：只清 messages（UI 级），保留 generations / imagesById（全局任务池）。
    // 原因：切走时若后台还有 generation 在跑，它的 Generation 记录不能丢，否则：
    //   - GlobalTaskTray 不再显示该任务
    //   - SSE 事件到达时 s.generations[id] 不存在，更新会 no-op
    //   - 切回会话时渲染不出进度/结果卡片
    // loadHistoricalMessages 会反查 generations pool 把 generation_id 重新绑回 assistant msg。
    setCurrentConv: (id) => {
      const previousConvId = get().currentConvId;
      if (previousConvId === id) return;
      const cached = readConversationHistoryCache(id);

      // 会话切换时取消旧历史拉取和发送请求，避免旧响应回写到新会话。
      abortAllHistoryRequests();
      abortAllSendRequests();

      set((s) => ({
        currentConvId: id,
        messages: cached?.messages ?? [],
        generations: cached
          ? { ...s.generations, ...cached.generations }
          : s.generations,
        imagesById: cached ? { ...s.imagesById, ...cached.imagesById } : s.imagesById,
        messagesCursor: cached?.messagesCursor ?? null,
        messagesHasMore: cached?.messagesHasMore ?? false,
        messagesLoading: false,
        messagesError: null,
      }));
      clearCompletionStreamBuffer();
      scheduleBase64Eviction();
    },

    setComposerError: (e) => set({ composerError: e }),

    setText: (text) => set((s) => ({ composer: { ...s.composer, text } })),
    setMode: (mode) =>
      set((s) => ({
        composer: { ...s.composer, mode, forceIntent: undefined },
      })),
    setForceIntent: (v) =>
      set((s) => ({ composer: { ...s.composer, forceIntent: v } })),
    setAspectRatio: (aspect) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, aspect_ratio: aspect },
        },
      })),
    setSizeMode: (mode) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, size_mode: mode },
        },
      })),
    setFixedSize: (size) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, fixed_size: size },
        },
      })),
    setQuality: (q) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, quality: q },
        },
      })),
    setRenderQuality: (q) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, render_quality: q },
        },
      })),
    setImageCount: (count) =>
      set((s) => ({
        composer: {
          ...s.composer,
          params: { ...s.composer.params, count: clampImageCount(count) },
        },
      })),
    setReasoningEffort: (v) =>
      set((s) => ({ composer: { ...s.composer, reasoningEffort: v } })),
    setFast: (v) => {
      _fastTouchedByUser = true;
      set((s) => ({ composer: { ...s.composer, fast: v } }));
    },
    setWebSearch: (v) =>
      set((s) => ({ composer: { ...s.composer, webSearch: v } })),
    setFileSearch: (v) =>
      set((s) => ({ composer: { ...s.composer, fileSearch: v } })),
    setCodeInterpreter: (v) =>
      set((s) => ({ composer: { ...s.composer, codeInterpreter: v } })),
    setImageGeneration: (v) =>
      set((s) => ({ composer: { ...s.composer, imageGeneration: v } })),
    addAttachment: (att) =>
      set((s) => {
        const previousAttachments = s.composer.attachments;
        if (previousAttachments.some((a) => a.id === att.id)) return s;
        if (previousAttachments.length >= MAX_COMPOSER_ATTACHMENTS) {
          return {
            composerError: `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`,
          };
        }
        const nextAttachments = [...previousAttachments, att];
        // 局部修改 mask 仅允许单张参考图：第二张加入时自动清除已设置的 mask。
        const nextMask =
          previousAttachments.length === 0 ? s.composer.mask : null;
        return {
          composer: {
            ...s.composer,
            text: remapPromptImageMentions(
              s.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
            mask: nextMask,
          },
        };
      }),
    removeAttachment: (id) =>
      set((s) => {
        const previousAttachments = s.composer.attachments;
        const nextAttachments = previousAttachments.filter((a) => a.id !== id);
        if (nextAttachments.length === previousAttachments.length) return s;
        // mask 跟着第一张参考图：若被删的是 mask 绑定的那张，或剩余张数为 0，
        // 都要把 mask 清掉，避免脏 mask_image_id 跟着发出去。
        const nextMask =
          s.composer.mask &&
          nextAttachments.some(
            (a) => a.id === s.composer.mask!.target_attachment_id,
          )
            ? s.composer.mask
            : null;
        return {
          composer: {
            ...s.composer,
            text: remapPromptImageMentions(
              s.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
            mask: nextMask,
          },
        };
      }),
    moveAttachment: (id, targetId) =>
      set((s) => {
        if (id === targetId) return s;
        const previousAttachments = s.composer.attachments;
        const from = previousAttachments.findIndex((a) => a.id === id);
        const to = previousAttachments.findIndex((a) => a.id === targetId);
        if (from < 0 || to < 0 || from === to) return s;
        const nextAttachments = [...previousAttachments];
        const [moved] = nextAttachments.splice(from, 1);
        if (!moved) return s;
        nextAttachments.splice(to, 0, moved);
        return {
          composer: {
            ...s.composer,
            text: remapPromptImageMentions(
              s.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
          },
        };
      }),
    setMask: (mask) =>
      set((s) => ({ composer: { ...s.composer, mask } })),
    clearMask: () =>
      set((s) => ({ composer: { ...s.composer, mask: null } })),
    clearComposer: () =>
      set((s) => ({
        composer: {
          ...createInitialComposer(),
          mode: s.composer.mode,
          params: s.composer.params,
          // 推理强度、Fast 和工具开关是用户偏好，保留跨次发送
          reasoningEffort: s.composer.reasoningEffort,
          fast: s.composer.fast,
          webSearch: s.composer.webSearch,
          fileSearch: s.composer.fileSearch,
          codeInterpreter: s.composer.codeInterpreter,
          imageGeneration: s.composer.imageGeneration,
        },
      })),
    promoteImageToReference: (imageId) => {
      const img = get().imagesById[imageId];
      if (!img) return;
      const att: AttachmentImage = {
        id: uuid(),
        kind: "generated",
        data_url: img.data_url,
        mime: "image/png",
        width: img.width,
        height: img.height,
        source_image_id: img.id,
      };
      set((s) => ({
        composerError:
          s.composer.attachments.length >= MAX_COMPOSER_ATTACHMENTS
            ? `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`
            : s.composerError,
        composer: {
          ...s.composer,
          text:
            s.composer.attachments.length >= MAX_COMPOSER_ATTACHMENTS
              ? s.composer.text
              : remapPromptImageMentions(
                  s.composer.text,
                  s.composer.attachments,
                  [att, ...s.composer.attachments],
                ),
          attachments:
            s.composer.attachments.length >= MAX_COMPOSER_ATTACHMENTS
              ? s.composer.attachments
              : [att, ...s.composer.attachments],
          mode: "image",
          // 新参考图被插到首位 → 旧的 mask 已不再绑定主参考图，必须清掉
          mask:
            s.composer.attachments.length >= MAX_COMPOSER_ATTACHMENTS
              ? s.composer.mask
              : null,
        },
      }));
    },

    // —— 上传附件：先上后端拿到 image_id，再作为 attachment 挂到 composer ——
    async uploadAttachment(file, opts = {}) {
      const compressed = await compressToMaxDim(file, opts.signal);
      const uploaded = await apiUploadImage(compressed, {
        signal: opts.signal,
      });
      const att: AttachmentImage = {
        id: uploaded.id, // 使用后端返回的 image_id（后续 postMessage 直接用）
        kind: "upload",
        data_url: uploaded.url?.startsWith("data:")
          ? uploaded.url
          : imageBinaryUrl(uploaded.id),
        mime: uploaded.mime ?? compressed.type ?? file.type,
        width: uploaded.width,
        height: uploaded.height,
      };
      return att;
    },

    // —— 载入指定会话的历史文本消息 ——
    // 后端 /conversations/{id}/messages 只返回 MessageOut（不含 generations/images）。
    // 但 store.generations 是全局任务池（切会话不清），所以可以反查 message_id 把
    // 进行中 / 已完成的 Generation 绑回 assistant msg，让切回会话仍能看到进度卡/结果图。
    async loadHistoricalMessages(convId, loadMore = false) {
      const snapshot = get();
      if (snapshot.messagesLoading) return;
      if (loadMore && !snapshot.messagesHasMore) return;
      if (loadMore && snapshot.currentConvId !== convId) return;

      // 抢占式 abort：上次该会话的首屏历史请求若未完成，直接放弃，避免竞态写入。
      if (!loadMore) {
        abortHistoryRequest(convId);
      }
      const ctl = new AbortController();
      _historyAborts.set(convId, ctl);
      const cursor = loadMore ? snapshot.messagesCursor : null;
      const cached = loadMore ? null : readConversationHistoryCache(convId);
      set((s) => ({
        messagesLoading: true,
        messagesError: null,
        ...(loadMore
          ? {}
          : cached && s.currentConvId === convId
            ? {
                messages: cached.messages,
                generations: { ...s.generations, ...cached.generations },
                imagesById: { ...s.imagesById, ...cached.imagesById },
                messagesCursor: cached.messagesCursor,
                messagesHasMore: cached.messagesHasMore,
              }
            : { messagesCursor: null, messagesHasMore: false }),
      }));

      try {
        let resp = await apiListMessages(convId, {
          limit: MESSAGE_PAGE_LIMIT,
          cursor: cursor ?? undefined,
          signal: ctl.signal,
          include: ["tasks"],
        });

        // 兼容当前后端可能把首个 next_cursor 返回为 message_id 的情况：
        // cursor 请求若没有带来新消息，则改用 since=message_id 拉后续页。
        if (loadMore && cursor) {
          const existingIds = new Set(get().messages.map((m) => m.id));
          const newCount = (resp.items ?? []).filter(
            (m) => !existingIds.has(m.id),
          ).length;
          if (newCount === 0 && resp.next_cursor === cursor) {
            resp = await apiListMessages(convId, {
              limit: MESSAGE_PAGE_LIMIT,
              since: cursor,
              signal: ctl.signal,
              include: ["tasks"],
            });
          }
        }

        const built = buildMessageListState(
          convId,
          resp,
          get().generations,
          get().imagesById,
        );
        let cacheEntry: ConversationHistoryCacheEntry | null = null;
        set((s) => {
          if (s.currentConvId !== convId) {
            logWarn(
              "loadHistoricalMessages: conv switched mid-flight; dropping stale result",
              {
                scope: "chat",
                extra: { requested: convId, current: s.currentConvId },
              },
            );
            return s;
          }
          const nextMessages = loadMore
            ? mergeMessagesById(s.messages, built.messages)
            : built.messages;
          const previousCount = s.messages.length;
          const nextCursor = resp.next_cursor ?? null;
          const gotNewMessages =
            nextMessages.length > previousCount || !loadMore;
          const messagesHasMore = Boolean(nextCursor) && gotNewMessages;
          cacheEntry = makeConversationHistoryCacheEntry(
            nextMessages,
            built.generations,
            built.imagesById,
            nextCursor,
            messagesHasMore,
          );
          return {
            messages: nextMessages,
            generations: built.generations,
            imagesById: built.imagesById,
            messagesCursor: nextCursor,
            messagesHasMore,
            messagesLoading: false,
            messagesError: null,
          };
        });
        if (cacheEntry) rememberConversationHistoryCache(convId, cacheEntry);
      } catch (err) {
        // AbortError：被新切换覆盖，静默放弃即可。
        if (isHistoryRequestAbort(err, ctl.signal)) {
          if (_historyAborts.get(convId) === ctl)
            set({ messagesLoading: false });
          return;
        }
        const message = errorToMessage(err);
        set((s) =>
          s.currentConvId === convId
            ? { messagesLoading: false, messagesError: message }
            : s,
        );
        throw err;
      } finally {
        if (_historyAborts.get(convId) === ctl) _historyAborts.delete(convId);
      }
    },

    async sendMessage(opts) {
      const ctl = new AbortController();
      const untrackSendRequest = trackSendRequest(ctl);
      let phase: "create" | "post" = "post";
      let state = get();
      // 每次发送前清掉上一条错误提示；后续任一步失败时再设置
      set({ composerError: null });

      const initialComposer = state.composer;
      if (
        !initialComposer.text.trim() &&
        initialComposer.attachments.length === 0
      ) {
        untrackSendRequest();
        return;
      }
      if (isPromptTooLong(initialComposer.text.trim())) {
        set({ composerError: PROMPT_TOO_LONG_MESSAGE });
        untrackSendRequest();
        return;
      }

      // 没有活动会话：自动创建一个（Onboarding / 空态首条发送的自然路径）
      let convId = state.currentConvId;
      if (!convId) {
        try {
          phase = "create";
          const created = await apiCreateConversation(
            {},
            { signal: ctl.signal },
          );
          if (ctl.signal.aborted) {
            untrackSendRequest();
            return;
          }
          if (get().currentConvId && get().currentConvId !== created.id) {
            untrackSendRequest();
            return;
          }
          set({ currentConvId: created.id });
          convId = created.id;
        } catch (err) {
          untrackSendRequest();
          if (isAbortRequest(err, ctl.signal)) return;
          const msg =
            err instanceof ApiError
              ? `新建会话失败：${err.message}（${err.code}）`
              : err instanceof Error
                ? `新建会话失败：${err.message}`
                : "新建会话失败";
          logWarn("auto-create conversation failed", {
            scope: "chat",
            code: err instanceof ApiError ? err.code : undefined,
            extra: { msg: err instanceof Error ? err.message : "unknown" },
          });
          set({ composerError: msg });
          return;
        }
      }
      phase = "post";
      state = get();
      const composerToSend = cloneComposerState(state.composer);
      const {
        text: rawText,
        attachments,
        mode,
        params: rawParams,
        forceIntent,
        reasoningEffort,
        fast,
        webSearch,
        fileSearch,
        codeInterpreter,
        imageGeneration,
        mask,
      } = composerToSend;
      const params = normalizeImageParams(rawParams);
      const text = rawText.trim();
      const invalidMentionLabels = findInvalidImageMentionLabels(
        text,
        attachments.length,
      );
      if (invalidMentionLabels.length > 0) {
        const preview = invalidMentionLabels.slice(0, 3).join("、");
        const suffix = invalidMentionLabels.length > 3 ? " 等" : "";
        set({
          composerError: `参考图引用无效：${preview}${suffix}，请先移除或补齐附件`,
        });
        untrackSendRequest();
        return;
      }
      const requestText = serializePromptImageMentionsForRequest(
        text,
        attachments,
      );
      if (!text && attachments.length === 0) {
        untrackSendRequest();
        return;
      }
      if (isPromptTooLong(requestText)) {
        set({ composerError: PROMPT_TOO_LONG_MESSAGE });
        untrackSendRequest();
        return;
      }
      if (ctl.signal.aborted || state.currentConvId !== convId) {
        untrackSendRequest();
        return;
      }

      const intent =
        opts?.intentOverride ??
        resolveIntent(mode, attachments.length > 0, forceIntent);
      const isImage = intent === "text_to_image" || intent === "image_to_image";
      // 局部修改 mask：只有 image_to_image + 单张参考图 + mask.target 仍指向第一张时才发。
      // 任意一条不满足都视为脏状态，直接吞掉避免发出无效字段。
      const maskImageId =
        intent === "image_to_image" &&
        attachments.length === 1 &&
        mask &&
        mask.target_attachment_id === attachments[0]?.id
          ? mask.image_id
          : undefined;
      const structuredAttachments = structuredAttachmentsFromComposer(
        attachments,
        intent,
        Boolean(maskImageId),
      );
      const attachmentImageIds = structuredAttachments.map((a) => a.image_id);
      const actionSource = isImage
        ? maskImageId
          ? "composer.inpaint"
          : intent === "image_to_image"
            ? "composer.image_to_image"
            : "composer.text_to_image"
        : intent === "vision_qa"
          ? "composer.vision_qa"
          : "composer.chat";
      const traceId = uuid();

      // 1) 乐观插入 user msg + pending assistant msg
      const optimisticUserId = `opt-user-${uuid()}`;
      const optimisticAssistantId = `opt-asst-${uuid()}`;
      const optimisticGenIds = isImage ? [`opt-gen-${uuid()}`] : [];
      const optimisticGenId = optimisticGenIds[0];
      const now = Date.now();

      const userMsg: UserMessage = {
        id: optimisticUserId,
        role: "user",
        text,
        attachments,
        intent,
        image_params: params,
        web_search: !isImage ? webSearch : undefined,
        file_search: !isImage ? fileSearch : undefined,
        code_interpreter: !isImage ? codeInterpreter : undefined,
        image_generation: !isImage ? imageGeneration : undefined,
        created_at: now,
      };
      const assistantMsg: AssistantMessage = {
        id: optimisticAssistantId,
        role: "assistant",
        parent_user_message_id: optimisticUserId,
        intent_resolved: intent,
        status: "pending",
        generation_ids:
          optimisticGenIds.length > 0 ? optimisticGenIds : undefined,
        generation_id: optimisticGenId,
        created_at: now,
      };

      setBounded(_messageConvIds, optimisticUserId, convId);
      setBounded(_messageConvIds, optimisticAssistantId, convId);
      for (const id of optimisticGenIds) {
        setBounded(_generationConvIds, id, convId);
      }
      invalidateConversationHistoryCache(convId);

      let optimisticGens: Record<string, Generation> = {};
      if (isImage && optimisticGenIds.length > 0) {
        const action: Generation["action"] =
          intent === "image_to_image" ? "edit" : "generate";
        const sizeRequested =
          params.size_mode === "fixed" && params.fixed_size
            ? params.fixed_size
            : "auto";
        optimisticGens = Object.fromEntries(
          optimisticGenIds.map((id) => [
            id,
            {
              id,
              message_id: optimisticAssistantId,
              action,
              prompt: requestText,
              size_requested: sizeRequested,
              aspect_ratio: params.aspect_ratio,
              input_image_ids: attachmentImageIds,
              primary_input_image_id: attachmentImageIds[0] ?? null,
              status: "queued" as const,
              stage: "queued" as const,
              source: "composer",
              action_source: actionSource,
              trace_id: traceId,
              attachment_roles: structuredAttachments,
              attempt: 0,
              started_at: 0,
            } satisfies Generation,
          ]),
        );
      }

      set((s) => ({
        messages: [...s.messages, userMsg, assistantMsg],
        generations:
          Object.keys(optimisticGens).length > 0
            ? { ...s.generations, ...optimisticGens }
            : s.generations,
        composer: {
          ...createInitialComposer(),
          mode: s.composer.mode,
          params: s.composer.params,
          reasoningEffort: s.composer.reasoningEffort,
          fast: s.composer.fast,
          webSearch: s.composer.webSearch,
          fileSearch: s.composer.fileSearch,
          codeInterpreter: s.composer.codeInterpreter,
          imageGeneration: s.composer.imageGeneration,
        },
      }));

      // chat_params 仅对 chat / vision_qa 有意义。
      const chatParamsObj: Record<string, unknown> | undefined = (() => {
        if (isImage) return undefined;
        const cp: Record<string, unknown> = {};
        if (reasoningEffort) cp.reasoning_effort = reasoningEffort;
        cp.fast = fast;
        if (webSearch) cp.web_search = true;
        if (fileSearch) cp.file_search = true;
        if (codeInterpreter) cp.code_interpreter = true;
        if (imageGeneration) cp.image_generation = true;
        return Object.keys(cp).length > 0 ? cp : undefined;
      })();

      const body: PostMessageIn = {
        idempotency_key: uuid(),
        text: requestText,
        // generated 参考图的 a.id 是本地 uuid（用于 composer 增删管理），真实后端
        // image_id 在 source_image_id；upload 路径下两者相同（id = 后端 image_id）。
        attachment_image_ids: attachmentImageIds,
        attachments: structuredAttachments,
        input_images: attachmentImageIds,
        source: "composer",
        action_source: actionSource,
        trace_id: traceId,
        ...(maskImageId ? { mask_image_id: maskImageId } : {}),
        intent,
        image_params: isImage
          ? (() => {
              const {
                quality: _q,
                render_quality: renderQualityOverride,
                output_format: outputFormatOverride,
                output_compression: outputCompressionOverride,
                background: backgroundOverride,
                moderation: moderationOverride,
                ...rest
              } = params;
              const q = _q ?? "4k";
              const resolved = qualityToFixedSize(q, params.aspect_ratio);
              const outputFormat = outputFormatOverride;
              const renderQuality = normalizeRenderQuality(renderQualityOverride);
              const outputCompression =
                outputFormat === undefined
                  ? undefined
                  : (outputCompressionOverride ??
                    defaultOutputCompression({
                      renderQuality,
                      outputFormat,
                      fast,
                    }));
              return {
                ...rest,
                ...resolved,
                quality: q,
                fast,
                render_quality: renderQuality,
                ...(outputFormat === undefined
                  ? {}
                  : { output_format: outputFormat }),
                ...(outputCompression === undefined
                  ? {}
                  : { output_compression: outputCompression }),
                background: backgroundOverride ?? "auto",
                moderation: moderationOverride ?? "low",
              };
            })()
          : undefined,
        chat_params: chatParamsObj,
      };

      const removeOptimisticSend = () => {
        if (!optimisticUserId || !optimisticAssistantId) return;
        _messageConvIds.delete(optimisticUserId);
        _messageConvIds.delete(optimisticAssistantId);
        for (const id of optimisticGenIds) {
          _generationConvIds.delete(id);
        }
        set((s) => {
          const messages = s.messages.filter(
            (m) => m.id !== optimisticUserId && m.id !== optimisticAssistantId,
          );
          if (optimisticGenIds.length === 0) {
            return messages.length === s.messages.length ? s : { messages };
          }
          const generations = { ...s.generations };
          let changedGens = false;
          for (const id of optimisticGenIds) {
            if (id in generations) {
              delete generations[id];
              changedGens = true;
            }
          }
          return {
            messages,
            generations: changedGens ? generations : s.generations,
          };
        });
      };

      try {
        const out = await apiPostMessage(convId, body, { signal: ctl.signal });
        if (ctl.signal.aborted || get().currentConvId !== convId) {
          removeOptimisticSend();
          return;
        }

        // 2) 校正 id：把乐观条目替换成后端权威条目
        const realUser = {
          ...adaptBackendUserMessage(
            out.user_message,
            attachments,
            params,
            intent,
          ),
          // UI 保留原始 @图N 文本；请求体里的 [image N] 只给后端/模型看。
          text,
        };
        const genIds = out.generation_ids ?? [];
        // chat / vision_qa 只会有 completion_id；text_to_image / image_to_image 只会有 generation_ids
        const completionId = !isImage
          ? (out.completion_id ?? undefined)
          : undefined;
        const aliasNow = Date.now();
        genIds.forEach((realId, index) => {
          const optimisticId = optimisticGenIds[index];
          if (optimisticId)
            rememberGenerationAlias(realId, optimisticId, aliasNow);
        });
        if (completionId) {
          rememberCompletionAlias(
            completionId,
            optimisticAssistantId,
            aliasNow,
          );
        }
        const realAssistant = adaptBackendAssistantMessage(
          out.assistant_message,
          realUser.id,
          intent,
          isImage ? genIds : undefined,
          completionId,
        );
        rememberCompletionMessage(completionId, realAssistant.id);
        _messageConvIds.delete(optimisticUserId);
        _messageConvIds.delete(optimisticAssistantId);
        setBounded(_messageConvIds, realUser.id, convId);
        setBounded(_messageConvIds, realAssistant.id, convId);
        for (const id of genIds) {
          setBounded(_generationConvIds, id, convId);
        }

        set((s) => {
          if (s.currentConvId !== convId) return s;
          const nextMessages = s.messages.map((m) => {
            if (m.id === optimisticUserId) return realUser;
            if (m.id === optimisticAssistantId) return realAssistant;
            return m;
          });
          // 迁移 generation ids：把所有乐观占位迁移为后端真实 ids
          let nextGens = s.generations;
          if (optimisticGenIds.length > 0 && genIds.length > 0) {
            const rest = { ...s.generations };
            const migrated: Record<string, Generation> = {};
            optimisticGenIds.forEach((optimisticId, index) => {
              const old = rest[optimisticId];
              delete rest[optimisticId];
              _generationConvIds.delete(optimisticId);
              const realId = genIds[index];
              if (!old || !realId) return;
              _generationIdAliases.delete(realId);
              migrated[realId] = {
                ...rest[realId],
                ...old,
                id: realId,
                message_id: realAssistant.id,
                image: rest[realId]?.image ?? old.image,
                status: rest[realId]?.status ?? old.status,
                stage: rest[realId]?.stage ?? old.stage,
                finished_at: rest[realId]?.finished_at ?? old.finished_at,
              };
            });
            nextGens = { ...rest, ...migrated };
          } else if (optimisticGenIds.length > 0 && genIds.length === 0) {
            // 后端未返回 generation_ids：助手消息可能是 chat / vision_qa，清理占位 generation
            const rest = { ...s.generations };
            optimisticGenIds.forEach((id) => {
              delete rest[id];
              _generationConvIds.delete(id);
            });
            nextGens = rest;
          }
          return { messages: nextMessages, generations: nextGens };
        });
        if (completionId) _completionMessageAliases.delete(completionId);
      } catch (err) {
        if (
          isAbortRequest(err, ctl.signal) ||
          ctl.signal.aborted ||
          get().currentConvId !== convId
        ) {
          removeOptimisticSend();
          return;
        }
        const code = err instanceof ApiError ? err.code : "client_exception";
        const rawMessage = err instanceof Error ? err.message : "发送失败";
        // 优先用错误码映射的友好文案；映射缺失时回退到原始 message（不再暴露 code 给用户）
        const friendly = errorCodeToMessage(code);
        const message = friendly ?? rawMessage;
        const uiErr = `发送失败：${message}`;
        logWarn("sendMessage failed", {
          scope: "chat",
          code,
          extra: { raw: rawMessage, phase },
        });
        removeOptimisticSend();
        set((s) => ({
          composerError: uiErr,
          ...(opts?.restoreComposerOnFailure !== false &&
          isResetComposerDraft(s.composer)
            ? { composer: cloneComposerState(composerToSend) }
            : {}),
        }));
      } finally {
        untrackSendRequest();
      }
    },

    async retryAssistant(assistantMsgId) {
      const state = get();
      const asst = state.messages.find(
        (m): m is AssistantMessage =>
          m.role === "assistant" && m.id === assistantMsgId,
      );
      if (!asst) return;
      if (
        asst.intent_resolved === "text_to_image" ||
        asst.intent_resolved === "image_to_image"
      ) {
        const genIds = generationIdsOfMessage(asst);
        const genId = genIds.find((id) => {
          const status = get().generations[id]?.status;
          return status === "failed" || status === "canceled";
        }) ?? genIds[0];
        if (genId) {
          await get().retryGeneration(genId);
          return;
        }
      }
      const userMsg = state.messages.find(
        (m): m is UserMessage =>
          m.role === "user" && m.id === asst.parent_user_message_id,
      );
      if (!userMsg) return;

      // BUG-018: 若用户消息文本为空（仅附件），使用原始消息内容作为 retry 文本。
      const retryText = userMsg.text.trim() || "(请继续)";

      // 把 composer 临时覆盖为该消息的快照，再 sendMessage。
      // 用 try/finally 确保 sendMessage 抛错时 composer 也能被清理（sendMessage 成功路径会 clearComposer，
      // 失败路径只留错误提示而 composer 仍是临时快照——这里兜底清掉，避免下次发送沿用 retry 草稿）。
      const composerSnapshot = get().composer;
      set((s) => ({
        composer: {
          ...s.composer,
          text: retryText,
          attachments: userMsg.attachments,
          params: userMsg.image_params,
          webSearch: userMsg.web_search ?? s.composer.webSearch,
          fileSearch: userMsg.file_search ?? s.composer.fileSearch,
          codeInterpreter:
            userMsg.code_interpreter ?? s.composer.codeInterpreter,
          imageGeneration:
            userMsg.image_generation ?? s.composer.imageGeneration,
        },
      }));
      try {
        await get().sendMessage({ intentOverride: asst.intent_resolved });
      } finally {
        // sendMessage 成功时 composer 已被 clearComposer 重置；这里仅当仍是 retry 注入的快照时还原
        const cur = get().composer;
        if (cur.text === retryText && cur.attachments === userMsg.attachments) {
          set({ composer: composerSnapshot });
        }
      }
    },

    async retryGeneration(generationId) {
      await retryTask("generations", generationId);

      set((s) => {
        const gen = s.generations[generationId];
        if (!gen) return s;

        const nextGen: Generation = {
          ...gen,
          status: "queued",
          stage: "queued",
          substage: undefined,
          image: undefined,
          error_code: undefined,
          error_message: undefined,
          attempt: 0,
          max_attempts: undefined,
          retry_eta: undefined,
          retry_error: undefined,
          elapsed: undefined,
          partial_count: undefined,
          failover_count: undefined,
          started_at: 0,
          finished_at: undefined,
        };
        const nextGenerations = {
          ...s.generations,
          [generationId]: nextGen,
        };
        return {
          composerError: null,
          generations: nextGenerations,
          messages: s.messages.map((m) => {
            if (
              m.role !== "assistant" ||
              !assistantHasGeneration(m, generationId)
            ) {
              return m;
            }
            return {
              ...m,
              status: aggregateGenerationStatus(
                generationIdsOfMessage(m),
                nextGenerations,
                m.status,
              ),
            } as AssistantMessage;
          }),
        };
      });
    },

    // 意图纠偏重跑：找到对应 assistant msg → POST regenerate → 乐观替换为 pending
    // 后端会取消旧任务、cancel 旧 assistant，并通过 SSE 推 message.created/generation.queued
    // 等事件，store 已有的 SSE 处理器会消费它们更新 UI。
    async regenerateAssistant(messageId, newIntent) {
      const state = get();
      const convId = state.currentConvId;
      if (!convId) {
        throw new ApiError({
          code: "no_conversation",
          message: "当前没有活动会话",
          status: 0,
        });
      }
      const asstIdx = state.messages.findIndex(
        (m) => m.role === "assistant" && m.id === messageId,
      );
      if (asstIdx < 0) {
        throw new ApiError({
          code: "message_not_found",
          message: "找不到对应的助手消息",
          status: 0,
        });
      }
      const oldAsst = state.messages[asstIdx] as AssistantMessage;
      const parentUserId = oldAsst.parent_user_message_id;
      if (!parentUserId) {
        throw new ApiError({
          code: "missing_parent",
          message: "助手消息缺少 parent_user_message_id",
          status: 0,
        });
      }
      const oldGenId = oldAsst.generation_id;
      const oldGen = oldGenId ? state.generations[oldGenId] : undefined;

      // 1) 乐观从 messages 中移除旧 assistant；保存快照用于回滚
      set((s) => ({
        messages: s.messages.filter(
          (m) => !(m.role === "assistant" && m.id === messageId),
        ),
      }));

      try {
        const out = await apiFetch<{
          assistant_message_id: string;
          completion_id: string | null;
          generation_ids: string[];
        }>(`/conversations/${convId}/messages/${messageId}/regenerate`, {
          method: "POST",
          body: JSON.stringify({
            intent: newIntent,
            idempotency_key: uuid(),
          }),
        });

        const isImage =
          newIntent === "text_to_image" || newIntent === "image_to_image";
        const newGenId = isImage ? out.generation_ids?.[0] : undefined;
        const completionId = !isImage
          ? (out.completion_id ?? undefined)
          : undefined;
        const now = Date.now();

        // 2) 乐观插入 pending assistant，避免 SSE 到达前空窗
        const pendingAsst: AssistantMessage = {
          id: out.assistant_message_id,
          role: "assistant",
          parent_user_message_id: parentUserId,
          intent_resolved: newIntent,
          status: "pending",
          generation_id: newGenId,
          completion_id: completionId,
          created_at: now,
        };
        setBounded(_messageConvIds, out.assistant_message_id, convId);
        rememberCompletionMessage(completionId, out.assistant_message_id);

        // 同时为 image intent 占位一个 queued generation，让当前会话画布立刻显示骨架。
        let pendingGen: Generation | undefined;
        if (isImage && newGenId) {
          const parentUser = state.messages.find(
            (m): m is UserMessage => m.role === "user" && m.id === parentUserId,
          );
          const params = parentUser?.image_params;
          const attachments = parentUser?.attachments ?? [];
          pendingGen = {
            id: newGenId,
            message_id: out.assistant_message_id,
            action: newIntent === "image_to_image" ? "edit" : "generate",
            prompt: parentUser?.text ?? oldGen?.prompt ?? "",
            size_requested:
              params?.size_mode === "fixed" && params.fixed_size
                ? params.fixed_size
                : "auto",
            aspect_ratio: params?.aspect_ratio ?? "16:9",
            input_image_ids: attachments.map((a) => a.source_image_id ?? a.id),
            primary_input_image_id:
              attachments[0]?.source_image_id ?? attachments[0]?.id ?? null,
            status: "queued",
            stage: "queued",
            attempt: 0,
            started_at: 0,
          };
          rememberGenerationForConversation(convId, pendingGen);
        }

        set((s) => {
          // 把 pending assistant 插回原位置（按 created_at 顺序时它就该在那）
          const nextMessages = [
            ...s.messages.slice(0, asstIdx),
            pendingAsst,
            ...s.messages.slice(asstIdx),
          ];
          let nextGens = s.generations;
          // 旧 generation 标 canceled（保留以便用户看到历史轨迹由 SSE 决定，但本地立即标记）
          if (oldGenId && nextGens[oldGenId]) {
            nextGens = {
              ...nextGens,
              [oldGenId]: {
                ...nextGens[oldGenId],
                status: "canceled",
                finished_at: now,
              },
            };
          }
          if (pendingGen) {
            nextGens = { ...nextGens, [pendingGen.id]: pendingGen };
          }
          return { messages: nextMessages, generations: nextGens };
        });
      } catch (err) {
        // 回滚：把旧 assistant 放回原位置
        set((s) => {
          if (s.messages.some((m) => m.id === oldAsst.id)) return s;
          return {
            messages: [
              ...s.messages.slice(0, asstIdx),
              oldAsst,
              ...s.messages.slice(asstIdx),
            ],
          };
        });
        throw err;
      }
    },

    async upscaleImage(imageId) {
      const state = get();
      const convId = state.currentConvId;
      if (!convId) return;
      const img = state.imagesById[imageId];
      if (!img) return;
      const gen = img.from_generation_id
        ? state.generations[img.from_generation_id]
        : undefined;
      const aspect = (gen?.aspect_ratio ?? "16:9") as AspectRatio;
      const preset = PRESET[aspect] ?? PRESET["16:9"];
      const fixedSize = `${preset.w}x${preset.h}`;
      const originalPrompt = gen?.prompt ?? "";
      const upscaleInstruction = [
        `[Pure fidelity upscale - ${fixedSize}]`,
        ``,
        `Faithfully upscale this image to ${fixedSize} as a pure fidelity task, not an enhancement or redraw.`,
        ``,
        `Preserve the exact framing, composition, face, expression, pose, colors, lighting, mood, skin texture, hair, fabric, water, grain, and natural smartphone-photo look.`,
        `Preserve all blur, softness, shallow depth of field, haze, and background defocus exactly; do not treat softness as missing detail.`,
        `Do not beautify, retouch, sharpen, denoise, smooth skin, add texture, invent details, alter facial features, change colors, or make it look AI-generated.`,
        `The result should look like the exact same photo captured at higher resolution.`,
      ].join("\n");
      const upscaleText = appendPromptWithinLimit(
        originalPrompt,
        upscaleInstruction,
      );
      if (
        originalPrompt &&
        originalPrompt.trim().length + upscaleInstruction.trim().length + 2 >
          MAX_PROMPT_CHARS
      ) {
        logWarn("upscale prompt trimmed to request limit", {
          scope: "chat",
          code: "prompt_too_long",
          extra: {
            originalLength: originalPrompt.length,
            finalLength: upscaleText.length,
          },
        });
      }

      const asst = gen
        ? state.messages.find(
            (m): m is AssistantMessage =>
              m.role === "assistant" &&
              (m.generation_id === img.from_generation_id ||
                (m.generation_ids?.includes(img.from_generation_id) ?? false)),
          )
        : undefined;
      const parentMsgId =
        asst?.parent_user_message_id ?? lastUserMessageId(state.messages);
      if (!parentMsgId) return;

      const out = await createSilentGeneration(convId, {
        idempotency_key: uuid(),
        parent_message_id: parentMsgId,
        intent: "image_to_image",
        prompt: upscaleText,
        attachment_image_ids: [img.id],
        image_params: {
          aspect_ratio: aspect,
          size_mode: "fixed",
          fixed_size: fixedSize,
          quality: "4k",
          count: 1,
          fast: _runtimeFastDefault ?? false,
          render_quality: "high",
          background: "auto",
          moderation: "low",
        },
      });

      const genIds = out.generation_ids ?? [];
      const realAssistant = adaptBackendAssistantMessage(
        out.assistant_message,
        parentMsgId,
        "image_to_image",
        genIds,
        undefined,
      );
      setBounded(_messageConvIds, realAssistant.id, convId);
      for (const gid of genIds) setBounded(_generationConvIds, gid, convId);

      const optimisticGens: Record<string, Generation> = {};
      for (const gid of genIds) {
        optimisticGens[gid] = {
          id: gid,
          message_id: realAssistant.id,
          action: "edit",
          prompt: upscaleText,
          size_requested: fixedSize,
          aspect_ratio: aspect,
          input_image_ids: [img.id],
          primary_input_image_id: img.id,
          status: "queued",
          stage: "queued",
          attempt: 0,
          started_at: 0,
        };
      }
      set((s) => ({
        messages: [...s.messages, realAssistant],
        generations: { ...s.generations, ...optimisticGens },
      }));
    },

    async rerollImage(imageId) {
      const state = get();
      const convId = state.currentConvId;
      if (!convId) return;
      const img = state.imagesById[imageId];
      if (!img) return;
      const genId = img.from_generation_id;
      if (!genId) return;
      const gen = state.generations[genId];
      if (!gen) return;

      const asst = state.messages.find(
        (m): m is AssistantMessage =>
          m.role === "assistant" &&
          (m.generation_id === genId ||
            (m.generation_ids?.includes(genId) ?? false)),
      );
      const parentMsgId =
        asst?.parent_user_message_id ?? lastUserMessageId(state.messages);
      if (!parentMsgId) return;

      const hasInput = gen.input_image_ids.length > 0;
      const intent: "text_to_image" | "image_to_image" = hasInput
        ? "image_to_image"
        : "text_to_image";
      const rerollRenderQuality = "high";
      const rerollQuality = qualityFromFixedSize(
        gen.size_requested,
        gen.aspect_ratio,
      );

      const out = await createSilentGeneration(convId, {
        idempotency_key: uuid(),
        parent_message_id: parentMsgId,
        intent,
        prompt: clampPromptForRequest(gen.prompt),
        attachment_image_ids: hasInput ? gen.input_image_ids : [],
        image_params: {
          aspect_ratio: gen.aspect_ratio,
          size_mode: gen.size_requested.includes("x") ? "fixed" : "auto",
          fixed_size: gen.size_requested.includes("x")
            ? gen.size_requested
            : undefined,
          quality: rerollQuality,
          count: 1,
          fast: _runtimeFastDefault ?? false,
          render_quality: rerollRenderQuality,
          background: "auto",
          moderation: "low",
        },
      });

      const genIds = out.generation_ids ?? [];
      const realAssistant = adaptBackendAssistantMessage(
        out.assistant_message,
        parentMsgId,
        intent,
        genIds,
        undefined,
      );
      setBounded(_messageConvIds, realAssistant.id, convId);
      for (const gid of genIds) setBounded(_generationConvIds, gid, convId);

      const optimisticGens: Record<string, Generation> = {};
      for (const gid of genIds) {
        optimisticGens[gid] = {
          id: gid,
          message_id: realAssistant.id,
          action: gen.action,
          prompt: gen.prompt,
          size_requested: gen.size_requested,
          aspect_ratio: gen.aspect_ratio,
          input_image_ids: gen.input_image_ids,
          primary_input_image_id: gen.primary_input_image_id,
          status: "queued",
          stage: "queued",
          attempt: 0,
          started_at: 0,
        };
      }
      set((s) => ({
        messages: [...s.messages, realAssistant],
        generations: { ...s.generations, ...optimisticGens },
      }));
    },

    // —— 独立的局部修改提交入口 ——
    // 浏览态（Lightbox / 卡片 / 对话气泡）的"局部修改"会调到这里。
    //
    // 实现：
    //   1) 把 mask blob 上传到后端拿到 mask_image_id
    //   2) 备份用户当前 composer 草稿
    //   3) 临时把 composer 覆盖为：单张 inpaint 参考图 + mask + prompt + image 模式
    //   4) 复用 sendMessage（它会发出 image_to_image + mask_image_id，并 reset composer 偏好以外的字段）
    //   5) finally 还原用户原始 text/attachments/mask/forceIntent —— 保留 mode/params/偏好已经被 sendMessage 留住
    //
    // 不走 createSilentGeneration：silent endpoint 当前不接受 mask_image_id，且 inpaint 期望在
    // 对话历史里出现一条用户消息（带 prompt 与所引用的图），UX 上更自然。
    async submitInpaintTask({
      sourceImageId,
      sourceSrc,
      sourceWidth,
      sourceHeight,
      maskBlob,
      maskPreviewDataUrl,
      prompt,
    }) {
      const text = prompt.trim();
      if (!text) {
        set({ composerError: "修改内容未填" });
        throw new Error("修改内容未填");
      }
      if (isPromptTooLong(text)) {
        set({ composerError: PROMPT_TOO_LONG_MESSAGE });
        throw new Error(PROMPT_TOO_LONG_MESSAGE);
      }
      if (!sourceImageId || !sourceSrc) {
        const msg = "图片信息不完整，无法发起局部修改";
        set({ composerError: msg });
        throw new Error(msg);
      }

      let maskUploaded;
      try {
        const maskFile = new File([maskBlob], "mask.png", {
          type: "image/png",
        });
        maskUploaded = await apiUploadImage(maskFile);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "mask 上传失败";
        logWarn("inpaint mask upload failed", {
          scope: "inpaint",
          extra: { msg },
        });
        set({ composerError: `局部修改失败：${msg}` });
        throw err instanceof Error ? err : new Error(msg);
      }

      const backup = get().composer;
      const tempAttId = uuid();
      const tempAtt: AttachmentImage = {
        id: tempAttId,
        kind: "generated",
        data_url: sourceSrc,
        mime: "image/png",
        width: sourceWidth,
        height: sourceHeight,
        source_image_id: sourceImageId,
      };

      // inpaint 必须按原图比例生成，否则后端会按 composer 的 aspect_ratio（默认 16:9）出图，
      // 16:9 的 mask 套到 4:3 原图上构图被拉变形 / 涂抹区错位 — 是用户高频反馈的体验崩溃点。
      // 优先用 source 传入的尺寸，缺失（旧入口/历史数据）才退到 composer.params.aspect_ratio。
      const inferredAspect =
        sourceWidth && sourceHeight
          ? nearestAspectRatio(sourceWidth, sourceHeight)
          : null;

      set((s) => ({
        composer: {
          ...s.composer,
          text,
          attachments: [tempAtt],
          mode: "image",
          forceIntent: "image",
          mask: {
            image_id: maskUploaded.id,
            preview_data_url: maskPreviewDataUrl,
            target_attachment_id: tempAttId,
          },
          // 局部修改强制单张 + 跟随原图比例（fallback：保留 composer 偏好）
          // size_mode/fixed_size 由 sendMessage 按 quality + aspect_ratio 重算，无需在此覆盖
          params: {
            ...s.composer.params,
            aspect_ratio: inferredAspect ?? s.composer.params.aspect_ratio,
            count: 1,
          },
        },
      }));

      try {
        await get().sendMessage({ restoreComposerOnFailure: false });
      } finally {
        // sendMessage reset composer 后，把用户原本未发出的草稿字段补回。
        // 但若 composer 已被外部改动（如其他流程主动写了新草稿），不要覆盖。
        // 识别：sendMessage 内部 reset 后 attachments=[] / text=""，这是我们能安全还原的标志。
        const cur = get().composer;
        const isStillReset =
          cur.text === "" &&
          cur.attachments.length === 0 &&
          cur.mask === null &&
          cur.forceIntent === undefined;
        if (isStillReset) {
          set((s) => ({
            composer: {
              ...s.composer,
              text: backup.text,
              attachments: backup.attachments,
              mask: backup.mask,
              forceIntent: backup.forceIntent,
              params: backup.params,
            },
          }));
        }
      }

      // sendMessage 失败时只设 composerError 不抛错（其他调用方依赖这一行为）；
      // 但 inpaint 路径需要把失败传给 InpaintModal，否则会走成功 toast/清草稿/关弹窗。
      const sendError = get().composerError;
      if (sendError) {
        throw new Error(sendError);
      }
    },

    appendUserMessage: (msg) => {
      const convId = get().currentConvId;
      if (convId) setBounded(_messageConvIds, msg.id, convId);
      invalidateConversationHistoryCache(convId);
      set((s) => ({ messages: [...s.messages, msg] }));
    },
    appendAssistantMessage: (msg) => {
      const convId = get().currentConvId;
      if (convId) setBounded(_messageConvIds, msg.id, convId);
      rememberCompletionMessage(msg.completion_id, msg.id);
      invalidateConversationHistoryCache(convId);
      set((s) => ({ messages: [...s.messages, msg] }));
    },
    upsertGeneration: (gen) => {
      const convId = _messageConvIds.get(gen.message_id) ?? get().currentConvId;
      if (convId) rememberGenerationForConversation(convId, gen);
      invalidateConversationHistoryCache(convId);
      set((s) => ({ generations: { ...s.generations, [gen.id]: gen } }));
    },
    attachImageToGeneration: (generationId, img) => {
      const finishedAt = Date.now();
      set((s) => {
        const gen = s.generations[generationId];
        if (!gen) return s;
        const convId = generationConversationId(s, gen);
        if (convId) {
          setBounded(_imageConvIds, img.id, convId);
          invalidateConversationHistoryCache(convId);
        }
        return {
          generations: {
            ...s.generations,
            [generationId]: {
              ...gen,
              image: img,
              status: "succeeded",
              stage: "finalizing",
              finished_at: finishedAt,
            },
          },
          imagesById: { ...s.imagesById, [img.id]: img },
        };
      });
    },

    // —— SSE 事件派发 ——
    // 对齐 DESIGN §5.7 事件名。未识别事件静默忽略，避免污染日志。
    applySSEEvent: (eventName, data) => {
      const eventNow = Date.now();
      const payload = ssePayloadRecord(eventName, data);
      if (!payload) return;
      invalidateConversationHistoryCache(get().currentConvId);
      const get_id = (key: string): string | undefined => {
        const v = payload[key];
        return typeof v === "string" ? v : undefined;
      };

      try {
        switch (eventName) {
        case "generation.queued":
        case "generation.started":
        case "generation.progress":
        case "generation.partial_image":
        case "generation.succeeded":
        case "generation.failed":
        case "generation.retrying": {
          const rawId = get_id("generation_id") ?? get_id("id");
          if (!rawId) return;
          const id = generationLookupId(rawId, eventNow);
          // generation.succeeded 终态可能携带 image：先在 set 外解析，但不要写 store。
          // 真正的状态变更（generations / imagesById / messages）合并到一次 set，避免高频事件竞态。
          let pendingImage: GeneratedImage | undefined;
          if (eventName === "generation.succeeded") {
            // DESIGN §5.7: payload = { images: [{image_id, url, actual_size, from_generation_id, parent_image_id?}, ...], final_size }
            const first = optionalRecordArray(payload.images)?.[0];
            const imageId = first ? recordString(first, "image_id") : undefined;
            if (!first || !imageId) {
              logWarn("missing image_id in succeeded payload", {
                scope: "chat-sse",
                extra: { generation_id: id },
              });
              return;
            }
            const src =
              recordString(first, "data_url") ??
              recordString(first, "url") ??
              imageBinaryUrl(imageId);
            const generationExplainability = generationExplainabilityFromPayload(payload);
            const firstMetadata = optionalRecord(first.metadata_jsonb);
            const imageMetadata = { ...(firstMetadata ?? {}) };
            if (
              generationExplainability.diagnostics &&
              imageMetadata.generation_diagnostics == null
            ) {
              imageMetadata.generation_diagnostics =
                generationExplainability.diagnostics;
            }
            if (
              generationExplainability.revised_prompt &&
              imageMetadata.revised_prompt == null
            ) {
              imageMetadata.revised_prompt = generationExplainability.revised_prompt;
            }
            const actualSize = recordString(first, "actual_size");
            const { width: w, height: h } = parseSizeString(actualSize);
            pendingImage = {
              id: imageId,
              data_url: src,
              mime: recordString(first, "mime"),
              display_url:
                recordString(first, "display_url") ??
                imageVariantUrl(imageId, "display2048"),
              preview_url:
                recordString(first, "preview_url") ??
                imageVariantUrl(imageId, "preview1024"),
              thumb_url:
                recordString(first, "thumb_url") ??
                imageVariantUrl(imageId, "thumb256"),
              width: w,
              height: h,
              parent_image_id: recordNullableString(first, "parent_image_id") ?? null,
              from_generation_id: id,
              size_requested: "auto",
              size_actual: actualSize ?? "unknown",
              filename: recordString(first, "filename"),
              metadata_jsonb:
                Object.keys(imageMetadata).length > 0 ? imageMetadata : null,
              ...generationExplainability,
              ...billingMetaFromPayload(
                {
                  is_dual_race_bonus: recordBoolean(first, "is_dual_race_bonus"),
                  billing_free: recordBoolean(first, "billing_free"),
                  billing_label: recordString(first, "billing_label"),
                  billing_exempt_reason: recordString(first, "billing_exempt_reason"),
                },
                firstMetadata,
              ),
            };
          }
          set((s) => {
            const gen = s.generations[id];
            const isTerminal =
              eventName === "generation.succeeded" ||
              eventName === "generation.failed";
            // 即使 gen 不存在（store 任务池被外部清理过），终态仍要把对应 assistant message 收尾
            if (!gen) {
              if (!isTerminal) {
                // 中间态事件没有对应 generation：可能是 store 已清理 / 跨 tab / SSE 乱序
                logWarn("SSE event for orphan generation", {
                  scope: "chat-sse",
                  extra: { generation_id: rawId, event: eventName },
                });
                return s;
              }
              return {
                messages: s.messages.map((m) => {
                  if (m.role === "assistant" && assistantHasGeneration(m, id)) {
                    return {
                      ...m,
                      status:
                        eventName === "generation.succeeded"
                          ? "succeeded"
                          : "failed",
                    } as AssistantMessage;
                  }
                  return m;
                }),
              };
            }
            const patch: Partial<Generation> = {};
            if (eventName === "generation.queued") {
              patch.status = "queued";
              patch.stage = "queued";
              patch.substage =
                coerceGenerationSubstage(payload.substage) ??
                (payload.reason === "image_provider_unavailable"
                  ? "waiting_provider"
                  : "waiting_queue");
              patch.queue_position =
                recordNumber(payload, "queue_position") ?? gen.queue_position ?? null;
              patch.retrying = payload.retrying === true || false;
              patch.waiting_provider =
                payload.waiting_provider === true ||
                payload.reason === "image_provider_unavailable" ||
                patch.substage === "waiting_provider";
              patch.cancelled = false;
              patch.started_at = 0;
            } else if (eventName === "generation.started") {
              patch.status = "running";
              patch.stage = "understanding";
              patch.substage =
                coerceGenerationSubstage(payload.substage) ?? "upstream_started";
              patch.started_at = gen.started_at > 0 ? gen.started_at : eventNow;
              const att = payload.attempt;
              if (typeof att === "number") patch.attempt = att;
              patch.retry_eta = undefined;
              patch.retry_error = undefined;
              patch.retrying = false;
              patch.waiting_provider = false;
              patch.cancelled = false;
            } else if (eventName === "generation.progress") {
              const stage = payload.stage;
              if (
                stage === "queued" ||
                stage === "understanding" ||
                stage === "rendering" ||
                stage === "finalizing"
              ) {
                patch.stage = stage;
              }
              // P1 细颗粒子阶段：worker 在 RENDERING/FINALIZING 内打了多个里程碑，
              // 让 DevelopingCard 等组件可读取 substage 切换更精细的视觉。
              // 旧消息（不含 substage）保持 undefined，行为同当前。
              const substage = coerceGenerationSubstage(payload.substage);
              if (substage) patch.substage = substage;
              patch.queue_position =
                recordNumber(payload, "queue_position") ?? gen.queue_position ?? null;
              if (payload.retrying === true) patch.retrying = true;
              if (payload.waiting_provider === true) patch.waiting_provider = true;
              if (payload.cancelled === true) patch.cancelled = true;
              // P2 worker 内 failover：上游切到下一个 provider 时携带 provider_failover=true。
              // 累加计数到 Generation 上，DevelopingCard 可显示"换号重试 N 次"。
              if (payload.provider_failover === true) {
                patch.failover_count = (gen.failover_count ?? 0) + 1;
              }
              patch.status = "running";
              if (!(gen.started_at > 0)) patch.started_at = eventNow;
            } else if (eventName === "generation.partial_image") {
              patch.status = "running";
              patch.stage = "rendering";
              patch.substage = "partial_received";
              patch.retrying = false;
              patch.waiting_provider = false;
              if (!(gen.started_at > 0)) patch.started_at = eventNow;
            } else if (eventName === "generation.succeeded" && pendingImage) {
              const generationExplainability =
                generationExplainabilityFromPayload(payload);
              // 用 gen.primary_input_image_id 兜底 parent_image_id / size_requested
              const finalImg: GeneratedImage = {
                ...pendingImage,
                parent_image_id:
                  pendingImage.parent_image_id ?? gen.primary_input_image_id,
                size_requested: gen.size_requested,
              };
              const convId = generationConversationId(s, gen);
              if (convId) setBounded(_imageConvIds, finalImg.id, convId);
              patch.image = finalImg;
              patch.status = "succeeded";
              patch.stage = "finalizing";
              patch.substage = "display_ready";
              patch.retrying = false;
              patch.waiting_provider = false;
              patch.cancelled = false;
              patch.finished_at = eventNow;
              Object.assign(patch, generationExplainability);
            } else if (eventName === "generation.failed") {
              const generationExplainability =
                generationExplainabilityFromPayload(payload);
              const code = get_id("code") ?? "generation_failed";
              const retryable = payload.retriable === true;
              patch.status = "failed";
              patch.stage = "finalizing";
              patch.substage = retryable ? "retryable" : "terminal";
              patch.error_code = code;
              patch.error_message =
                optionalString(generationExplainability.diagnostics?.safe_error_summary) ??
                get_id("safe_error_summary") ??
                get_id("message") ??
                "生成失败";
              patch.retryable = retryable;
              patch.recommended_actions =
                recommendedActionsFromUnknown(payload.recommended_actions) ??
                recommendedActionsForError(code, {
                  retryable,
                  status: "failed",
                });
              patch.retrying = false;
              patch.waiting_provider = false;
              patch.cancelled = false;
              patch.finished_at = eventNow;
              Object.assign(patch, generationExplainability);
            } else if (eventName === "generation.retrying") {
              patch.status = "queued";
              patch.stage = "queued";
              patch.substage = "upstream_retrying";
              patch.retrying = true;
              patch.waiting_provider = payload.reason === "image_provider_unavailable";
              patch.cancelled = false;
              patch.started_at = 0;
              const att = payload.attempt;
              if (typeof att === "number") patch.attempt = att;
              const maxAtt = payload.max_attempts;
              if (typeof maxAtt === "number") patch.max_attempts = maxAtt;
              const delaySec = payload.retry_delay_seconds;
              if (typeof delaySec === "number") {
                patch.retry_eta = eventNow + delaySec * 1000;
              }
              patch.retry_error =
                get_id("error_message") ?? get_id("message") ?? undefined;
              patch.error_code = get_id("error_code") ?? patch.error_code;
              patch.error_message = patch.retry_error;
            }
            const nextGen = { ...gen, ...patch };
            const nextGenerations = { ...s.generations, [id]: nextGen };
            const nextMessages = isTerminal
              ? s.messages.map((m) => {
                  if (m.role !== "assistant" || !assistantHasGeneration(m, id))
                    return m;
                  return {
                    ...m,
                    status: aggregateGenerationStatus(
                      generationIdsOfMessage(m),
                      nextGenerations,
                      m.status,
                    ),
                  } as AssistantMessage;
                })
              : s.messages;
            const nextImages =
              patch.image != null
                ? { ...s.imagesById, [patch.image.id]: patch.image }
                : s.imagesById;
            return {
              generations: nextGenerations,
              messages: nextMessages,
              imagesById: nextImages,
            };
          });
          if (eventName === "generation.succeeded") scheduleBase64Eviction();
          break;
        }

        case "generation.canceled": {
          const rawId = get_id("generation_id") ?? get_id("id");
          if (!rawId) return;
          const id = generationLookupId(rawId, eventNow);
          set((s) => {
            const gen = s.generations[id];
            if (!gen) return s;
            const nextGen: Generation = {
              ...gen,
              status: "canceled",
              stage: "finalizing",
              substage: "cancelled",
              cancelled: true,
              retrying: false,
              waiting_provider: false,
              retryable: true,
              error_code: get_id("code") ?? "cancelled",
              error_message: get_id("message") ?? "已取消",
              recommended_actions:
                recommendedActionsFromUnknown(payload.recommended_actions) ??
                recommendedActionsForError("cancelled", {
                  retryable: true,
                  status: "canceled",
                }),
              finished_at: eventNow,
            };
            const nextMessages = s.messages.map((m) => {
              if (m.role !== "assistant" || !assistantHasGeneration(m, id))
                return m;
              return {
                ...m,
                status: aggregateGenerationStatus(
                  generationIdsOfMessage(m),
                  { ...s.generations, [id]: nextGen },
                  "canceled",
                ),
              } as AssistantMessage;
            });
            return {
              generations: { ...s.generations, [id]: nextGen },
              messages: nextMessages,
            };
          });
          break;
        }

        case "generation.attached": {
          // dual_race bonus：winner 已 succeeded 后，loser 也成功 → 后端建独立
          // bonus generation row 并把 id push 到同一条 message。这里在 store 里
          // 建 placeholder + 把 id 加到 message.generation_ids，让随后到达的
          // generation.succeeded 事件能正确把 bonus 图挂到 generation 上。
          const rawMsgId = get_id("message_id");
          const rawGenId = get_id("generation_id");
          if (!rawMsgId || !rawGenId) return;
          const action = get_id("action");
          const prompt = get_id("prompt") ?? "";
          const size_requested = get_id("size_requested") ?? "auto";
          const aspect_ratio = get_id("aspect_ratio") ?? "1:1";
          const primary_input_image_id =
            get_id("primary_input_image_id") ?? null;
          const inputImagesRaw = payload.input_image_ids;
          const input_image_ids = Array.isArray(inputImagesRaw)
            ? (inputImagesRaw.filter((v) => typeof v === "string") as string[])
            : [];
          const generationBillingMeta = billingMetaFromPayload(payload);
          set((s) => {
            // 去重：若已 attach 过同 id（重连/事件回放）则 no-op
            if (s.generations[rawGenId]) {
              const alreadyAttached = s.messages.some(
                (m) =>
                  m.role === "assistant" &&
                  m.id === rawMsgId &&
                  assistantHasGeneration(m, rawGenId),
              );
              if (alreadyAttached) return s;
            }
            const placeholder: Generation = {
              id: rawGenId,
              message_id: rawMsgId,
              action: (action === "edit" ? "edit" : "generate") as
                | "generate"
                | "edit",
              prompt,
              size_requested,
              aspect_ratio: aspect_ratio as Generation["aspect_ratio"],
              input_image_ids,
              primary_input_image_id,
              status: "running",
              stage: "rendering",
              attempt: 0,
              started_at: eventNow,
              ...generationBillingMeta,
            };
            const nextGenerations = s.generations[rawGenId]
              ? s.generations
              : { ...s.generations, [rawGenId]: placeholder };
            const nextMessages = s.messages.map((m) => {
              if (m.role !== "assistant" || m.id !== rawMsgId) return m;
              const existing = generationIdsOfMessage(m);
              if (existing.includes(rawGenId)) return m;
              return {
                ...m,
                generation_ids: [...existing, rawGenId],
              } as AssistantMessage;
            });
            return {
              generations: nextGenerations,
              messages: nextMessages,
            };
          });
          break;
        }

        case "completion.queued":
        case "completion.started":
        case "completion.progress":
        case "completion.delta":
        case "completion.thinking_delta":
        case "completion.image":
        case "completion.succeeded":
        case "completion.failed":
        case "completion.restarted": {
          // 通过 payload.message_id / assistant_message_id 或 payload.completion_id 定位 assistant message
          const rawMsgId =
            get_id("assistant_message_id") ?? get_id("message_id");
          const compId =
            get_id("completion_id") ?? get_id("task_id") ?? get_id("id");
          const msgId = rawMsgId ?? completionMessageLookupId(compId, eventNow);
          if (!msgId && !compId) return;
          rememberCompletionMessage(compId, msgId);
          if (eventName === "completion.thinking_delta") {
            const td =
              typeof payload.thinking_delta === "string"
                ? (payload.thinking_delta as string)
                : "";
            queueCompletionStreamPatch(msgId, compId, "thinking", td);
            break;
          }
          if (eventName === "completion.delta") {
            const delta =
              typeof payload.text_delta === "string"
                ? (payload.text_delta as string)
                : typeof payload.delta === "string"
                  ? (payload.delta as string)
                  : typeof payload.text === "string"
                    ? (payload.text as string)
                    : "";
            queueCompletionStreamPatch(msgId, compId, "text", delta);
            break;
          }
          if (eventName === "completion.image") {
            const first = optionalRecordArray(payload.images)?.[0];
            const imageId = first ? recordString(first, "image_id") : undefined;
            if (!first || !imageId || !msgId || !compId) return;
            const src =
              recordString(first, "data_url") ??
              recordString(first, "url") ??
              imageBinaryUrl(imageId);
            const actualSize = recordString(first, "actual_size");
            const { width: w, height: h } = parseSizeString(actualSize);
            const genId = completionToolGenerationId(compId);
            const img: GeneratedImage = {
              id: imageId,
              data_url: src,
              mime: recordString(first, "mime"),
              display_url:
                recordString(first, "display_url") ??
                imageVariantUrl(imageId, "display2048"),
              preview_url:
                recordString(first, "preview_url") ??
                imageVariantUrl(imageId, "preview1024"),
              thumb_url:
                recordString(first, "thumb_url") ??
                imageVariantUrl(imageId, "thumb256"),
              width: w,
              height: h,
              parent_image_id: null,
              from_generation_id: genId,
              size_requested: actualSize ?? "auto",
              size_actual: actualSize ?? "unknown",
              filename: recordString(first, "filename"),
              metadata_jsonb: optionalRecord(first.metadata_jsonb) ?? null,
            };
            set((s) => {
              const existingGen = s.generations[genId];
              const gen: Generation = existingGen ?? {
                id: genId,
                message_id: msgId,
                action: "generate",
                prompt: "",
                size_requested: img.size_requested,
                aspect_ratio: "1:1",
                input_image_ids: [],
                primary_input_image_id: null,
                status: "succeeded",
                stage: "finalizing",
                attempt: 0,
                started_at: eventNow,
              };
              const nextGen: Generation = {
                ...gen,
                image: img,
                status: "succeeded",
                stage: "finalizing",
                finished_at: eventNow,
              };
              const nextMessages = s.messages.map((m) => {
                if (m.role !== "assistant" || m.id !== msgId) return m;
                const existing = generationIdsOfMessage(m);
                return {
                  ...m,
                  status: "streaming",
                  generation_ids: existing.includes(genId)
                    ? existing
                    : [...existing, genId],
                  generation_id: m.generation_id ?? genId,
                  last_delta_at: eventNow,
                } as AssistantMessage;
              });
              const convId = _messageConvIds.get(msgId) ?? s.currentConvId;
              if (convId) {
                rememberGenerationForConversation(convId, nextGen);
                setBounded(_imageConvIds, img.id, convId);
              }
              return {
                messages: nextMessages,
                generations: { ...s.generations, [genId]: nextGen },
                imagesById: { ...s.imagesById, [img.id]: img },
              };
            });
            scheduleBase64Eviction();
            break;
          }
          flushCompletionStreamPatches();
          set((s) => ({
            messages: s.messages.map((m) => {
              if (m.role !== "assistant") return m;
              const matches =
                (msgId && m.id === msgId) ||
                (compId && m.completion_id === compId);
              if (!matches) return m;
              const next = { ...m } as AssistantMessage;
              if (eventName === "completion.started") {
                next.status = "streaming";
                next.stream_started_at = next.stream_started_at ?? eventNow;
                next.last_delta_at = next.last_delta_at ?? eventNow;
              }
              if (eventName === "completion.progress") {
                next.status = "streaming";
                next.stream_started_at = next.stream_started_at ?? eventNow;
                next.last_delta_at = eventNow;
                const toolCall = coerceCompletionToolCalls([
                  payload.tool_call,
                ])[0];
                if (toolCall) {
                  next.tool_calls = mergeCompletionToolCall(
                    next.tool_calls,
                    toolCall,
                  );
                } else {
                  const toolCalls = coerceCompletionToolCalls(
                    payload.tool_calls,
                  );
                  if (toolCalls.length > 0) next.tool_calls = toolCalls;
                }
              }
              if (eventName === "completion.queued") {
                next.status = "pending";
                next.stream_started_at = undefined;
                next.last_delta_at = undefined;
              }
              if (eventName === "completion.succeeded") {
                next.status = "succeeded";
                if (typeof payload.text === "string") next.text = payload.text;
                const toolCalls = coerceCompletionToolCalls(payload.tool_calls);
                if (toolCalls.length > 0) next.tool_calls = toolCalls;
                const usedMemoryIds = stringArray(payload.used_memory_ids);
                if (usedMemoryIds.length > 0) {
                  next.used_memory_ids = usedMemoryIds;
                  next.used_memory_summary = coerceUsedMemorySummary(
                    payload.used_memory_summary,
                  );
                }
                if (typeof payload.confirmation_candidate_id === "string") {
                  next.confirmation_candidate_id = payload.confirmation_candidate_id;
                }
                next.last_delta_at = eventNow;
              }
              if (eventName === "completion.failed") {
                next.status = "failed";
                const code = get_id("code") ?? "completion_failed";
                const msg = get_id("message") ?? "文本生成失败";
                // 把错误原因拼到助手气泡里展示
                next.text = `⚠️ ${msg}（${code}）`;
                next.last_delta_at = eventNow;
              }
              if (eventName === "completion.restarted") {
                next.status = "pending";
                next.text = "";
                next.thinking = "";
                next.tool_calls = undefined;
                next.stream_started_at = undefined;
                next.last_delta_at = undefined;
              }
              return next;
            }),
          }));
          if (
            eventName === "completion.succeeded" &&
            compId &&
            typeof payload.text !== "string"
          ) {
            void get()
              .refreshCompletionText(compId)
              .catch((err) => {
                logWarn("completion terminal refresh after SSE failed", {
                  scope: "chat-sse",
                  code: err instanceof ApiError ? err.code : undefined,
                  extra: { completionId: compId, err: errorToMessage(err) },
                });
              });
          }
          break;
        }

        case "memory.writes": {
          const msgId =
            get_id("assistant_message_id") ?? get_id("message_id");
          const writes = coerceMemoryWrites(payload.memory_writes);
          if (!msgId || writes.length === 0) return;
          set((s) => ({
            messages: s.messages.map((m) => {
              if (m.role !== "assistant" || m.id !== msgId) return m;
              const current = (m as AssistantMessage).memory_writes ?? [];
              const seen = new Set(
                current.map((item) => `${item.kind}:${item.id ?? item.content}`),
              );
              const nextWrites = [
                ...current,
                ...writes.filter(
                  (item) => !seen.has(`${item.kind}:${item.id ?? item.content}`),
                ),
              ];
              return {
                ...(m as AssistantMessage),
                memory_writes: nextWrites,
              } as AssistantMessage;
            }),
          }));
          break;
        }

        case "account_settings_updated": {
          break;
        }

        case "message.intent_resolved": {
          const msgId = get_id("assistant_message_id") ?? get_id("message_id");
          const resolved = payload.intent_resolved;
          if (!msgId) return;
          if (
            resolved === "chat" ||
            resolved === "vision_qa" ||
            resolved === "text_to_image" ||
            resolved === "image_to_image"
          ) {
            set((s) => ({
              messages: s.messages.map((m) =>
                m.role === "assistant" && m.id === msgId
                  ? ({ ...m, intent_resolved: resolved } as AssistantMessage)
                  : m,
              ),
            }));
          }
          break;
        }

        case "conv.message.appended": {
          const convId = get_id("conversation_id") ?? get_id("conv_id");
          const messageId = get_id("message_id") ?? get_id("id");
          if (!convId || convId !== get().currentConvId) break;
          if (messageId && get().messages.some((m) => m.id === messageId))
            break;

          void (async () => {
            try {
              const state = get();
              if (state.currentConvId !== convId) return;
              const anchor = latestPersistedMessageId(state.messages);
              const resp = await apiListMessages(convId, {
                limit: MESSAGE_PAGE_LIMIT,
                since: anchor,
                include: ["tasks"],
              });
              if (get().currentConvId !== convId) return;
              if (
                messageId &&
                !(resp.items ?? []).some((m) => m.id === messageId) &&
                !get().messages.some((m) => m.id === messageId)
              ) {
                throw new Error(
                  "appended message missing from incremental response",
                );
              }
              const built = buildMessageListState(
                convId,
                resp,
                get().generations,
                get().imagesById,
              );
              set((s) => {
                if (s.currentConvId !== convId) return s;
                return {
                  messages: mergeMessagesById(s.messages, built.messages),
                  generations: built.generations,
                  imagesById: built.imagesById,
                };
              });
            } catch (err) {
              logWarn("conv.message.appended incremental sync failed", {
                scope: "chat-sse",
                extra: {
                  convId,
                  messageId,
                  err: errorToMessage(err),
                },
              });
              try {
                await get().loadHistoricalMessages(convId);
              } catch (reloadErr) {
                logWarn("conv.message.appended fallback reload failed", {
                  scope: "chat-sse",
                  extra: {
                    convId,
                    messageId,
                    err: errorToMessage(reloadErr),
                  },
                });
              }
            }
          })();
          break;
        }

        case "conv.renamed":
        case "user.notice":
          // 由上层消费（例如 sidebar / toast）
          break;

        default:
          // 未识别事件：忽略
          break;
        }
      } catch (err) {
        logWarn("dropped SSE event after store handler error", {
          scope: "chat-sse",
          extra: { event: eventName, err: errorToMessage(err) },
        });
      }
    },

    async refreshCompletionText(completionId, opts) {
      try {
        const fresh = (await apiGetTask(
          "completions",
          completionId,
          { signal: opts?.signal },
        )) as BackendCompletion;
        flushCompletionStreamPatches();
        const snapshotNow = Date.now();
        set((s) => ({
          messages: applyCompletionSnapshot(
            s.messages,
            completionId,
            fresh,
            snapshotNow,
          ),
        }));
      } catch (err) {
        if (opts?.signal && isAbortRequest(err, opts.signal)) return;
        logWarn("refreshCompletionText failed", {
          scope: "chat-poll",
          code: err instanceof ApiError ? err.code : undefined,
          extra: { completionId, err: errorToMessage(err) },
        });
        throw err;
      }
    },

    async pollInflightTasks(opts) {
      const state = get();

      // 收集所有 in-flight 任务 id（排除乐观占位）
      const inflightGenIds: string[] = [];
      const allowedGenIds =
        opts?.generationIds != null ? new Set(opts.generationIds) : null;
      for (const g of Object.values(state.generations)) {
        if (
          (g.status === "queued" || g.status === "running") &&
          !g.id.startsWith("opt-") &&
          (!allowedGenIds || allowedGenIds.has(g.id))
        ) {
          inflightGenIds.push(g.id);
        }
      }
      const inflightCompIds: string[] = [];
      const allowedCompIds =
        opts?.completionIds != null ? new Set(opts.completionIds) : null;
      for (const m of state.messages) {
        if (m.role !== "assistant") continue;
        if (m.status !== "pending" && m.status !== "streaming") continue;
        if (
          m.completion_id &&
          !m.completion_id.startsWith("opt-") &&
          (!allowedCompIds || allowedCompIds.has(m.completion_id))
        ) {
          inflightCompIds.push(m.completion_id);
        }
      }
      const maxChecks =
        typeof opts?.maxChecks === "number" && Number.isFinite(opts.maxChecks)
          ? Math.max(0, Math.trunc(opts.maxChecks))
          : undefined;
      const checkGenIds =
        maxChecks === undefined ? inflightGenIds : inflightGenIds.slice(0, maxChecks);
      const remainingChecks =
        maxChecks === undefined ? undefined : Math.max(0, maxChecks - checkGenIds.length);
      const checkCompIds =
        remainingChecks === undefined
          ? inflightCompIds
          : inflightCompIds.slice(0, remainingChecks);
      if (checkGenIds.length === 0 && checkCompIds.length === 0) return;

      // 并行拉最新状态；不阻塞彼此，单条失败容忍
      let needRefetchConvId: string | null = null;
      const checks: Array<Promise<void>> = [];

      for (const gid of checkGenIds) {
        checks.push(
          (async () => {
            try {
              if (opts?.signal?.aborted) return;
              const fresh = (await apiGetTask(
                "generations",
                gid,
                { signal: opts?.signal },
              )) as BackendGeneration;
              const freshExplainability = generationExplainabilityFromBackend(fresh);
              const freshTaskMeta = generationTaskMetaFromBackend(fresh);
              const local = get().generations[gid];
              if (!local) return;
              const isTerminal =
                fresh.status === "succeeded" ||
                fresh.status === "failed" ||
                fresh.status === "canceled";
              const localIsInflight =
                local.status === "queued" || local.status === "running";
              if (localIsInflight && isTerminal) {
                // 服务端已 terminal 但本地还卡在进行中——错过了 SSE event
                const owningMsg = get().messages.find(
                  (m) =>
                    m.role === "assistant" &&
                    (m as AssistantMessage).id === fresh.message_id,
                );
                if (owningMsg) {
                  needRefetchConvId = get().currentConvId;
                } else {
                  const finishedAt = fresh.finished_at
                    ? isoToMs(fresh.finished_at)
                    : Date.now();
                  set((s) => ({
                    generations: {
                      ...s.generations,
                      [gid]: {
                        ...local,
                        status: coerceGenerationStatus(
                          fresh.status,
                          local.status,
                        ),
                        stage: coerceGenerationStage(
                          fresh.progress_stage,
                          "finalizing",
                        ),
                        attempt:
                          typeof fresh.attempt === "number" &&
                          Number.isFinite(fresh.attempt)
                            ? fresh.attempt
                            : local.attempt,
                        error_code: fresh.error_code ?? undefined,
                        error_message: fresh.error_message ?? undefined,
                        ...freshExplainability,
                        ...freshTaskMeta,
                        finished_at: finishedAt,
                      },
                    },
                  }));
                }
              } else if (localIsInflight && !isTerminal) {
                const freshStage = coerceGenerationStage(
                  fresh.progress_stage,
                  local.stage,
                );
                const freshStatus = coerceGenerationStatus(
                  fresh.status,
                  local.status,
                );
                if (
                  freshStage !== local.stage ||
                  freshStatus !== local.status ||
                  fresh.attempt !== local.attempt
                ) {
                  set((s) => ({
                    generations: {
                      ...s.generations,
                      [gid]: {
                        ...local,
                        status: freshStatus,
                        stage: freshStage,
                        attempt:
                          typeof fresh.attempt === "number" &&
                          Number.isFinite(fresh.attempt)
                            ? fresh.attempt
                            : local.attempt,
                        error_code: fresh.error_code ?? undefined,
                        error_message: fresh.error_message ?? undefined,
                        ...freshExplainability,
                        ...freshTaskMeta,
                      },
                    },
                  }));
                }
              }
            } catch (err) {
              if (opts?.signal && isAbortRequest(err, opts.signal)) return;
              logWarn("pollInflightTasks generation check failed", {
                scope: "chat-poll",
                code: err instanceof ApiError ? err.code : undefined,
                extra: { generationId: gid, err: errorToMessage(err) },
              });
            }
          })(),
        );
      }

      for (const cid of checkCompIds) {
        checks.push(
          (async () => {
            try {
              if (opts?.signal?.aborted) return;
              const fresh = (await apiGetTask(
                "completions",
                cid,
                { signal: opts?.signal },
              )) as BackendCompletion;
              flushCompletionStreamPatches();
              const snapshotNow = Date.now();
              set((s) => ({
                messages: applyCompletionSnapshot(
                  s.messages,
                  cid,
                  fresh,
                  snapshotNow,
                ),
              }));
              const owningMsg = get().messages.find(
                (m) =>
                  m.role === "assistant" &&
                  (m as AssistantMessage).completion_id === cid,
              ) as AssistantMessage | undefined;
              if (
                owningMsg &&
                (owningMsg.status === "pending" ||
                  owningMsg.status === "streaming") &&
                (fresh.status === "succeeded" ||
                  fresh.status === "failed" ||
                  fresh.status === "canceled")
              ) {
                needRefetchConvId = get().currentConvId;
              }
            } catch (err) {
              if (opts?.signal && isAbortRequest(err, opts.signal)) return;
              logWarn("pollInflightTasks completion check failed", {
                scope: "chat-poll",
                code: err instanceof ApiError ? err.code : undefined,
                extra: { completionId: cid, err: errorToMessage(err) },
              });
            }
          })(),
        );
      }

      await Promise.all(checks);

      if (needRefetchConvId && !opts?.signal?.aborted) {
        // refetch 拉完整状态（含 image / 最终 text）；
        // loadHistoricalMessages 内部有 conv 切换防护，跨会话切换不会串数据
        try {
          await get().loadHistoricalMessages(needRefetchConvId);
        } catch (err) {
          logWarn("pollInflightTasks refetch failed", {
            scope: "chat-poll",
            code: err instanceof ApiError ? err.code : undefined,
            extra: { convId: needRefetchConvId, err: errorToMessage(err) },
          });
        }
      }
    },

    async hydrateActiveTasks(opts) {
      let resp: Awaited<ReturnType<typeof listMyActiveTasks>>;
      try {
        resp = await listMyActiveTasks({ signal: opts?.signal });
      } catch (err) {
        if (opts?.signal && isAbortRequest(err, opts.signal)) return;
        logWarn("hydrateActiveTasks fetch failed", {
          scope: "chat-hydrate",
          code: err instanceof ApiError ? err.code : undefined,
          extra: { err: errorToMessage(err) },
        });
        return;
      }
      const incoming = resp.generations ?? [];
      if (incoming.length === 0) return;
      set((s) => {
        const existing = s.generations;
        const next: Record<string, Generation> = { ...existing };
        let changed = false;
        for (const g of incoming) {
          const prev = existing[g.id];
          const generationExplainability = generationExplainabilityFromBackend(g);
          const generationTaskMeta = generationTaskMetaFromBackend(g);
          // 已知 task 且仍 inflight：本地权威，避免 hydrate 覆盖刚收到的 SSE 增量
          if (
            prev &&
            (prev.status === "queued" || prev.status === "running")
          ) {
            continue;
          }
          const built: Generation = {
            id: g.id,
            message_id: g.message_id,
            parent_generation_id: g.parent_generation_id ?? prev?.parent_generation_id ?? null,
            action: g.action === "edit" ? "edit" : "generate",
            prompt: typeof g.prompt === "string" ? g.prompt : "",
            size_requested:
              typeof g.size_requested === "string" ? g.size_requested : "auto",
            aspect_ratio: coerceAspectRatio(
              g.aspect_ratio,
              prev?.aspect_ratio,
            ),
            input_image_ids: stringArray(g.input_image_ids),
            primary_input_image_id:
              typeof g.primary_input_image_id === "string"
                ? g.primary_input_image_id
                : null,
            status: coerceGenerationStatus(g.status, prev?.status ?? "queued"),
            stage: coerceGenerationStage(g.progress_stage, prev?.stage ?? "queued"),
            image: prev?.image,
            error_code: g.error_code ?? undefined,
            error_message: g.error_message ?? undefined,
            ...generationExplainability,
            ...generationTaskMeta,
            attempt:
              typeof g.attempt === "number" && Number.isFinite(g.attempt)
                ? g.attempt
                : (prev?.attempt ?? 0),
            started_at: isoToMs(g.started_at),
            finished_at: g.finished_at ? isoToMs(g.finished_at) : undefined,
          };
          next[g.id] = built;
          changed = true;
        }
        return changed ? { generations: next } : s;
      });
    },

    reset: () => {
      _runtimeFastDefault = null;
      _fastTouchedByUser = false;
      clearCompletionStreamBuffer();
      abortAllHistoryRequests();
      abortAllSendRequests();
      if (_base64EvictionTimer) {
        clearTimeout(_base64EvictionTimer);
        _base64EvictionTimer = null;
      }
      clearConversationIndexes();
      if (typeof window !== "undefined") {
        window.dispatchEvent(new Event("lumen:chat-store-reset"));
      }
      set(createInitialChatData());
    },
  }));
}

type ChatStoreHook = ReturnType<typeof createChatStore>;

let browserChatStore: ChatStoreHook | null = null;

function getChatStore(): ChatStoreHook {
  if (typeof window === "undefined") {
    clearCompletionStreamBuffer();
    clearConversationIndexes();
    return createChatStore();
  }
  if (!browserChatStore) {
    browserChatStore = createChatStore();
  }
  return browserChatStore;
}

export const useChatStore: ChatStoreHook = new Proxy(
  ((...args: Parameters<ChatStoreHook>) =>
    getChatStore()(...args)) as ChatStoreHook,
  {
    get(_target, prop, receiver) {
      return Reflect.get(getChatStore(), prop, receiver);
    },
    set(_target, prop, value, receiver) {
      return Reflect.set(getChatStore(), prop, value, receiver);
    },
    has(_target, prop) {
      return prop in getChatStore();
    },
    ownKeys() {
      return Reflect.ownKeys(getChatStore());
    },
    getOwnPropertyDescriptor(_target, prop) {
      return Reflect.getOwnPropertyDescriptor(getChatStore(), prop);
    },
  },
) as ChatStoreHook;

export function disposeChatStoreRuntime(): void {
  clearCompletionStreamBuffer();
  abortAllHistoryRequests();
  abortAllSendRequests();
  if (_base64EvictionTimer) {
    clearTimeout(_base64EvictionTimer);
    _base64EvictionTimer = null;
  }
}

const hot = (
  import.meta as ImportMeta & { hot?: { dispose: (cb: () => void) => void } }
).hot;
if (hot) {
  hot.dispose(disposeChatStoreRuntime);
}
