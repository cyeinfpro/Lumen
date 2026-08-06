import {
  listMessages as apiListMessages,
} from "@/lib/api/conversations";
import { ApiError } from "@/lib/api/http";
import {
  imageBinaryUrl,
  imageVariantUrl,
} from "@/lib/api/images";
import { recommendedActionsForError } from "@/lib/errors";
import { logWarn } from "@/lib/logger";
import { requestRuntimeRecovery } from "@/lib/runtimeResilience";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  MemoryWrite,
} from "@/lib/types";
import { applyCompletionEventToMessage } from "./completionEvents";
import {
  aggregateGenerationStatus,
  assistantHasGeneration,
  completionToolGenerationId,
  generationExplainabilityFromPayload,
  generationIdsOfMessage,
} from "@/features/generation";
import {
  drainPendingCompletionImage,
  drainReadyPendingCompletionImages,
  reconcileCompletionImage,
} from "./completionImageReconciliation";
import { buildMessageListState } from "./history";
import {
  coerceMemoryWrites,
  optionalAssistantIntent,
} from "./messageAdapters";
import {
  latestPersistedMessageId,
  mergeMessagesById,
} from "./messageReconciliation";
import { DEFAULT_PARAMS } from "./imageParams";
import {
  billingMetaFromPayload,
  optionalRecordArray,
  parseSizeString,
  recommendedActionsFromUnknown,
  recordBoolean,
  recordNullableString,
  recordString,
} from "./payload";
import {
  applyGenerationEventState,
  completionMessageLookupId,
  errorToMessage,
  flushCompletionStreamPatches,
  generationLookupId,
  invalidateConversationHistoryCache,
  MESSAGE_PAGE_LIMIT,
  optionalRecord,
  queueCompletionStreamPatch,
  rememberCompletionMessage,
  rememberMessageListMaterialization,
  scheduleBase64Eviction,
} from "./runtime";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";

type SseIdGetter = (key: string) => string | undefined;

type SseEventContext = {
  set: ChatStateSetter;
  get: ChatStateGetter;
  eventName: string;
  payload: Record<string, unknown>;
  getId: SseIdGetter;
  eventNow: number;
  cursor?: string;
  dependencies: SseEventDependencies;
};

type SseEventHandler = (context: SseEventContext) => void;

const GENERATION_LIFECYCLE_EVENTS = new Set([
  "generation.queued",
  "generation.started",
  "generation.progress",
  "generation.partial_image",
  "generation.succeeded",
  "generation.failed",
  "generation.retrying",
]);

const COMPLETION_EVENTS = new Set([
  "completion.queued",
  "completion.started",
  "completion.progress",
  "completion.delta",
  "completion.thinking_delta",
  "completion.image",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
]);

type SseEventDependencies = {
  listMessages: typeof apiListMessages;
  requestRealtimeRecovery: () => void;
  retryDelaysMs: readonly number[];
};

type AppendedMessageRecovery = {
  key: string;
  attempt: number;
  timer: ReturnType<typeof setTimeout> | null;
  context: SseEventContext;
  userId: string;
  convId: string;
  messageId: string | undefined;
};

const DEFAULT_APPENDED_MESSAGE_RETRY_DELAYS_MS = [0, 250, 1_000] as const;
const pendingAppendedMessageRecoveries = new Map<
  string,
  AppendedMessageRecovery
>();

export function clearAppendedMessageRecoveryQueue(): void {
  for (const recovery of pendingAppendedMessageRecoveries.values()) {
    if (recovery.timer !== null) clearTimeout(recovery.timer);
  }
  pendingAppendedMessageRecoveries.clear();
}

export function pendingAppendedMessageRecoveryCount(): number {
  return pendingAppendedMessageRecoveries.size;
}

function resolveSseEventDependencies(
  facadeDelegates: unknown,
): SseEventDependencies {
  const delegates =
    facadeDelegates && typeof facadeDelegates === "object"
      ? (facadeDelegates as Partial<SseEventDependencies>)
      : {};
  return {
    listMessages: delegates.listMessages ?? apiListMessages,
    requestRealtimeRecovery:
      delegates.requestRealtimeRecovery ??
      (() => requestRuntimeRecovery("realtime")),
    retryDelaysMs:
      delegates.retryDelaysMs ?? DEFAULT_APPENDED_MESSAGE_RETRY_DELAYS_MS,
  };
}

export function createSseIdGetter(
  payload: Record<string, unknown>,
): SseIdGetter {
  return (key) => {
    const value = payload[key];
    return typeof value === "string" ? value : undefined;
  };
}

function imageMetadataWithExplainability(
  first: Record<string, unknown>,
  payload: Record<string, unknown>,
): {
  metadata: Record<string, unknown> | null;
  explainability: ReturnType<typeof generationExplainabilityFromPayload>;
  firstMetadata: Record<string, unknown> | undefined;
} {
  const explainability = generationExplainabilityFromPayload(payload);
  const firstMetadata = optionalRecord(first.metadata_jsonb);
  const metadata = { ...(firstMetadata ?? {}) };
  if (
    explainability.diagnostics &&
    metadata.generation_diagnostics == null
  ) {
    metadata.generation_diagnostics = explainability.diagnostics;
  }
  if (explainability.revised_prompt && metadata.revised_prompt == null) {
    metadata.revised_prompt = explainability.revised_prompt;
  }
  return {
    metadata: Object.keys(metadata).length > 0 ? metadata : null,
    explainability,
    firstMetadata,
  };
}

function generationSucceededImage(
  payload: Record<string, unknown>,
  generationId: string,
): GeneratedImage | null {
  const first = optionalRecordArray(payload.images)?.[0];
  const imageId = first ? recordString(first, "image_id") : undefined;
  if (!first || !imageId) {
    logWarn("missing image_id in succeeded payload", {
      scope: "chat-sse",
      extra: { generation_id: generationId },
    });
    return null;
  }
  const actualSize = recordString(first, "actual_size");
  const { width, height } = parseSizeString(actualSize);
  const { metadata, explainability, firstMetadata } =
    imageMetadataWithExplainability(first, payload);
  return {
    id: imageId,
    data_url:
      recordString(first, "data_url") ??
      recordString(first, "url") ??
      imageBinaryUrl(imageId),
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
    width,
    height,
    parent_image_id:
      recordNullableString(first, "parent_image_id") ?? null,
    from_generation_id: generationId,
    size_requested: "auto",
    size_actual: actualSize ?? "unknown",
    filename: recordString(first, "filename"),
    metadata_jsonb: metadata,
    ...explainability,
    ...billingMetaFromPayload(
      {
        is_dual_race_bonus: recordBoolean(first, "is_dual_race_bonus"),
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

function handleGenerationLifecycle(context: SseEventContext): void {
  const rawId =
    context.getId("generation_id") ?? context.getId("id");
  if (!rawId) return;
  const generationId = generationLookupId(rawId, context.eventNow);
  const pendingImage =
    context.eventName === "generation.succeeded"
      ? (generationSucceededImage(context.payload, generationId) ?? undefined)
      : undefined;
  if (context.eventName === "generation.succeeded" && !pendingImage) return;
  context.set((state) =>
    applyGenerationEventState(state, {
      generationId,
      rawGenerationId: rawId,
      eventName: context.eventName,
      payload: context.payload,
      pendingImage,
      getId: context.getId,
      eventNow: context.eventNow,
    }),
  );
  if (context.eventName === "generation.succeeded") {
    scheduleBase64Eviction();
  }
}

function cancelGenerationState(
  state: ChatState,
  generationId: string,
  context: SseEventContext,
): ChatState | Partial<ChatState> {
  const generation = state.generations[generationId];
  if (!generation) return state;
  const nextGeneration: Generation = {
    ...generation,
    status: "canceled",
    stage: "finalizing",
    substage: "cancelled",
    cancelled: true,
    retrying: false,
    waiting_provider: false,
    retryable: true,
    error_code: context.getId("code") ?? "cancelled",
    error_message: context.getId("message") ?? "已取消",
    recommended_actions:
      recommendedActionsFromUnknown(context.payload.recommended_actions) ??
      recommendedActionsForError("cancelled", {
        retryable: true,
        status: "canceled",
      }),
    finished_at: context.eventNow,
  };
  const generations = {
    ...state.generations,
    [generationId]: nextGeneration,
  };
  return {
    generations,
    messages: state.messages.map((message) => {
      if (
        message.role !== "assistant" ||
        !assistantHasGeneration(message, generationId)
      ) {
        return message;
      }
      return {
        ...message,
        status: aggregateGenerationStatus(
          generationIdsOfMessage(message),
          generations,
          "canceled",
        ),
      } as AssistantMessage;
    }),
  };
}

function handleGenerationCanceled(context: SseEventContext): void {
  const rawId =
    context.getId("generation_id") ?? context.getId("id");
  if (!rawId) return;
  const generationId = generationLookupId(rawId, context.eventNow);
  context.set((state) =>
    cancelGenerationState(state, generationId, context),
  );
}

function attachedGeneration(
  context: SseEventContext,
  messageId: string,
  generationId: string,
): Generation {
  const inputImages = context.payload.input_image_ids;
  return {
    id: generationId,
    message_id: messageId,
    action: context.getId("action") === "edit" ? "edit" : "generate",
    prompt: context.getId("prompt") ?? "",
    size_requested: context.getId("size_requested") ?? "auto",
    aspect_ratio: (context.getId("aspect_ratio") ??
      DEFAULT_PARAMS.aspect_ratio) as Generation["aspect_ratio"],
    input_image_ids: Array.isArray(inputImages)
      ? inputImages.filter((value): value is string => typeof value === "string")
      : [],
    primary_input_image_id:
      context.getId("primary_input_image_id") ?? null,
    status: "running",
    stage: "rendering",
    attempt: 0,
    started_at: context.eventNow,
    ...billingMetaFromPayload(context.payload),
  };
}

function attachGenerationState(
  state: ChatState,
  context: SseEventContext,
  messageId: string,
  generationId: string,
): ChatState | Partial<ChatState> {
  const existingGeneration = state.generations[generationId];
  const targetMessage = state.messages.find(
    (message) => message.role === "assistant" && message.id === messageId,
  );
  if (
    existingGeneration &&
    targetMessage?.role === "assistant" &&
    assistantHasGeneration(targetMessage, generationId)
  ) {
    return state;
  }
  return {
    generations: existingGeneration
      ? state.generations
      : {
          ...state.generations,
          [generationId]: attachedGeneration(
            context,
            messageId,
            generationId,
          ),
        },
    messages: state.messages.map((message) => {
      if (message.role !== "assistant" || message.id !== messageId) {
        return message;
      }
      const existingIds = generationIdsOfMessage(message);
      if (existingIds.includes(generationId)) return message;
      return {
        ...message,
        generation_ids: [...existingIds, generationId],
      } as AssistantMessage;
    }),
  };
}

function handleGenerationAttached(context: SseEventContext): void {
  const messageId = context.getId("message_id");
  const generationId = context.getId("generation_id");
  if (!messageId || !generationId) return;
  context.set((state) =>
    attachGenerationState(state, context, messageId, generationId),
  );
}

function completionTarget(context: SseEventContext): {
  rawMessageId: string | undefined;
  knownMessageId: string | undefined;
  messageId: string | undefined;
  completionId: string | undefined;
} {
  const rawMessageId =
    context.getId("assistant_message_id") ?? context.getId("message_id");
  const completionId =
    context.getId("completion_id") ??
    context.getId("task_id") ??
    context.getId("id");
  const knownMessageId = completionMessageLookupId(
    completionId,
    context.eventNow,
  );
  return {
    rawMessageId,
    knownMessageId,
    messageId: rawMessageId ?? knownMessageId,
    completionId,
  };
}

function completionTextDelta(payload: Record<string, unknown>): string {
  if (typeof payload.text_delta === "string") return payload.text_delta;
  if (typeof payload.delta === "string") return payload.delta;
  return typeof payload.text === "string" ? payload.text : "";
}

function completionImage(
  payload: Record<string, unknown>,
  generationId: string,
): GeneratedImage | null {
  const first = optionalRecordArray(payload.images)?.[0];
  const imageId = first ? recordString(first, "image_id") : undefined;
  if (!first || !imageId) return null;
  const actualSize = recordString(first, "actual_size");
  const { width, height } = parseSizeString(actualSize);
  return {
    id: imageId,
    data_url:
      recordString(first, "data_url") ??
      recordString(first, "url") ??
      imageBinaryUrl(imageId),
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
    width,
    height,
    parent_image_id: null,
    from_generation_id: generationId,
    size_requested: actualSize ?? "auto",
    size_actual: actualSize ?? "unknown",
    filename: recordString(first, "filename"),
    metadata_jsonb: optionalRecord(first.metadata_jsonb) ?? null,
  };
}

function handleCompletionImage(
  context: SseEventContext,
  target: ReturnType<typeof completionTarget>,
): void {
  const completionId = target.completionId;
  if (!completionId) return;
  const image = completionImage(
    context.payload,
    completionToolGenerationId(completionId),
  );
  if (!image) return;
  reconcileCompletionImage(
    context.set,
    context.get,
    {
      completionId,
      rawMessageId: target.rawMessageId,
    },
    image,
    context.eventNow,
  );
}

function refreshTerminalCompletion(
  context: SseEventContext,
  completionId: string | undefined,
): void {
  if (context.eventName !== "completion.succeeded") return;
  if (!completionId || typeof context.payload.text === "string") return;
  void context
    .get()
    .refreshCompletionText(completionId)
    .catch((err) => {
      logWarn("completion terminal refresh after SSE failed", {
        scope: "chat-sse",
        code: err instanceof ApiError ? err.code : undefined,
        extra: { completionId, err: errorToMessage(err) },
      });
    });
}

function handleCompletionLifecycle(context: SseEventContext): void {
  const target = completionTarget(context);
  const { messageId, completionId } = target;
  if (!messageId && !completionId) {
    // 修复静默丢事件：两个 id 都没有的 completion 事件无法归属到任何消息，只能丢弃，
    // 但之前是无声丢弃。这属于后端 payload 契约异常，留一条日志才能定位。
    logWarn("completion event without message or completion id", {
      scope: "chat-sse",
      extra: { event: context.eventName },
    });
    return;
  }
  if (context.eventName === "completion.image") {
    handleCompletionImage(context, target);
    return;
  }
  rememberCompletionMessage(completionId, messageId, context.eventNow);
  if (completionId) {
    drainPendingCompletionImage(context.set, context.get, completionId);
  }
  if (context.eventName === "completion.thinking_delta") {
    const delta =
      typeof context.payload.thinking_delta === "string"
        ? context.payload.thinking_delta
        : "";
    queueCompletionStreamPatch(
      messageId,
      completionId,
      "thinking",
      delta,
    );
    return;
  }
  if (context.eventName === "completion.delta") {
    queueCompletionStreamPatch(
      messageId,
      completionId,
      "text",
      completionTextDelta(context.payload),
    );
    return;
  }
  flushCompletionStreamPatches();
  context.set((state) => ({
    messages: state.messages.map((message) =>
      applyCompletionEventToMessage(message, {
        messageId,
        completionId,
        eventName: context.eventName,
        payload: context.payload,
        getId: context.getId,
        eventNow: context.eventNow,
      }),
    ),
  }));
  refreshTerminalCompletion(context, completionId);
}

function memoryWriteKey(write: MemoryWrite): string {
  return `${write.kind}:${write.id ?? write.content}`;
}

function mergeMemoryWrites(
  message: AssistantMessage,
  writes: MemoryWrite[],
): AssistantMessage {
  const current = message.memory_writes ?? [];
  const seen = new Set(current.map(memoryWriteKey));
  return {
    ...message,
    memory_writes: [
      ...current,
      ...writes.filter((write) => !seen.has(memoryWriteKey(write))),
    ],
  };
}

function handleMemoryWrites(context: SseEventContext): void {
  const messageId =
    context.getId("assistant_message_id") ?? context.getId("message_id");
  const writes = coerceMemoryWrites(context.payload.memory_writes);
  if (!messageId || writes.length === 0) return;
  context.set((state) => ({
    messages: state.messages.map((message) =>
      message.role === "assistant" && message.id === messageId
        ? mergeMemoryWrites(message, writes)
        : message,
    ),
  }));
}

function handleIntentResolved(context: SseEventContext): void {
  const messageId =
    context.getId("assistant_message_id") ?? context.getId("message_id");
  const resolved = optionalAssistantIntent(context.payload.intent_resolved);
  if (!messageId || !resolved) return;
  context.set((state) => ({
    messages: state.messages.map((message) =>
      message.role === "assistant" && message.id === messageId
        ? ({ ...message, intent_resolved: resolved } as AssistantMessage)
        : message,
    ),
  }));
}

async function syncAppendedMessage(
  context: SseEventContext,
  convId: string,
  messageId: string | undefined,
): Promise<void> {
  try {
    const initialState = context.get();
    if (initialState.currentConvId !== convId) return;
    const response = await context.dependencies.listMessages(convId, {
      limit: MESSAGE_PAGE_LIMIT,
      since: latestPersistedMessageId(initialState.messages),
      include: ["tasks"],
    });
    const currentState = context.get();
    if (currentState.currentConvId !== convId) return;
    const responseHasMessage = (response.items ?? []).some(
      (message) => message.id === messageId,
    );
    const storeHasMessage = currentState.messages.some(
      (message) => message.id === messageId,
    );
    if (messageId && !responseHasMessage && !storeHasMessage) {
      throw new Error("appended message missing from incremental response");
    }
    const built = buildMessageListState(
      response,
      currentState.generations,
      currentState.imagesById,
    );
    rememberMessageListMaterialization(convId, built.materialization);
    context.set((state) => {
      if (state.currentConvId !== convId) return state;
      return {
        messages: mergeMessagesById(state.messages, built.messages),
        generations: built.generations,
        imagesById: built.imagesById,
      };
    });
    drainReadyPendingCompletionImages(context.set, context.get);
    return;
  } catch (err) {
    logWarn("conv.message.appended incremental sync failed", {
      scope: "chat-sse",
      extra: { convId, messageId, err: errorToMessage(err) },
    });
    try {
      await context.get().loadHistoricalMessages(convId);
      const current = context.get();
      if (
        current.currentConvId !== convId ||
        (messageId &&
          !current.messages.some((message) => message.id === messageId))
      ) {
        throw new Error("appended message missing after full history reload");
      }
      drainReadyPendingCompletionImages(context.set, context.get);
      return;
    } catch (reloadErr) {
      logWarn("conv.message.appended fallback reload failed", {
        scope: "chat-sse",
        extra: {
          convId,
          messageId,
          err: errorToMessage(reloadErr),
        },
      });
      throw reloadErr;
    }
  }
}

function appendedMessageRecoveryKey(
  userId: string,
  convId: string,
  messageId: string | undefined,
  cursor: string | undefined,
): string {
  return JSON.stringify([userId, convId, messageId ?? null, cursor ?? null]);
}

function scheduleAppendedMessageRecovery(
  recovery: AppendedMessageRecovery,
): void {
  const delays = recovery.context.dependencies.retryDelaysMs;
  if (recovery.attempt >= delays.length) {
    invalidateConversationHistoryCache(recovery.convId);
    recovery.context.dependencies.requestRealtimeRecovery();
    pendingAppendedMessageRecoveries.delete(recovery.key);
    return;
  }
  const delayMs = Math.max(0, delays[recovery.attempt] ?? 0);
  recovery.timer = setTimeout(() => {
    recovery.timer = null;
    void runAppendedMessageRecovery(recovery);
  }, delayMs);
}

async function runAppendedMessageRecovery(
  recovery: AppendedMessageRecovery,
): Promise<void> {
  if (pendingAppendedMessageRecoveries.get(recovery.key) !== recovery) return;
  const state = recovery.context.get();
  if (
    state.currentUserId !== recovery.userId ||
    state.currentConvId !== recovery.convId
  ) {
    pendingAppendedMessageRecoveries.delete(recovery.key);
    return;
  }
  recovery.attempt += 1;
  try {
    await syncAppendedMessage(
      recovery.context,
      recovery.convId,
      recovery.messageId,
    );
    pendingAppendedMessageRecoveries.delete(recovery.key);
  } catch {
    scheduleAppendedMessageRecovery(recovery);
  }
}

function handleConversationMessageAppended(context: SseEventContext): void {
  const convId =
    context.getId("conversation_id") ?? context.getId("conv_id");
  const messageId = context.getId("message_id") ?? context.getId("id");
  const state = context.get();
  if (!convId || convId !== state.currentConvId) return;
  if (messageId && state.messages.some((message) => message.id === messageId)) {
    drainReadyPendingCompletionImages(context.set, context.get);
    return;
  }
  const userId = state.currentUserId;
  if (!userId) return;
  const key = appendedMessageRecoveryKey(
    userId,
    convId,
    messageId,
    context.cursor,
  );
  if (pendingAppendedMessageRecoveries.has(key)) return;
  const recovery: AppendedMessageRecovery = {
    key,
    attempt: 0,
    timer: null,
    context,
    userId,
    convId,
    messageId,
  };
  pendingAppendedMessageRecoveries.set(key, recovery);
  scheduleAppendedMessageRecovery(recovery);
}

const NOOP_HANDLER: SseEventHandler = () => {};

const EVENT_HANDLERS: Record<string, SseEventHandler> = {
  "generation.canceled": handleGenerationCanceled,
  "generation.attached": handleGenerationAttached,
  "memory.writes": handleMemoryWrites,
  "message.intent_resolved": handleIntentResolved,
  "conv.message.appended": handleConversationMessageAppended,
  account_settings_updated: NOOP_HANDLER,
  "conv.renamed": NOOP_HANDLER,
  "user.notice": NOOP_HANDLER,
};

export function applySseEventPayload(
  set: ChatStateSetter,
  get: ChatStateGetter,
  eventName: string,
  payload: Record<string, unknown>,
  eventNow: number,
  facadeDelegates?: unknown,
  cursor?: string,
): void {
  const context: SseEventContext = {
    set,
    get,
    eventName,
    payload,
    getId: createSseIdGetter(payload),
    eventNow,
    cursor,
    dependencies: resolveSseEventDependencies(facadeDelegates),
  };
  if (GENERATION_LIFECYCLE_EVENTS.has(eventName)) {
    handleGenerationLifecycle(context);
    return;
  }
  if (COMPLETION_EVENTS.has(eventName)) {
    handleCompletionLifecycle(context);
    return;
  }
  EVENT_HANDLERS[eventName]?.(context);
}
