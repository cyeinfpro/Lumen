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
  PROMPT_TOO_LONG_MESSAGE,
  appendPromptWithinLimit,
  clampPromptForRequest,
  isPromptTooLong,
} from "@/lib/promptLimits";
import {
  findInvalidImageMentionLabels,
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
  Generation,
  GeneratedImage,
  ImageParams,
  Intent,
  Message,
  Quality,
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
  imageBinaryUrl,
  imageVariantUrl,
  listMessages as apiListMessages,
  postMessage as apiPostMessage,
  retryTask,
  uploadImage as apiUploadImage,
  type PostMessageIn,
} from "@/lib/apiClient";
import { errorCodeToFullText, recommendedActionsForError } from "@/lib/errors";
import { reduceGenerationLifecycleEvent } from "./chatGenerationEvents";
import { createRequestFence } from "./chat/requestGuards";
import { compressToMaxDim } from "./chat/imageUpload";
import {
  latestPersistedMessageId,
  mergeMessagesById,
} from "./chat/messageReconciliation";
import {
  applyCompletionStreamPatches,
  completionStreamPatchKey,
  createCompletionStreamPatch,
  mergeCompletionStreamPatch,
  type PendingCompletionStreamPatch,
} from "./chat/completionStreamPatches";
import { buildBase64EvictionPatch } from "./chat/base64Eviction";
import {
  DEFAULT_PARAMS,
  clampImageCount,
  normalizeImageParams,
  normalizeRenderQuality,
} from "./chat/imageParams";
import {
  billingMetaFromPayload,
  optionalRecord as parseOptionalRecord,
  optionalRecordArray,
  optionalString,
  parseSizeString,
  recommendedActionsFromUnknown,
  recordBoolean,
  recordNullableString,
  recordString,
  ssePayloadRecord as parseSsePayloadRecord,
  structuredAttachmentsFromComposer,
} from "./chat/payload";
import {
  adaptBackendAssistantMessage,
  adaptBackendUserMessage,
  coerceMemoryWrites,
  optionalAssistantIntent,
} from "./chat/messageAdapters";
import {
  applyCompletionEventToMessage,
  type SseIdGetter,
} from "./chat/completionEvents";
import {
  aggregateGenerationStatus,
  assistantHasGeneration,
  completionToolGenerationId,
  generationExplainabilityFromPayload,
  generationIdsOfMessage,
  terminalGenerationEventStatus,
  updateGenerationAssistantStatuses,
} from "./chat/generationSlice";
import {
  buildMessageListState,
  cloneConversationHistoryCacheEntry,
  isEvictableDataUrl,
  makeConversationHistoryCacheEntry,
  type ConversationHistoryCacheEntry,
  type MessageListMaterialization,
} from "./chat/history";
import {
  cloneComposerState,
  createComposerActions,
  createComposerState,
  didPromptNeedTrimming,
  hasComposerContent,
  inpaintAspectRatio,
  inpaintValidationError,
  isResetComposerDraft,
  isRetryComposerDraft,
  isTemporaryInpaintComposerDraft,
  resolveIntent,
} from "./chat/composerSlice";
import { createTaskRecoveryActions } from "./chat/taskRecovery";
import type {
  ChatDataSlice,
  ChatState,
  ComposerState,
} from "./chat/types";

export type { ReasoningEffort } from "./chat/types";

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

let _runtimeFastDefault: boolean | null = null;
let _fastTouchedByUser = false;

function createInitialComposer(): ComposerState {
  return createComposerState(_runtimeFastDefault);
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

  useChatStore.setState((s) => {
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
    useChatStore.setState((s) => {
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

function createChatStore() {
  return create<ChatState>((set, get) => ({
    ...createInitialChatData(),
    setCurrentUser: (id) => {
      const previousUserId = get().currentUserId;
      if (previousUserId === id) return;
      if (previousUserId === null && id !== null) {
        set({ currentUserId: id });
        return;
      }
      _userSessionFence.advance();
      _conversationMutationFence.advance();
      clearUserScopedRuntime();
      set({ ...createInitialChatData(), currentUserId: id });
    },
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
      _conversationMutationFence.advance();
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
        imagesById: cached
          ? { ...s.imagesById, ...cached.imagesById }
          : s.imagesById,
        messagesCursor: cached?.messagesCursor ?? null,
        messagesHasMore: cached?.messagesHasMore ?? false,
        messagesLoading: false,
        messagesError: null,
      }));
      clearCompletionStreamBuffer();
      scheduleBase64Eviction();
    },

    ...createComposerActions(set, get, {
      createInitialComposer,
      markFastTouched: () => {
        _fastTouchedByUser = true;
      },
    }),

    // —— 上传附件：先上后端拿到 image_id，再作为 attachment 挂到 composer ——
    async uploadAttachment(file, opts = {}) {
      const compressed = await compressToMaxDim(file, {
        maxSourceBytes: MAX_UPLOAD_SOURCE_BYTES,
        maxSourceMessage: maxUploadSourceMessage(),
        signal: opts.signal,
      });
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
      if (shouldSkipHistoryLoad(snapshot, convId, loadMore)) return;

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
          resp,
          get().generations,
          get().imagesById,
        );
        rememberMessageListMaterialization(convId, built.materialization);
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
          const nextMessages = mergeMessagesById(s.messages, built.messages);
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
      if (!hasComposerContent(initialComposer)) {
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
      const isImage = isImageIntent(intent);
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
      const optimisticGenIds = isImage
        ? Array.from(
            { length: clampImageCount(params.count) },
            () => `opt-gen-${uuid()}`,
          )
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
              const renderQuality = normalizeRenderQuality(
                renderQualityOverride,
              );
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
          isResetComposerDraft(s.composer, composerToSend)
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
        const genId =
          genIds.find((id) => {
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
      const composerSnapshot = cloneComposerState(get().composer);
      const retryAttachmentIds = userMsg.attachments.map(
        (attachment) => attachment.id,
      );
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
      const retryComposer = cloneComposerState(get().composer);
      try {
        await get().sendMessage({
          intentOverride: asst.intent_resolved,
          restoreComposerOnFailure: false,
        });
      } finally {
        const cur = get().composer;
        const isRetryDraft = isRetryComposerDraft(
          cur,
          retryText,
          retryAttachmentIds,
          retryComposer,
        );
        if (isResetComposerDraft(cur, retryComposer) || isRetryDraft) {
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
      const mutationFence = _conversationMutationFence.snapshot();
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
        if (
          !isConversationMutationCurrent(
            get().currentConvId,
            convId,
            mutationFence,
          )
        ) {
          return;
        }

        const isImage = isImageIntent(newIntent);
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
        const pendingGen = buildPendingRegenerationGeneration({
          state,
          assistantMessageId: out.assistant_message_id,
          parentUserId,
          newIntent,
          newGenerationId: newGenId,
          oldGeneration: oldGen,
        });
        if (pendingGen) rememberGenerationForConversation(convId, pendingGen);

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
        if (
          !isConversationMutationCurrent(
            get().currentConvId,
            convId,
            mutationFence,
          )
        ) {
          return;
        }
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
      const mutationFence = _conversationMutationFence.snapshot();
      const img = state.imagesById[imageId];
      if (!img) return;
      const gen = generationForImage(state, img);
      const aspect = (gen?.aspect_ratio ??
        DEFAULT_PARAMS.aspect_ratio) as AspectRatio;
      const preset = PRESET[aspect] ?? PRESET[DEFAULT_PARAMS.aspect_ratio];
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
      if (didPromptNeedTrimming(originalPrompt, upscaleInstruction)) {
        logWarn("upscale prompt trimmed to request limit", {
          scope: "chat",
          code: "prompt_too_long",
          extra: {
            originalLength: originalPrompt.length,
            finalLength: upscaleText.length,
          },
        });
      }

      const parentMsgId = generationParentUserMessageId(
        state,
        img.from_generation_id,
      );
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
      if (
        !isConversationMutationCurrent(
          get().currentConvId,
          convId,
          mutationFence,
        )
      ) {
        return;
      }

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
      const mutationFence = _conversationMutationFence.snapshot();
      const img = state.imagesById[imageId];
      if (!img) return;
      const genId = img.from_generation_id;
      if (!genId) return;
      const gen = state.generations[genId];
      if (!gen) return;

      const parentMsgId = generationParentUserMessageId(state, genId);
      if (!parentMsgId) return;

      const hasInput = gen.input_image_ids.length > 0;
      const intent = rerollIntent(gen);
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
      if (
        !isConversationMutationCurrent(
          get().currentConvId,
          convId,
          mutationFence,
        )
      ) {
        return;
      }

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
      const convId = get().currentConvId;
      if (!convId) {
        const msg = "当前没有活动会话";
        set({ composerError: msg });
        throw new Error(msg);
      }
      const mutationFence = _conversationMutationFence.snapshot();
      const text = prompt.trim();
      const validationError = inpaintValidationError(
        text,
        sourceImageId,
        sourceSrc,
      );
      if (validationError) {
        set({ composerError: validationError });
        throw new Error(validationError);
      }

      let maskUploaded;
      try {
        const maskFile = new File([maskBlob], "mask.png", {
          type: "image/png",
        });
        maskUploaded = await apiUploadImage(maskFile);
      } catch (err) {
        if (
          !isConversationMutationCurrent(
            get().currentConvId,
            convId,
            mutationFence,
          )
        ) {
          return;
        }
        const msg = err instanceof Error ? err.message : "mask 上传失败";
        logWarn("inpaint mask upload failed", {
          scope: "inpaint",
          extra: { msg },
        });
        set({ composerError: `局部修改失败：${msg}` });
        throw err instanceof Error ? err : new Error(msg);
      }
      if (
        !isConversationMutationCurrent(
          get().currentConvId,
          convId,
          mutationFence,
        )
      ) {
        return;
      }

      const backup = cloneComposerState(get().composer);
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
      const inferredAspect = inpaintAspectRatio(sourceWidth, sourceHeight);

      set((s) => {
        return {
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
        };
      });
      const temporaryComposer = cloneComposerState(get().composer);

      try {
        await get().sendMessage({ restoreComposerOnFailure: false });
      } finally {
        if (
          isConversationMutationCurrent(
            get().currentConvId,
            convId,
            mutationFence,
          )
        ) {
        // sendMessage reset composer 后，把用户原本未发出的草稿字段补回。
        // 但若 composer 已被外部改动（如其他流程主动写了新草稿），不要覆盖。
        const cur = get().composer;
          const isTemporaryInpaintDraft = isTemporaryInpaintComposerDraft(
            cur,
            text,
            tempAttId,
            temporaryComposer,
          );
          if (
            isResetComposerDraft(cur, temporaryComposer) ||
            isTemporaryInpaintDraft
          ) {
            set({ composer: backup });
          }
        }
      }

      if (
        !isConversationMutationCurrent(
          get().currentConvId,
          convId,
          mutationFence,
        )
      ) {
        return;
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
              const imageId = first
                ? recordString(first, "image_id")
                : undefined;
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
              const generationExplainability =
                generationExplainabilityFromPayload(payload);
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
                imageMetadata.revised_prompt =
                  generationExplainability.revised_prompt;
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
                parent_image_id:
                  recordNullableString(first, "parent_image_id") ?? null,
              from_generation_id: id,
              size_requested: "auto",
              size_actual: actualSize ?? "unknown",
              filename: recordString(first, "filename"),
              metadata_jsonb:
                Object.keys(imageMetadata).length > 0 ? imageMetadata : null,
              ...generationExplainability,
              ...billingMetaFromPayload(
                {
                    is_dual_race_bonus: recordBoolean(
                      first,
                      "is_dual_race_bonus",
                    ),
                  billing_free: recordBoolean(first, "billing_free"),
                  billing_label: recordString(first, "billing_label"),
                    billing_exempt_reason: recordString(
                      first,
                      "billing_exempt_reason",
                    ),
                },
                firstMetadata,
              ),
            };
          }
          set((s) =>
            applyGenerationEventState(s, {
              generationId: id,
              rawGenerationId: rawId,
              eventName,
              payload,
              pendingImage,
              getId: get_id,
              eventNow,
            }),
          );
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
            const aspect_ratio =
              get_id("aspect_ratio") ?? DEFAULT_PARAMS.aspect_ratio;
          const primary_input_image_id =
            get_id("primary_input_image_id") ?? null;
          const inputImagesRaw = payload.input_image_ids;
          const input_image_ids = Array.isArray(inputImagesRaw)
              ? (inputImagesRaw.filter(
                  (v) => typeof v === "string",
                ) as string[])
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
                  "generate" | "edit",
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
            const msgId =
              rawMsgId ?? completionMessageLookupId(compId, eventNow);
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
              const imageId = first
                ? recordString(first, "image_id")
                : undefined;
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
                aspect_ratio: DEFAULT_PARAMS.aspect_ratio,
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
            messages: s.messages.map((message) =>
              applyCompletionEventToMessage(message, {
                messageId: msgId,
                completionId: compId,
                eventName,
                payload,
                getId: get_id,
                eventNow,
              }),
            ),
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
                  current.map(
                    (item) => `${item.kind}:${item.id ?? item.content}`,
                  ),
              );
              const nextWrites = [
                ...current,
                ...writes.filter(
                    (item) =>
                      !seen.has(`${item.kind}:${item.id ?? item.content}`),
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
          const msgId =
            get_id("assistant_message_id") ?? get_id("message_id");
          const resolved = optionalAssistantIntent(payload.intent_resolved);
          if (!msgId || !resolved) return;
          set((s) => ({
            messages: s.messages.map((m) =>
              m.role === "assistant" && m.id === msgId
                ? ({ ...m, intent_resolved: resolved } as AssistantMessage)
                : m,
            ),
          }));
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
                resp,
                get().generations,
                get().imagesById,
              );
              rememberMessageListMaterialization(convId, built.materialization);
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

    ...createTaskRecoveryActions(set, get, {
      flushCompletionStreamPatches,
      userSessionFence: _userSessionFence,
      isAbortRequest,
      errorToMessage,
    }),

    reset: () => {
      _runtimeFastDefault = null;
      _fastTouchedByUser = false;
      _userSessionFence.advance();
      _conversationMutationFence.advance();
      clearUserScopedRuntime();
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
