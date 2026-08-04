import {
  createConversation as apiCreateConversation,
  postMessage as apiPostMessage,
  type PostMessageIn,
  type PostMessageOut,
} from "@/lib/api/conversations";
import { ApiError } from "@/lib/api/http";
import { semanticPostIdempotency } from "@/lib/api/semanticIdempotency";
import { logWarn } from "@/lib/logger";
import {
  findInvalidImageMentionLabels,
  serializePromptImageMentionsForRequest,
} from "@/lib/promptImageMentions";
import {
  PROMPT_TOO_LONG_MESSAGE,
  isPromptTooLong,
} from "@/lib/promptLimits";
import {
  defaultOutputCompression,
  qualityToFixedSize,
} from "@/lib/sizing";
import type {
  AssistantMessage,
  Generation,
  ImageParams,
  Intent,
  StructuredAttachment,
  UserMessage,
} from "@/lib/types";
import { uuid } from "@/lib/utils";
import {
  cloneComposerState,
  hasComposerContent,
  isResetComposerDraft,
  resolveIntent,
} from "./composerSlice";
import { drainPendingCompletionImage } from "./completionImageReconciliation";
import {
  clampImageCount,
  normalizeImageParams,
  normalizeRenderQuality,
} from "./imageParams";
import {
  adaptBackendAssistantMessage,
  adaptBackendUserMessage,
} from "./messageAdapters";
import { structuredAttachmentsFromComposer } from "./payload";
import {
  _completionMessageAliases,
  _generationConvIds,
  _generationIdAliases,
  _messageConvIds,
  errorCodeToMessage,
  invalidateConversationHistoryCache,
  isAbortRequest,
  isImageIntent,
  markSendRequestSubmitted,
  rememberCompletionAlias,
  rememberCompletionMessage,
  rememberGenerationAlias,
  setBounded,
  trackSendRequest,
  _conversationMutationFence,
  _userSessionFence,
} from "./runtime";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
  ComposerState,
} from "./types";

type SendMessageOptions = Parameters<ChatState["sendMessage"]>[0];

type SendMessageDependencies = {
  createInitialComposer: () => ComposerState;
  facadeDelegates?: unknown;
};

type PreparedSend = {
  composer: ComposerState;
  text: string;
  requestText: string;
  attachments: ComposerState["attachments"];
  params: ImageParams;
  intent: Exclude<Intent, "auto">;
  isImage: boolean;
  maskImageId: string | undefined;
  structuredAttachments: StructuredAttachment[];
  attachmentImageIds: string[];
  actionSource: string;
};

type PrepareResult =
  | { prepared: PreparedSend; error: null }
  | { prepared: null; error: string | null };

type OptimisticSend = {
  userId: string;
  assistantId: string;
  generationIds: string[];
  userMessage: UserMessage;
  assistantMessage: AssistantMessage;
  generations: Record<string, Generation>;
};

type ValidatedSend = {
  isImage: boolean;
  realUser: UserMessage;
  realAssistant: AssistantMessage;
  generationIds: string[];
  completionId: string | undefined;
};

function createConversationError(err: unknown): string {
  if (err instanceof ApiError) {
    return `新建会话失败：${err.message}（${err.code}）`;
  }
  if (err instanceof Error) return `新建会话失败：${err.message}`;
  return "新建会话失败";
}

function initialHistorySendError(
  state: ChatState,
  convId: string,
): string | null {
  if (state.currentConvId !== convId || state.messages.length > 0) return null;
  if (state.messagesLoading) return "历史消息仍在加载，稍候";
  if (state.messagesError) return "历史消息加载失败，先重试";
  return null;
}

async function ensureConversation(
  set: ChatStateSetter,
  get: ChatStateGetter,
  signal: AbortSignal,
): Promise<string | null> {
  const currentConvId = get().currentConvId;
  if (currentConvId) return currentConvId;
  try {
    const created = await apiCreateConversation({}, { signal });
    if (signal.aborted) return null;
    const activeConvId = get().currentConvId;
    if (activeConvId && activeConvId !== created.id) return null;
    set({ currentConvId: created.id });
    return created.id;
  } catch (err) {
    if (isAbortRequest(err, signal)) return null;
    const message = createConversationError(err);
    logWarn("auto-create conversation failed", {
      scope: "chat",
      code: err instanceof ApiError ? err.code : undefined,
      extra: { msg: err instanceof Error ? err.message : "unknown" },
    });
    set({ composerError: message });
    return null;
  }
}

function invalidMentionError(labels: string[]): string | null {
  if (labels.length === 0) return null;
  const preview = labels.slice(0, 3).join("、");
  const suffix = labels.length > 3 ? " 等" : "";
  return `参考图引用无效：${preview}${suffix}，先移除或补齐附件`;
}

function resolveMaskImageId(
  composer: ComposerState,
  intent: Exclude<Intent, "auto">,
): string | undefined {
  const firstAttachment = composer.attachments[0];
  if (intent !== "image_to_image") return undefined;
  if (composer.attachments.length !== 1 || !firstAttachment) return undefined;
  return composer.mask?.target_attachment_id === firstAttachment.id
    ? composer.mask.image_id
    : undefined;
}

function resolveActionSource(
  intent: Exclude<Intent, "auto">,
  maskImageId: string | undefined,
): string {
  if (maskImageId) return "composer.inpaint";
  switch (intent) {
    case "image_to_image":
      return "composer.image_to_image";
    case "text_to_image":
      return "composer.text_to_image";
    case "vision_qa":
      return "composer.vision_qa";
    default:
      return "composer.chat";
  }
}

function prepareSend(
  composer: ComposerState,
  options: SendMessageOptions,
): PrepareResult {
  const snapshot = cloneComposerState(composer);
  const attachments = snapshot.attachments;
  const text = snapshot.text.trim();
  const invalidMentions = findInvalidImageMentionLabels(
    text,
    attachments.length,
  );
  const mentionError = invalidMentionError(invalidMentions);
  if (mentionError) return { prepared: null, error: mentionError };
  if (!text && attachments.length === 0) {
    return { prepared: null, error: null };
  }
  const requestText = serializePromptImageMentionsForRequest(text, attachments);
  if (isPromptTooLong(requestText)) {
    return { prepared: null, error: PROMPT_TOO_LONG_MESSAGE };
  }
  const params = normalizeImageParams(snapshot.params);
  const intent =
    options?.intentOverride ??
    resolveIntent(snapshot.mode, attachments.length > 0, snapshot.forceIntent);
  const maskImageId = resolveMaskImageId(snapshot, intent);
  const structuredAttachments = structuredAttachmentsFromComposer(
    attachments,
    intent,
    Boolean(maskImageId),
  );
  return {
    prepared: {
      composer: snapshot,
      text,
      requestText,
      attachments,
      params,
      intent,
      isImage: isImageIntent(intent),
      maskImageId,
      structuredAttachments,
      attachmentImageIds: structuredAttachments.map(
        (attachment) => attachment.image_id,
      ),
      actionSource: resolveActionSource(intent, maskImageId),
    },
    error: null,
  };
}

function optimisticGeneration(
  id: string,
  prepared: PreparedSend,
  assistantId: string,
  traceId: string,
): Generation {
  return {
    id,
    message_id: assistantId,
    action: prepared.intent === "image_to_image" ? "edit" : "generate",
    prompt: prepared.requestText,
    size_requested:
      prepared.params.size_mode === "fixed" && prepared.params.fixed_size
        ? prepared.params.fixed_size
        : "auto",
    aspect_ratio: prepared.params.aspect_ratio,
    input_image_ids: prepared.attachmentImageIds,
    primary_input_image_id: prepared.attachmentImageIds[0] ?? null,
    status: "queued",
    stage: "queued",
    source: "composer",
    action_source: prepared.actionSource,
    trace_id: traceId,
    attachment_roles: prepared.structuredAttachments,
    attempt: 0,
    started_at: 0,
  };
}

function buildOptimisticSend(
  prepared: PreparedSend,
  traceId: string,
): OptimisticSend {
  const userId = `opt-user-${uuid()}`;
  const assistantId = `opt-asst-${uuid()}`;
  const generationIds = prepared.isImage
    ? Array.from(
        { length: clampImageCount(prepared.params.count) },
        () => `opt-gen-${uuid()}`,
      )
    : [];
  const now = Date.now();
  const userMessage: UserMessage = {
    id: userId,
    role: "user",
    text: prepared.text,
    attachments: prepared.attachments,
    intent: prepared.intent,
    image_params: prepared.params,
    web_search: prepared.isImage ? undefined : prepared.composer.webSearch,
    file_search: prepared.isImage ? undefined : prepared.composer.fileSearch,
    code_interpreter: prepared.isImage
      ? undefined
      : prepared.composer.codeInterpreter,
    image_generation: prepared.isImage
      ? undefined
      : prepared.composer.imageGeneration,
    created_at: now,
  };
  const assistantMessage: AssistantMessage = {
    id: assistantId,
    role: "assistant",
    parent_user_message_id: userId,
    intent_resolved: prepared.intent,
    status: "pending",
    generation_ids: generationIds.length > 0 ? generationIds : undefined,
    generation_id: generationIds[0],
    created_at: now,
  };
  const generations = Object.fromEntries(
    generationIds.map((id) => [
      id,
      optimisticGeneration(id, prepared, assistantId, traceId),
    ]),
  );
  return {
    userId,
    assistantId,
    generationIds,
    userMessage,
    assistantMessage,
    generations,
  };
}

function resetComposerAfterSend(
  state: ChatState,
  createInitialComposer: () => ComposerState,
): ComposerState {
  return {
    ...createInitialComposer(),
    mode: state.composer.mode,
    params: state.composer.params,
    reasoningEffort: state.composer.reasoningEffort,
    fast: state.composer.fast,
    webSearch: state.composer.webSearch,
    fileSearch: state.composer.fileSearch,
    codeInterpreter: state.composer.codeInterpreter,
    imageGeneration: state.composer.imageGeneration,
  };
}

function commitOptimisticSend(
  set: ChatStateSetter,
  convId: string,
  optimistic: OptimisticSend,
  createInitialComposer: () => ComposerState,
): void {
  setBounded(_messageConvIds, optimistic.userId, convId);
  setBounded(_messageConvIds, optimistic.assistantId, convId);
  for (const id of optimistic.generationIds) {
    setBounded(_generationConvIds, id, convId);
  }
  invalidateConversationHistoryCache(convId);
  set((state) => ({
    messages: [
      ...state.messages,
      optimistic.userMessage,
      optimistic.assistantMessage,
    ],
    generations:
      optimistic.generationIds.length > 0
        ? { ...state.generations, ...optimistic.generations }
        : state.generations,
    composer: resetComposerAfterSend(state, createInitialComposer),
  }));
}

function buildChatParams(prepared: PreparedSend): Record<string, unknown> | undefined {
  if (prepared.isImage) return undefined;
  const params: Record<string, unknown> = {
    fast: prepared.composer.fast,
  };
  if (prepared.composer.reasoningEffort) {
    params.reasoning_effort = prepared.composer.reasoningEffort;
  }
  if (prepared.composer.webSearch) params.web_search = true;
  if (prepared.composer.fileSearch) params.file_search = true;
  if (prepared.composer.codeInterpreter) params.code_interpreter = true;
  if (prepared.composer.imageGeneration) params.image_generation = true;
  return params;
}

function buildImageParams(prepared: PreparedSend): ImageParams | undefined {
  if (!prepared.isImage) return undefined;
  const {
    quality,
    render_quality: renderQualityOverride,
    output_format: outputFormat,
    output_compression: outputCompressionOverride,
    background: backgroundOverride,
    moderation: moderationOverride,
    ...rest
  } = prepared.params;
  const resolvedQuality = quality ?? "4k";
  const resolvedSize = qualityToFixedSize(
    resolvedQuality,
    prepared.params.aspect_ratio,
  );
  const renderQuality = normalizeRenderQuality(renderQualityOverride);
  const outputCompression =
    outputFormat === undefined
      ? undefined
      : (outputCompressionOverride ??
        defaultOutputCompression({
          renderQuality,
          outputFormat,
          fast: prepared.composer.fast,
        }));
  const imageParams: ImageParams = {
    ...rest,
    ...resolvedSize,
    quality: resolvedQuality,
    fast: prepared.composer.fast,
    render_quality: renderQuality,
    background: backgroundOverride ?? "auto",
    moderation: moderationOverride ?? "low",
  };
  if (outputFormat !== undefined) imageParams.output_format = outputFormat;
  if (outputCompression !== undefined) {
    imageParams.output_compression = outputCompression;
  }
  return imageParams;
}

type PostMessagePayload = Omit<
  PostMessageIn,
  "idempotency_key" | "trace_id"
>;

function buildPostPayload(prepared: PreparedSend): PostMessagePayload {
  return {
    text: prepared.requestText,
    attachment_image_ids: prepared.attachmentImageIds,
    attachments: prepared.structuredAttachments,
    input_images: prepared.attachmentImageIds,
    source: "composer",
    action_source: prepared.actionSource,
    ...(prepared.maskImageId
      ? { mask_image_id: prepared.maskImageId }
      : {}),
    intent: prepared.intent,
    image_params: buildImageParams(prepared),
    chat_params: buildChatParams(prepared),
  };
}

function buildPostBody(
  payload: PostMessagePayload,
  idempotencyKey: string,
): PostMessageIn {
  return {
    ...payload,
    idempotency_key: idempotencyKey,
    trace_id: idempotencyKey,
  };
}

function removeOptimisticGenerations(
  state: ChatState,
  generationIds: string[],
): Partial<ChatState> {
  if (generationIds.length === 0) return {};
  const generations = { ...state.generations };
  let changed = false;
  for (const id of generationIds) {
    if (!(id in generations)) continue;
    delete generations[id];
    changed = true;
  }
  return changed ? { generations } : {};
}

function dropOptimisticAliases(optimistic: OptimisticSend): void {
  // registerResponseAliases maps realId -> optimisticId, so the entries can
  // only be found by value. Reachable when reconcileSuccessfulSend throws
  // after registering (e.g. a malformed assistant_message): the rollback then
  // deletes the optimistic rows while the aliases still redirect real SSE ids
  // onto them, so later generation events resolve to messages that are gone.
  const orphaned = new Set(optimistic.generationIds);
  for (const [realId, alias] of _generationIdAliases) {
    if (orphaned.has(alias.optimisticId)) _generationIdAliases.delete(realId);
  }
  for (const [realId, alias] of _completionMessageAliases) {
    if (alias.optimisticMessageId === optimistic.assistantId) {
      _completionMessageAliases.delete(realId);
    }
  }
}

function removeOptimisticSend(
  set: ChatStateSetter,
  optimistic: OptimisticSend,
): void {
  _messageConvIds.delete(optimistic.userId);
  _messageConvIds.delete(optimistic.assistantId);
  for (const id of optimistic.generationIds) {
    _generationConvIds.delete(id);
  }
  dropOptimisticAliases(optimistic);
  set((state) => ({
    messages: state.messages.filter(
      (message) =>
        message.id !== optimistic.userId &&
        message.id !== optimistic.assistantId,
    ),
    ...removeOptimisticGenerations(state, optimistic.generationIds),
  }));
}

function registerResponseAliases(
  generationIds: string[],
  optimistic: OptimisticSend,
  completionId: string | undefined,
): void {
  const now = Date.now();
  for (const [index, realId] of generationIds.entries()) {
    const optimisticId = optimistic.generationIds[index];
    if (optimisticId) rememberGenerationAlias(realId, optimisticId, now);
  }
  if (completionId) {
    rememberCompletionAlias(completionId, optimistic.assistantId, now);
  }
}

function migrateOptimisticGenerations(
  state: ChatState,
  optimistic: OptimisticSend,
  realIds: string[],
  realAssistantId: string,
): Record<string, Generation> {
  if (optimistic.generationIds.length === 0) return state.generations;
  const remaining = { ...state.generations };
  const migrated: Record<string, Generation> = {};
  for (const [index, optimisticId] of optimistic.generationIds.entries()) {
    const old = remaining[optimisticId];
    delete remaining[optimisticId];
    _generationConvIds.delete(optimisticId);
    const realId = realIds[index];
    if (!old || !realId) continue;
    _generationIdAliases.delete(realId);
    migrated[realId] = {
      ...remaining[realId],
      ...old,
      id: realId,
      message_id: realAssistantId,
      image: remaining[realId]?.image ?? old.image,
      status: remaining[realId]?.status ?? old.status,
      stage: remaining[realId]?.stage ?? old.stage,
      finished_at: remaining[realId]?.finished_at ?? old.finished_at,
    };
  }
  return realIds.length > 0 ? { ...remaining, ...migrated } : remaining;
}

function replaceOptimisticMessages(
  state: ChatState,
  convId: string,
  optimistic: OptimisticSend,
  realUser: UserMessage,
  realAssistant: AssistantMessage,
  realGenerationIds: string[],
): ChatState | Partial<ChatState> {
  if (state.currentConvId !== convId) return state;
  return {
    messages: state.messages.map((message) => {
      if (message.id === optimistic.userId) return realUser;
      if (message.id === optimistic.assistantId) return realAssistant;
      return message;
    }),
    generations: migrateOptimisticGenerations(
      state,
      optimistic,
      realGenerationIds,
      realAssistant.id,
    ),
  };
}

function validatedGenerationIds(
  output: PostMessageOut,
  required: boolean,
): string[] {
  const raw = (output as { generation_ids?: unknown }).generation_ids;
  if (raw === undefined) {
    if (required) throw new TypeError("malformed send response");
    return [];
  }
  if (
    !Array.isArray(raw) ||
    raw.some(
      (generationId) =>
        typeof generationId !== "string" ||
        generationId.trim().length === 0,
    ) ||
    (required && raw.length === 0)
  ) {
    throw new TypeError("malformed send response");
  }
  return raw;
}

function validatedCompletionId(
  output: PostMessageOut,
  required: boolean,
): string | undefined {
  const raw = (output as { completion_id?: unknown }).completion_id;
  if (raw === undefined || raw === null) {
    if (required) throw new TypeError("malformed send response");
    return undefined;
  }
  if (typeof raw !== "string" || raw.trim().length === 0) {
    throw new TypeError("malformed send response");
  }
  return raw;
}

function validateSuccessfulSend(
  prepared: PreparedSend,
  output: PostMessageOut,
): ValidatedSend {
  const generationIds = validatedGenerationIds(output, prepared.isImage);
  const validatedCompletion = validatedCompletionId(
    output,
    !prepared.isImage,
  );
  const completionId = prepared.isImage ? undefined : validatedCompletion;
  const realUser: UserMessage = {
    ...adaptBackendUserMessage(
      output.user_message,
      prepared.attachments,
      prepared.params,
      prepared.intent,
    ),
    text: prepared.text,
  };
  const realAssistant = adaptBackendAssistantMessage(
    output.assistant_message,
    realUser.id,
    prepared.intent,
    prepared.isImage ? generationIds : undefined,
    completionId,
  );
  if (
    typeof realUser.id !== "string" ||
    realUser.id.trim().length === 0 ||
    typeof realAssistant.id !== "string" ||
    realAssistant.id.trim().length === 0
  ) {
    throw new TypeError("malformed send response");
  }
  return {
    isImage: prepared.isImage,
    realUser,
    realAssistant,
    generationIds,
    completionId,
  };
}

function applySuccessfulSend(
  set: ChatStateSetter,
  get: ChatStateGetter,
  convId: string,
  optimistic: OptimisticSend,
  validated: ValidatedSend,
): void {
  const { realUser, realAssistant, generationIds, completionId } = validated;
  registerResponseAliases(generationIds, optimistic, completionId);
  rememberCompletionMessage(completionId, realAssistant.id);
  _messageConvIds.delete(optimistic.userId);
  _messageConvIds.delete(optimistic.assistantId);
  setBounded(_messageConvIds, realUser.id, convId);
  setBounded(_messageConvIds, realAssistant.id, convId);
  for (const id of generationIds) setBounded(_generationConvIds, id, convId);
  set((state) =>
    replaceOptimisticMessages(
      state,
      convId,
      optimistic,
      realUser,
      realAssistant,
      generationIds,
    ),
  );
  if (completionId) {
    _completionMessageAliases.delete(completionId);
    drainPendingCompletionImage(set, get, completionId);
  }
}

function isStaleSend(
  get: ChatStateGetter,
  convId: string,
  userId: string | null,
  conversationEpoch: number,
  userEpoch: number,
  signal: AbortSignal,
): boolean {
  const state = get();
  return (
    signal.aborted ||
    state.currentConvId !== convId ||
    state.currentUserId !== userId ||
    !_conversationMutationFence.isCurrent(conversationEpoch) ||
    !_userSessionFence.isCurrent(userEpoch)
  );
}

function handlePostFailure(
  set: ChatStateSetter,
  err: unknown,
  options: SendMessageOptions,
  composer: ComposerState,
): void {
  const code = err instanceof ApiError ? err.code : "client_exception";
  const rawMessage = err instanceof Error ? err.message : "发送失败";
  const message = errorCodeToMessage(code) ?? rawMessage;
  logWarn("sendMessage failed", {
    scope: "chat",
    code,
    extra: { raw: rawMessage, phase: "post" },
  });
  set((state) => ({
    composerError: `发送失败：${message}`,
    ...(options?.restoreComposerOnFailure !== false &&
    isResetComposerDraft(state.composer, composer)
      ? { composer: cloneComposerState(composer) }
      : {}),
  }));
}

export function createSendMessageAction(
  set: ChatStateSetter,
  get: ChatStateGetter,
  dependencies: SendMessageDependencies,
): ChatState["sendMessage"] {
  void dependencies.facadeDelegates;
  return async (options) => {
    const controller = new AbortController();
    const untrack = trackSendRequest(controller);
    let optimistic: OptimisticSend | null = null;
    try {
      set({ composerError: null });
      const initialComposer = get().composer;
      if (!hasComposerContent(initialComposer)) return;
      if (isPromptTooLong(initialComposer.text.trim())) {
        set({ composerError: PROMPT_TOO_LONG_MESSAGE });
        return;
      }
      const convId = await ensureConversation(set, get, controller.signal);
      if (!convId) return;
      const historyError = initialHistorySendError(get(), convId);
      if (historyError) {
        set({ composerError: historyError });
        return;
      }
      const result = prepareSend(get().composer, options);
      if (!result.prepared) {
        if (result.error) set({ composerError: result.error });
        return;
      }
      const userId = get().currentUserId;
      const conversationEpoch = _conversationMutationFence.snapshot();
      const userEpoch = _userSessionFence.snapshot();
      if (
        isStaleSend(
          get,
          convId,
          userId,
          conversationEpoch,
          userEpoch,
          controller.signal,
        )
      ) {
        return;
      }
      const payload = buildPostPayload(result.prepared);
      const idempotency = await semanticPostIdempotency.acquire(
        {
          operation: "conversation.message.create",
          userId,
          conversationId: convId,
        },
        payload,
      );
      if (
        isStaleSend(
          get,
          convId,
          userId,
          conversationEpoch,
          userEpoch,
          controller.signal,
        )
      ) {
        await semanticPostIdempotency.discard(idempotency);
        return;
      }
      await semanticPostIdempotency.markSubmitted(idempotency);
      if (
        isStaleSend(
          get,
          convId,
          userId,
          conversationEpoch,
          userEpoch,
          controller.signal,
        )
      ) {
        await semanticPostIdempotency.discard(idempotency);
        return;
      }
      optimistic = buildOptimisticSend(result.prepared, idempotency.key);
      try {
        commitOptimisticSend(
          set,
          convId,
          optimistic,
          dependencies.createInitialComposer,
        );
      } catch (err) {
        await semanticPostIdempotency.discard(idempotency);
        throw err;
      }
      try {
        // POST 交给后端前标记已提交：此后切会话的 abortAllSendRequests 不再
        // abort 本请求——后端收到即可能已计费，abort 只会静默丢弃已计费发送。
        markSendRequestSubmitted(controller);
        const output: PostMessageOut = await apiPostMessage(
          convId,
          buildPostBody(payload, idempotency.key),
          { signal: controller.signal },
        );
        const validated = validateSuccessfulSend(result.prepared, output);
        await semanticPostIdempotency.confirm(idempotency);
        if (
          isStaleSend(
            get,
            convId,
            userId,
            conversationEpoch,
            userEpoch,
            controller.signal,
          )
        ) {
          removeOptimisticSend(set, optimistic);
          return;
        }
        applySuccessfulSend(
          set,
          get,
          convId,
          optimistic,
          validated,
        );
      } catch (err) {
        await semanticPostIdempotency.recordFailure(idempotency, err);
        removeOptimisticSend(set, optimistic);
        if (isAbortRequest(err, controller.signal)) return;
        if (
          isStaleSend(
            get,
            convId,
            userId,
            conversationEpoch,
            userEpoch,
            controller.signal,
          )
        ) {
          return;
        }
        handlePostFailure(set, err, options, result.prepared.composer);
      }
    } finally {
      untrack();
    }
  };
}
