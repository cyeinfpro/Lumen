"use client";

// Lumen 会话 store（后端接入版）
// 对齐 DESIGN.md §13.1 / §22.9（消息 → 任务 → 图像 三层状态机）。
// 乐观插入用户 msg + pending 助手 msg → POST /conversations/:id/messages →
// 用返回的 user_message / assistant_message / generation_ids 校正 → SSE 流式更新。
//
// 本文件不直接调用上游网关；所有网络交互走 apiClient。

import { create } from "zustand";
import { uuid } from "@/lib/utils";
import { logWarn } from "@/lib/logger";
import {
  MAX_PROMPT_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  appendPromptWithinLimit,
  clampPromptForRequest,
  isPromptTooLong,
} from "@/lib/promptLimits";
import type {
  AspectRatio,
  AssistantMessage,
  AttachmentImage,
  CompletionToolCall,
  Generation,
  GeneratedImage,
  ImageParams,
  Intent,
  Message,
  Quality,
  RenderQualityChoice,
  SizeMode,
  UserMessage,
} from "@/lib/types";
import {
  PRESET,
  defaultOutputCompression,
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
}

interface ChatState {
  // 会话上下文
  currentUserId: string | null;
  currentConvId: string | null;
  setCurrentUser: (id: string | null) => void;
  setCurrentConv: (id: string | null) => void;

  // 数据
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  messagesCursor: string | null;
  messagesHasMore: boolean;
  messagesLoading: boolean;
  messagesError: string | null;

  // Composer 层面向用户暴露的最近一次错误（如 sendMessage 失败、会话创建失败）。
  // 由 PromptComposer 渲染到红色提示条，避免错误被静默吞掉。
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
  clearComposer: () => void;
  promoteImageToReference: (imageId: string) => void;

  // —— async actions ——
  // 把本地 File 上传到后端 → 返回 AttachmentImage（含后端 image_id）
  uploadAttachment: (file: File) => Promise<AttachmentImage>;
  // 把 composer 当前状态作为一次发送：乐观插入 + POST → 校正
  sendMessage: (opts?: {
    intentOverride?: Exclude<Intent, "auto">;
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

  // —— 内部 / SSE ——
  appendUserMessage: (msg: UserMessage) => void;
  appendAssistantMessage: (msg: AssistantMessage) => void;
  upsertGeneration: (gen: Generation) => void;
  attachImageToGeneration: (generationId: string, img: GeneratedImage) => void;
  applySSEEvent: (eventName: string, data: unknown) => void;

  // —— 自愈：扫描在途任务，发现服务端已 terminal 但本地仍 running 时主动 refetch ——
  // 用途：刷新瞬间 worker 完成 → SSE 事件已发但浏览器还没订上 → 错过事件 → 永远卡 running
  pollInflightTasks: () => Promise<void>;
  // —— 用户级中心任务列表：从 /tasks/mine/active 拉取当前用户全部进行中任务，
  //     一次性 merge 到 store.generations，让 GlobalTaskTray 显示**所有会话**的任务（即便
  //     当前会话没访问过也能看到）。SSE onOpen / 在线恢复时调用。
  hydrateActiveTasks: () => Promise<void>;
  refreshCompletionText: (completionId: string) => Promise<void>;
  reset: () => void;
}

const DEFAULT_PARAMS: ImageParams = {
  aspect_ratio: "16:9",
  size_mode: "fixed",
  quality: "2k",
  render_quality: "medium",
  count: 1,
};

const IMAGE_COUNT_MIN = 1;
const IMAGE_COUNT_MAX = 8;
const MESSAGE_PAGE_LIMIT = 50;
const BASE64_EVICTION_DELAY_MS = 60_000;
const BASE64_EVICTION_MIN_CHARS = 1024;
const COMPLETION_STREAM_FLUSH_MS = 64;
const CONVERSATION_INDEX_LIMIT = 5_000;
const OPTIMISTIC_ALIAS_TTL_MS = 120_000;

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
    : "medium";
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
};

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
  return {
    ...DEFAULT_COMPOSER,
    attachments: [],
    params: { ...DEFAULT_PARAMS },
  };
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
export function errorCodeToMessage(code: string): string | null {
  switch (code) {
    case "network_error":
      return "网络异常，请稍后重试";
    case "upstream_timeout":
      return "服务繁忙，请稍后重试";
    case "rate_limited":
      return "操作过于频繁，请稍后再试";
    case "unauthorized":
      return "登录已过期，请重新登录";
    case "forbidden":
      return "没有访问权限";
    case "quota_exceeded":
      return "上游服务暂时拥挤，请稍后重试";
    case "upstream_error":
      return "上游服务异常，请稍后重试";
    case "prompt_too_long":
      return PROMPT_TOO_LONG_MESSAGE;
    case "invalid_request":
      return "请求内容不合法，请检查输入后重试";
    case "validation_error":
      return "输入内容不合法";
    case "not_found":
      return "请求的资源不存在";
    case "client_exception":
      return "客户端异常，请刷新重试";
    default:
      return null;
  }
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

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
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

const TOOL_STATUSES = new Set<CompletionToolCall["status"]>([
  "queued",
  "running",
  "succeeded",
  "failed",
]);

function coerceCompletionToolCalls(value: unknown): CompletionToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): CompletionToolCall[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const id = typeof raw.id === "string" && raw.id ? raw.id : "";
    if (!id) return [];
    const status =
      typeof raw.status === "string" &&
      TOOL_STATUSES.has(raw.status as CompletionToolCall["status"])
        ? (raw.status as CompletionToolCall["status"])
        : "running";
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

function loadBrowserImage(
  file: File,
): Promise<{ img: HTMLImageElement; url: string }> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => resolve({ img, url });
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("读取图片失败"));
    };
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
): Promise<{ blob: Blob; mime: "image/webp" | "image/jpeg" }> {
  let best: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null = null;

  for (const mime of ["image/webp", "image/jpeg"] as const) {
    const canvas = drawImageToCanvas(
      img,
      maxSide,
      mime === "image/jpeg" ? "#fff" : null,
    );
    for (const quality of ENCODE_QUALITIES) {
      const blob = await canvasToBlob(canvas, mime, quality);
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

async function compressToMaxDim(file: File): Promise<File> {
  const { img, url } = await loadBrowserImage(file);
  try {
    const { naturalWidth: w, naturalHeight: h } = img;
    if (!w || !h) throw new Error("读取图片失败");

    const supportedOriginal = UPLOAD_MIME.has(file.type);
    const oversizedDimensions = Math.max(w, h) > MAX_DIM;
    const oversizedBytes = file.size > UPLOAD_TARGET_BYTES;
    if (supportedOriginal && !oversizedDimensions && !oversizedBytes) {
      return file;
    }

    let maxSide = Math.min(MAX_DIM, Math.max(w, h));
    let encoded: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null =
      null;
    for (let attempt = 0; attempt < 6; attempt++) {
      encoded = await encodeImageForUpload(img, maxSide);
      if (
        encoded.blob.size <= UPLOAD_TARGET_BYTES ||
        maxSide <= MIN_COMPRESSED_DIM
      ) {
        break;
      }
      maxSide = nextCompressedSide(maxSide, encoded.blob.size);
    }

    if (!encoded) throw imageEncodeError();
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
const _generationIdAliases = new Map<
  string,
  { optimisticId: string; expiresAt: number }
>();
const _completionMessageAliases = new Map<
  string,
  { optimisticMessageId: string; expiresAt: number }
>();

interface PendingCompletionStreamPatch {
  msgId?: string;
  compId?: string;
  text: string;
  thinking: string;
}

const _completionStreamPatches = new Map<
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

function pruneAliases(now?: number): void {
  const effectiveNow = now ?? Date.now();
  for (const [id, alias] of _generationIdAliases) {
    if (alias.expiresAt <= effectiveNow) _generationIdAliases.delete(id);
  }
  for (const [id, alias] of _completionMessageAliases) {
    if (alias.expiresAt <= effectiveNow) _completionMessageAliases.delete(id);
  }
  pruneMapToLimit(_generationIdAliases);
  pruneMapToLimit(_completionMessageAliases);
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
  return _completionMessageAliases.get(id)?.optimisticMessageId;
}

function completionStreamPatchKey(
  msgId: string | undefined,
  compId: string | undefined,
): string | null {
  return compId ?? msgId ?? null;
}

function flushCompletionStreamPatches(): void {
  if (_completionStreamTimer) {
    clearTimeout(_completionStreamTimer);
    _completionStreamTimer = null;
  }
  if (_completionStreamPatches.size === 0) return;

  const patches = Array.from(_completionStreamPatches.values());
  _completionStreamPatches.clear();
  const now = Date.now();

  useChatStore.setState((s) => {
    let changed = false;
    const messages = s.messages.map((m) => {
      if (m.role !== "assistant") return m;
      let next: AssistantMessage | null = null;

      for (const patch of patches) {
        const matches =
          (patch.msgId != null && m.id === patch.msgId) ||
          (patch.compId != null && m.completion_id === patch.compId);
        if (!matches) continue;
        next ??= { ...m };
        if (patch.text) next.text = (next.text ?? "") + patch.text;
        if (patch.thinking)
          next.thinking = (next.thinking ?? "") + patch.thinking;
      }

      if (!next) return m;
      next.status = "streaming";
      next.stream_started_at ??= now;
      next.last_delta_at = now;
      changed = true;
      return next;
    });

    return changed ? { messages } : s;
  });
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
  const current = _completionStreamPatches.get(key) ?? {
    msgId,
    compId,
    text: "",
    thinking: "",
  };
  current.msgId = current.msgId ?? msgId;
  current.compId = current.compId ?? compId;
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
}

function clearConversationIndexes(): void {
  _messageConvIds.clear();
  _generationConvIds.clear();
  _imageConvIds.clear();
  _generationIdAliases.clear();
  _completionMessageAliases.clear();
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
      const merged: Generation = {
        id: g.id,
        message_id: g.message_id,
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
        image: builtImage ?? existing?.image,
        error_code: g.error_code ?? undefined,
        error_message: g.error_message ?? undefined,
        attempt:
          typeof g.attempt === "number" && Number.isFinite(g.attempt)
            ? g.attempt
            : (existing?.attempt ?? 0),
        started_at: isoToMs(g.started_at),
        finished_at: g.finished_at ? isoToMs(g.finished_at) : undefined,
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
    // 切换会话：只清 messages（UI 级），保留 generations / imagesById（全局任务池）。
    // 原因：切走时若后台还有 generation 在跑，它的 Generation 记录不能丢，否则：
    //   - GlobalTaskTray 不再显示该任务
    //   - SSE 事件到达时 s.generations[id] 不存在，更新会 no-op
    //   - 切回会话时渲染不出进度/结果卡片
    // loadHistoricalMessages 会反查 generations pool 把 generation_id 重新绑回 assistant msg。
    setCurrentConv: (id) => {
      const previousConvId = get().currentConvId;
      if (previousConvId === id) return;

      // 会话切换时取消旧历史拉取和发送请求，避免旧响应回写到新会话。
      abortAllHistoryRequests();
      abortAllSendRequests();

      set({
        currentConvId: id,
        messages: [],
        messagesCursor: null,
        messagesHasMore: false,
        messagesLoading: false,
        messagesError: null,
      });
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
    setFast: (v) => set((s) => ({ composer: { ...s.composer, fast: v } })),
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
        if (s.composer.attachments.some((a) => a.id === att.id)) return s;
        return {
          composer: {
            ...s.composer,
            attachments: [...s.composer.attachments, att],
          },
        };
      }),
    removeAttachment: (id) =>
      set((s) => ({
        composer: {
          ...s.composer,
          attachments: s.composer.attachments.filter((a) => a.id !== id),
        },
      })),
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
        composer: {
          ...s.composer,
          attachments: [att, ...s.composer.attachments],
          mode: "image",
        },
      }));
    },

    // —— 上传附件：先上后端拿到 image_id，再作为 attachment 挂到 composer ——
    async uploadAttachment(file) {
      const compressed = await compressToMaxDim(file);
      const uploaded = await apiUploadImage(compressed);
      const att: AttachmentImage = {
        id: uploaded.id, // 使用后端返回的 image_id（后续 postMessage 直接用）
        kind: "upload",
        data_url: uploaded.url?.startsWith("data:")
          ? uploaded.url
          : imageBinaryUrl(uploaded.id),
        mime: uploaded.mime ?? file.type,
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
      set({
        messagesLoading: true,
        messagesError: null,
        ...(loadMore ? {} : { messagesCursor: null, messagesHasMore: false }),
      });

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
          return {
            messages: nextMessages,
            generations: built.generations,
            imagesById: built.imagesById,
            messagesCursor: nextCursor,
            messagesHasMore: Boolean(nextCursor) && gotNewMessages,
            messagesLoading: false,
            messagesError: null,
          };
        });
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
      } = state.composer;
      const params = normalizeImageParams(rawParams);
      const text = rawText.trim();
      if (!text && attachments.length === 0) {
        untrackSendRequest();
        return;
      }
      if (isPromptTooLong(text)) {
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

      // 1) 乐观插入 user msg + pending assistant msg
      const optimisticUserId = `opt-user-${uuid()}`;
      const optimisticAssistantId = `opt-asst-${uuid()}`;
      const imageCount = isImage ? clampImageCount(params.count) : 0;
      const optimisticGenIds = isImage
        ? Array.from({ length: imageCount }, () => `opt-gen-${uuid()}`)
        : [];
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
              prompt: text,
              size_requested: sizeRequested,
              aspect_ratio: params.aspect_ratio,
              input_image_ids: attachments.map((a) => a.id),
              primary_input_image_id: attachments[0]?.id ?? null,
              status: "queued" as const,
              stage: "queued" as const,
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
        if (fast) cp.fast = true;
        if (webSearch) cp.web_search = true;
        if (fileSearch) cp.file_search = true;
        if (codeInterpreter) cp.code_interpreter = true;
        if (imageGeneration) cp.image_generation = true;
        return Object.keys(cp).length > 0 ? cp : undefined;
      })();

      const body: PostMessageIn = {
        idempotency_key: uuid(),
        text,
        // generated 参考图的 a.id 是本地 uuid（用于 composer 增删管理），真实后端
        // image_id 在 source_image_id；upload 路径下两者相同（id = 后端 image_id）。
        attachment_image_ids: attachments.map((a) => a.source_image_id ?? a.id),
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
              const q = _q ?? "2k";
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
        const realUser = adaptBackendUserMessage(
          out.user_message,
          attachments,
          params,
          intent,
        );
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
        const failedAt = Date.now();
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
        // chat/vision 没有 generation 卡，把错误写进助手气泡 text；
        // image 场景下留空 text，由 GenerationView 的红色失败卡承担渲染，避免重复
        set((s) => ({
          composerError: uiErr,
          messages: s.messages.map((m) => {
            if (m.id === optimisticAssistantId && m.role === "assistant") {
              return {
                ...m,
                status: "failed",
                text: isImage ? m.text : uiErr,
              } as AssistantMessage;
            }
            return m;
          }),
          generations:
            optimisticGenIds.length > 0
              ? {
                  ...s.generations,
                  ...Object.fromEntries(
                    optimisticGenIds
                      .filter((id) => s.generations[id])
                      .map((id) => [
                        id,
                        {
                          ...s.generations[id],
                          status: "failed" as const,
                          stage: "finalizing" as const,
                          error_code: code,
                          error_message: message,
                          finished_at: failedAt,
                        },
                      ]),
                  ),
                }
              : s.generations,
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

        // 同时为 image intent 占位一个 queued generation，让 GenerationView 立刻显示骨架
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
          count: 1,
          fast: false,
          render_quality: "medium",
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
      const rerollRenderQuality = "medium";

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
          count: 1,
          fast: false,
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

    appendUserMessage: (msg) => {
      const convId = get().currentConvId;
      if (convId) setBounded(_messageConvIds, msg.id, convId);
      set((s) => ({ messages: [...s.messages, msg] }));
    },
    appendAssistantMessage: (msg) => {
      const convId = get().currentConvId;
      if (convId) setBounded(_messageConvIds, msg.id, convId);
      set((s) => ({ messages: [...s.messages, msg] }));
    },
    upsertGeneration: (gen) => {
      const convId = _messageConvIds.get(gen.message_id) ?? get().currentConvId;
      if (convId) rememberGenerationForConversation(convId, gen);
      set((s) => ({ generations: { ...s.generations, [gen.id]: gen } }));
    },
    attachImageToGeneration: (generationId, img) => {
      const finishedAt = Date.now();
      set((s) => {
        const gen = s.generations[generationId];
        if (!gen) return s;
        const convId = generationConversationId(s, gen);
        if (convId) setBounded(_imageConvIds, img.id, convId);
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
      const payload = (data ?? {}) as Record<string, unknown>;
      const get_id = (key: string): string | undefined => {
        const v = payload[key];
        return typeof v === "string" ? v : undefined;
      };

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
            const images = payload.images as
              | Array<{
                  image_id?: string;
                  url?: string;
                  data_url?: string;
                  actual_size?: string;
                  display_url?: string;
                  preview_url?: string;
                  thumb_url?: string;
                  mime?: string;
                  parent_image_id?: string | null;
                }>
              | undefined;
            const first = Array.isArray(images) ? images[0] : undefined;
            if (!first?.image_id) {
              logWarn("missing image_id in succeeded payload", {
                scope: "chat-sse",
                extra: { generation_id: id },
              });
              return;
            }
            const src =
              first.data_url ?? first.url ?? imageBinaryUrl(first.image_id);
            let w = 0;
            let h = 0;
            if (typeof first.actual_size === "string") {
              const m = first.actual_size.match(/^(\d+)x(\d+)$/);
              if (m) {
                w = Number(m[1]);
                h = Number(m[2]);
              }
            }
            pendingImage = {
              id: first.image_id,
              data_url: src,
              mime: first.mime,
              display_url:
                first.display_url ??
                imageVariantUrl(first.image_id, "display2048"),
              preview_url:
                first.preview_url ??
                imageVariantUrl(first.image_id, "preview1024"),
              thumb_url:
                first.thumb_url ?? imageVariantUrl(first.image_id, "thumb256"),
              width: w,
              height: h,
              parent_image_id: first.parent_image_id ?? null,
              from_generation_id: id,
              size_requested: "auto",
              size_actual: first.actual_size ?? "unknown",
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
              patch.started_at = 0;
            } else if (eventName === "generation.started") {
              patch.status = "running";
              patch.stage = "understanding";
              patch.started_at = gen.started_at > 0 ? gen.started_at : eventNow;
              const att = payload.attempt;
              if (typeof att === "number") patch.attempt = att;
              patch.retry_eta = undefined;
              patch.retry_error = undefined;
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
              const substage = payload.substage;
              if (
                substage === "provider_selected" ||
                substage === "stream_started" ||
                substage === "partial_received" ||
                substage === "final_received" ||
                substage === "processing" ||
                substage === "storing"
              ) {
                patch.substage = substage;
              }
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
              if (!(gen.started_at > 0)) patch.started_at = eventNow;
            } else if (eventName === "generation.succeeded" && pendingImage) {
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
              patch.finished_at = eventNow;
            } else if (eventName === "generation.failed") {
              patch.status = "failed";
              patch.stage = "finalizing";
              patch.error_code = get_id("code") ?? "generation_failed";
              patch.error_message = get_id("message") ?? "生成失败";
              patch.finished_at = eventNow;
            } else if (eventName === "generation.retrying") {
              patch.status = "queued";
              patch.stage = "queued";
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
            const images = payload.images as
              | Array<{
                  image_id?: string;
                  url?: string;
                  data_url?: string;
                  actual_size?: string;
                  display_url?: string;
                  preview_url?: string;
                  thumb_url?: string;
                  mime?: string;
                }>
              | undefined;
            const first = Array.isArray(images) ? images[0] : undefined;
            if (!first?.image_id || !msgId || !compId) return;
            const src =
              first.data_url ?? first.url ?? imageBinaryUrl(first.image_id);
            let w = 0;
            let h = 0;
            if (typeof first.actual_size === "string") {
              const m = first.actual_size.match(/^(\d+)x(\d+)$/);
              if (m) {
                w = Number(m[1]);
                h = Number(m[2]);
              }
            }
            const genId = completionToolGenerationId(compId);
            const img: GeneratedImage = {
              id: first.image_id,
              data_url: src,
              mime: first.mime,
              display_url:
                first.display_url ??
                imageVariantUrl(first.image_id, "display2048"),
              preview_url:
                first.preview_url ??
                imageVariantUrl(first.image_id, "preview1024"),
              thumb_url:
                first.thumb_url ?? imageVariantUrl(first.image_id, "thumb256"),
              width: w,
              height: h,
              parent_image_id: null,
              from_generation_id: genId,
              size_requested: first.actual_size ?? "auto",
              size_actual: first.actual_size ?? "unknown",
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
    },

    async refreshCompletionText(completionId) {
      try {
        const fresh = (await apiGetTask(
          "completions",
          completionId,
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
        logWarn("refreshCompletionText failed", {
          scope: "chat-poll",
          code: err instanceof ApiError ? err.code : undefined,
          extra: { completionId, err: errorToMessage(err) },
        });
        throw err;
      }
    },

    async pollInflightTasks() {
      const state = get();

      // 收集所有 in-flight 任务 id（排除乐观占位）
      const inflightGenIds: string[] = [];
      for (const g of Object.values(state.generations)) {
        if (
          (g.status === "queued" || g.status === "running") &&
          !g.id.startsWith("opt-")
        ) {
          inflightGenIds.push(g.id);
        }
      }
      const inflightCompIds: string[] = [];
      for (const m of state.messages) {
        if (m.role !== "assistant") continue;
        if (m.status !== "pending" && m.status !== "streaming") continue;
        if (m.completion_id && !m.completion_id.startsWith("opt-")) {
          inflightCompIds.push(m.completion_id);
        }
      }
      if (inflightGenIds.length === 0 && inflightCompIds.length === 0) return;

      // 并行拉最新状态；不阻塞彼此，单条失败容忍
      let needRefetchConvId: string | null = null;
      const checks: Array<Promise<void>> = [];

      for (const gid of inflightGenIds) {
        checks.push(
          (async () => {
            try {
              const fresh = (await apiGetTask(
                "generations",
                gid,
              )) as BackendGeneration;
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
                      },
                    },
                  }));
                }
              }
            } catch (err) {
              logWarn("pollInflightTasks generation check failed", {
                scope: "chat-poll",
                code: err instanceof ApiError ? err.code : undefined,
                extra: { generationId: gid, err: errorToMessage(err) },
              });
            }
          })(),
        );
      }

      for (const cid of inflightCompIds) {
        checks.push(
          (async () => {
            try {
              const fresh = (await apiGetTask(
                "completions",
                cid,
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

      if (needRefetchConvId) {
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

    async hydrateActiveTasks() {
      let resp: Awaited<ReturnType<typeof listMyActiveTasks>>;
      try {
        resp = await listMyActiveTasks();
      } catch (err) {
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
      clearCompletionStreamBuffer();
      abortAllHistoryRequests();
      abortAllSendRequests();
      if (_base64EvictionTimer) {
        clearTimeout(_base64EvictionTimer);
        _base64EvictionTimer = null;
      }
      clearConversationIndexes();
      set(createInitialChatData());
    },
  }));
}

type ChatStoreHook = ReturnType<typeof createChatStore>;
type ChatSelector<T> = (state: ChatState) => T;

let _clientChatStore: ChatStoreHook | null = null;

function getChatStore(): ChatStoreHook {
  if (typeof window === "undefined") {
    return createChatStore();
  }
  _clientChatStore ??= createChatStore();
  return _clientChatStore;
}

function useChatStoreBound(): ChatState;
function useChatStoreBound<T>(selector: ChatSelector<T>): T;
function useChatStoreBound<T>(selector?: ChatSelector<T>): ChatState | T {
  const store = getChatStore();
  return selector ? store(selector) : store();
}

// Browser runtime keeps one interactive store. SSR access gets a fresh store so
// module evaluation cannot share mutable chat state across requests.
export const useChatStore = useChatStoreBound as ChatStoreHook;

Object.defineProperties(useChatStore, {
  getState: { get: () => getChatStore().getState },
  setState: { get: () => getChatStore().setState },
  subscribe: { get: () => getChatStore().subscribe },
  getInitialState: { get: () => getChatStore().getInitialState },
});

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
