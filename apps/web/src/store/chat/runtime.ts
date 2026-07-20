import { logWarn } from "@/lib/logger";
import { PROMPT_TOO_LONG_MESSAGE } from "@/lib/promptLimits";
import type {
  AspectRatio,
  AssistantMessage,
  AttachmentImage,
  Generation,
  GeneratedImage,
  ImageParams,
  Intent,
  Message,
  Quality,
  UserMessage,
} from "@/lib/types";
import { qualityToFixedSize } from "@/lib/sizing";
import { ApiError } from "@/lib/api/http";
import { imageBinaryUrl } from "@/lib/api/images";
import { errorCodeToFullText, recommendedActionsForError } from "@/lib/errors";
import { reduceGenerationLifecycleEvent } from "../chatGenerationEvents";
import { createRequestFence } from "./requestGuards";
import {
  applyCompletionStreamPatches,
  completionStreamPatchKey,
  createCompletionStreamPatch,
  mergeCompletionStreamPatch,
  type PendingCompletionStreamPatch,
} from "./completionStreamPatches";
import { buildBase64EvictionPatch } from "./base64Eviction";
import { DEFAULT_PARAMS } from "./imageParams";
import { optionalRecord as parseOptionalRecord, optionalString, recommendedActionsFromUnknown, ssePayloadRecord as parseSsePayloadRecord } from "./payload";
import {
  assistantHasGeneration,
  generationExplainabilityFromPayload,
  terminalGenerationEventStatus,
  updateGenerationAssistantStatuses,
} from "./generationSlice";
import { cloneConversationHistoryCacheEntry, isEvictableDataUrl, type ConversationHistoryCacheEntry, type MessageListMaterialization } from "./history";
import type { SseIdGetter } from "./completionEvents";
import type { ChatState, ChatStateGetter, ChatStateSetter } from "./types";

type ChatStoreBinding = {
  getState: ChatStateGetter;
  setState: ChatStateSetter;
};

let chatStoreBinding: ChatStoreBinding | null = null;

export function bindChatStoreRuntime(binding: ChatStoreBinding): void {
  chatStoreBinding = binding;
}

function getBoundChatStore(): ChatStoreBinding {
  if (!chatStoreBinding) {
    throw new Error("chat store runtime is not bound");
  }
  return chatStoreBinding;
}

const MESSAGE_PAGE_LIMIT = 50;
const BASE64_EVICTION_DELAY_MS = 60_000;
const COMPLETION_STREAM_FLUSH_MS = 64;
const COMPLETION_PENDING_DELTA_TTL_MS = 10_000;
const COMPLETION_PENDING_DELTA_MAX_ENTRIES = 1_000;
const CONVERSATION_INDEX_LIMIT = 5_000;
const CONVERSATION_HISTORY_CACHE_LIMIT = 32;
const CONVERSATION_HISTORY_CACHE_TTL_MS = 90_000;
const OPTIMISTIC_ALIAS_TTL_MS = 120_000;
const COMPLETION_MESSAGE_ID_TTL_MS = 60 * 60 * 1000;

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

function isImageIntent(
  intent: Exclude<Intent, "auto">,
): intent is "text_to_image" | "image_to_image" {
  return intent === "text_to_image" || intent === "image_to_image";
}

function shouldSkipHistoryLoad(
  state: ChatState,
  convId: string,
  loadMore: boolean,
): boolean {
  if (state.currentConvId !== convId) return true;
  if (!loadMore) return false;
  return state.messagesLoading || !state.messagesHasMore;
}

function rerollIntent(
  generation: Generation,
): "text_to_image" | "image_to_image" {
  return generation.input_image_ids.length > 0
    ? "image_to_image"
    : "text_to_image";
}

function generationParentUserMessageId(
  state: ChatState,
  generationId: string | undefined,
): string | undefined {
  const assistant = generationId
    ? state.messages.find(
        (message): message is AssistantMessage =>
          message.role === "assistant" &&
          assistantHasGeneration(message, generationId),
      )
    : undefined;
  return (
    assistant?.parent_user_message_id ?? lastUserMessageId(state.messages)
  );
}

function buildPendingRegenerationGeneration(input: {
  state: ChatState;
  assistantMessageId: string;
  parentUserId: string;
  newIntent: Exclude<Intent, "auto">;
  newGenerationId: string | undefined;
  oldGeneration: Generation | undefined;
}): Generation | undefined {
  const {
    state,
    assistantMessageId,
    parentUserId,
    newIntent,
    newGenerationId,
    oldGeneration,
  } = input;
  if (!newGenerationId || !isImageIntent(newIntent)) return undefined;
  const source = pendingRegenerationSource(
    state,
    parentUserId,
    oldGeneration,
  );
  return {
    id: newGenerationId,
    message_id: assistantMessageId,
    action: newIntent === "image_to_image" ? "edit" : "generate",
    ...source,
    status: "queued",
    stage: "queued",
    attempt: 0,
    started_at: 0,
  };
}

function pendingRegenerationSource(
  state: ChatState,
  parentUserId: string,
  oldGeneration: Generation | undefined,
): Pick<
  Generation,
  | "prompt"
  | "size_requested"
  | "aspect_ratio"
  | "input_image_ids"
  | "primary_input_image_id"
> {
  const parentUser = state.messages.find(
    (message): message is UserMessage =>
      message.role === "user" && message.id === parentUserId,
  );
  const params = parentUser?.image_params;
  const attachments = parentUser?.attachments ?? [];
  return {
    prompt: parentUser?.text ?? oldGeneration?.prompt ?? "",
    size_requested: pendingGenerationRequestedSize(params),
    aspect_ratio: params?.aspect_ratio ?? DEFAULT_PARAMS.aspect_ratio,
    input_image_ids: attachments.map(attachmentSourceId),
    primary_input_image_id: firstAttachmentSourceId(attachments),
  };
}

function pendingGenerationRequestedSize(
  params: ImageParams | undefined,
): string {
  return params?.size_mode === "fixed" && params.fixed_size
    ? params.fixed_size
    : "auto";
}

function attachmentSourceId(attachment: AttachmentImage): string {
  return attachment.source_image_id ?? attachment.id;
}

function firstAttachmentSourceId(
  attachments: AttachmentImage[],
): string | null {
  const first = attachments[0];
  return first ? attachmentSourceId(first) : null;
}

function generationForImage(
  state: ChatState,
  image: GeneratedImage,
): Generation | undefined {
  return image.from_generation_id
    ? state.generations[image.from_generation_id]
    : undefined;
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
function errorCodeToMessage(code: string): string | null {
  if (code === "prompt_too_long") return PROMPT_TOO_LONG_MESSAGE;
  return errorCodeToFullText(code);
}

function optionalRecord(value: unknown): Record<string, unknown> | undefined {
  return parseOptionalRecord(value, logWarn);
}

function ssePayloadRecord(
  eventName: string,
  data: unknown,
): Record<string, unknown> | null {
  return parseSsePayloadRecord(eventName, data, logWarn);
}

function errorToMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message || err.code;
  if (err instanceof Error) return err.message;
  return "未知错误";
}

function orphanGenerationEventState(
  state: ChatState,
  generationId: string,
  rawGenerationId: string,
  eventName: string,
): ChatState | Partial<ChatState> {
  const terminalStatus = terminalGenerationEventStatus(eventName);
  if (!terminalStatus) {
    logWarn("SSE event for orphan generation", {
      scope: "chat-sse",
      extra: { generation_id: rawGenerationId, event: eventName },
    });
    return state;
  }
  return {
    messages: state.messages.map((message) =>
      message.role === "assistant" &&
      assistantHasGeneration(message, generationId)
        ? ({ ...message, status: terminalStatus } as AssistantMessage)
        : message,
    ),
  };
}

function successfulGenerationEventPatch(
  state: ChatState,
  generation: Generation,
  pendingImage: GeneratedImage,
  payload: Record<string, unknown>,
  eventNow: number,
): Partial<Generation> {
  const generationExplainability =
    generationExplainabilityFromPayload(payload);
  const finalImage: GeneratedImage = {
    ...pendingImage,
    parent_image_id:
      pendingImage.parent_image_id ?? generation.primary_input_image_id,
    size_requested: generation.size_requested,
  };
  const convId = generationConversationId(state, generation);
  if (convId) setBounded(_imageConvIds, finalImage.id, convId);
  return {
    image: finalImage,
    status: "succeeded",
    stage: "finalizing",
    substage: "display_ready",
    retrying: false,
    waiting_provider: false,
    cancelled: false,
    finished_at: eventNow,
    ...generationExplainability,
  };
}

function failedGenerationEventPatch(
  payload: Record<string, unknown>,
  getId: SseIdGetter,
  eventNow: number,
): Partial<Generation> {
  const generationExplainability =
    generationExplainabilityFromPayload(payload);
  const code = getId("code") ?? "generation_failed";
  const retryable = payload.retriable === true;
  return {
    status: "failed",
    stage: "finalizing",
    substage: retryable ? "retryable" : "terminal",
    error_code: code,
    error_message:
      optionalString(
        generationExplainability.diagnostics?.safe_error_summary,
      ) ??
      getId("safe_error_summary") ??
      getId("message") ??
      "生成失败",
    retryable,
    recommended_actions:
      recommendedActionsFromUnknown(payload.recommended_actions) ??
      recommendedActionsForError(code, {
        retryable,
        status: "failed",
      }),
    retrying: false,
    waiting_provider: false,
    cancelled: false,
    finished_at: eventNow,
    ...generationExplainability,
  };
}

function generationEventPatch(
  state: ChatState,
  generation: Generation,
  eventName: string,
  payload: Record<string, unknown>,
  pendingImage: GeneratedImage | undefined,
  getId: SseIdGetter,
  eventNow: number,
): Partial<Generation> {
  const lifecyclePatch = reduceGenerationLifecycleEvent(
    eventName,
    payload,
    generation,
    eventNow,
  );
  if (lifecyclePatch) return lifecyclePatch;
  if (eventName === "generation.succeeded" && pendingImage) {
    return successfulGenerationEventPatch(
      state,
      generation,
      pendingImage,
      payload,
      eventNow,
    );
  }
  return eventName === "generation.failed"
    ? failedGenerationEventPatch(payload, getId, eventNow)
    : {};
}

function applyGenerationEventState(
  state: ChatState,
  input: {
    generationId: string;
    rawGenerationId: string;
    eventName: string;
    payload: Record<string, unknown>;
    pendingImage: GeneratedImage | undefined;
    getId: SseIdGetter;
    eventNow: number;
  },
): ChatState | Partial<ChatState> {
  const generation = state.generations[input.generationId];
  if (!generation) {
    return orphanGenerationEventState(
      state,
      input.generationId,
      input.rawGenerationId,
      input.eventName,
    );
  }
  const patch = generationEventPatch(
    state,
    generation,
    input.eventName,
    input.payload,
    input.pendingImage,
    input.getId,
    input.eventNow,
  );
  const nextGeneration = { ...generation, ...patch };
  const generations = {
    ...state.generations,
    [input.generationId]: nextGeneration,
  };
  const messages = terminalGenerationEventStatus(input.eventName)
    ? updateGenerationAssistantStatuses(
        state.messages,
        input.generationId,
        generations,
      )
    : state.messages;
  const imagesById = patch.image
    ? { ...state.imagesById, [patch.image.id]: patch.image }
    : state.imagesById;
  return { generations, messages, imagesById };
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
const _userSessionFence = createRequestFence();
const _conversationMutationFence = createRequestFence();
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

const _conversationHistoryCache = new Map<
  string,
  ConversationHistoryCacheEntry
>();

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
  let appliedPatchKeys = new Set<string>();
  let appliedPendingCompletionIds = new Set<string>();

  getBoundChatStore().setState((s) => {
    const result = applyCompletionStreamPatches(
      s.messages,
      patchEntries,
      _pendingDeltasByCompletionId,
      now,
    );
    appliedPatchKeys = result.appliedPatchKeys;
    appliedPendingCompletionIds = result.appliedPendingCompletionIds;
    return result.changed ? { messages: result.messages } : s;
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

function clearUserScopedRuntime(): void {
  clearCompletionStreamBuffer();
  abortAllHistoryRequests();
  abortAllSendRequests();
  if (_base64EvictionTimer) {
    clearTimeout(_base64EvictionTimer);
    _base64EvictionTimer = null;
  }
  clearConversationIndexes();
}

function isConversationMutationCurrent(
  currentConvId: string | null,
  expectedConvId: string,
  fenceSnapshot: number,
): boolean {
  return (
    currentConvId === expectedConvId &&
    _conversationMutationFence.isCurrent(fenceSnapshot)
  );
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

function rememberMessageListMaterialization(
  convId: string,
  materialization: MessageListMaterialization,
): void {
  for (const imageId of materialization.imageIds) {
    setBounded(_imageConvIds, imageId, convId);
  }
  for (const generation of materialization.generations) {
    rememberGenerationForConversation(convId, generation);
  }
  for (const item of materialization.completionMessages) {
    rememberCompletionMessage(item.completionId, item.messageId);
  }
  rememberMessagesForConversation(convId, materialization.messages);
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
    getBoundChatStore().setState((s) => {
      const patch = buildBase64EvictionPatch(s, {
        generationConversationId: (generation) =>
          generationConversationId(s, generation),
        imageConversationId: (imageId, _image, generation) =>
          _imageConvIds.get(imageId) ??
          (generation ? generationConversationId(s, generation) : null),
        releaseImage: releaseImageBase64,
      });
      return patch ?? s;
    });
  }, BASE64_EVICTION_DELAY_MS);
}
export {
  MESSAGE_PAGE_LIMIT,
  qualityFromFixedSize,
  isImageIntent,
  shouldSkipHistoryLoad,
  rerollIntent,
  generationParentUserMessageId,
  buildPendingRegenerationGeneration,
  generationForImage,
  isAbortRequest,
  isHistoryRequestAbort,
  errorCodeToMessage,
  optionalRecord,
  ssePayloadRecord,
  errorToMessage,
  applyGenerationEventState,
  abortHistoryRequest,
  abortAllHistoryRequests,
  trackSendRequest,
  abortAllSendRequests,
  setBounded,
  rememberConversationHistoryCache,
  readConversationHistoryCache,
  invalidateConversationHistoryCache,
  rememberGenerationAlias,
  rememberCompletionAlias,
  rememberCompletionMessage,
  generationLookupId,
  completionMessageLookupId,
  flushCompletionStreamPatches,
  queueCompletionStreamPatch,
  clearCompletionStreamBuffer,
  clearConversationIndexes,
  clearUserScopedRuntime,
  isConversationMutationCurrent,
  rememberGenerationForConversation,
  rememberMessageListMaterialization,
  generationConversationId,
  scheduleBase64Eviction,
  _historyAborts,
  _userSessionFence,
  _conversationMutationFence,
  _messageConvIds,
  _generationConvIds,
  _generationIdAliases,
  _imageConvIds,
  _completionMessageAliases,
};

export function disposeChatRuntime(): void {
  clearCompletionStreamBuffer();
  abortAllHistoryRequests();
  abortAllSendRequests();
  clearUserScopedRuntime();
}
