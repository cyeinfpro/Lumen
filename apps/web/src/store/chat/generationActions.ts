import { imageParamsForReroll } from "../../lib/imageModels";
import { uuid } from "@/lib/utils";
import { logWarn } from "@/lib/logger";
import { appendPromptWithinLimit, clampPromptForRequest } from "@/lib/promptLimits";
import { PRESET } from "@/lib/sizing";
import type {
  AspectRatio,
  AttachmentImage,
  AssistantMessage,
  Generation,
  Intent,
  UserMessage,
} from "@/lib/types";
import {
  ApiError,
  apiFetch,
  createSilentGeneration,
  retryTask,
} from "@/lib/apiClient";
import {
  idempotentPostRequest,
  semanticPostIdempotency,
  type SemanticIdempotencyLease,
} from "@/lib/api/semanticIdempotency";
import { uploadImage as apiUploadImage } from "@/lib/api/images";
import { adaptBackendAssistantMessage } from "./messageAdapters";
import {
  cloneComposerState,
  didPromptNeedTrimming,
  inpaintAspectRatio,
  inpaintValidationError,
} from "./composerSlice";
import {
  aggregateGenerationStatus,
  assistantHasGeneration,
  generationIdsOfMessage,
} from "@/features/generation";
import { DEFAULT_PARAMS } from "./imageParams";
import { resolveRerollMask } from "./rerollMask";
import { mergeSubmissionMessages, mergeSubmissionGenerations } from "./submissionReconciliation";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
  InpaintSubmissionResult,
} from "./types";
import {
  buildPendingRegenerationGeneration,
  generationForImage,
  generationParentUserMessageId,
  invalidateConversationHistoryCache,
  isConversationMutationCurrent,
  isImageIntent,
  qualityFromFixedSize,
  rerollIntent,
  rememberCompletionMessage,
  rememberGenerationForConversation,
  setBounded,
  _conversationMutationFence,
  _generationConvIds,
  _messageConvIds,
} from "./runtime";
import {
  createGenerationRequestFence,
  generationRequestIsCurrent,
  markGenerationRequestSubmitted,
  type GenerationRequestFence,
} from "./generationRequestFence";

// 重试在途去重：retryAssistant / retryGeneration 是前端全部「重试」入口的唯一漏斗
// （GlobalTaskTray、桌面/移动画布的重试按钮均未绑定 disabled）。双击或连点会并发触发
// 多次重试请求 —— 每次重试都会重新计费。按目标 id 加锁，请求完成（成功或失败）后释放，
// 不影响之后再次重试。
const _retryInFlightAssistants = new Set<string>();
const _retryInFlightGenerations = new Set<string>();

// 重生成 / 放大 / 重roll 在途去重：regenerateAssistant / upscaleImage / rerollImage 与重试
// 一样没有绑定 disabled 的按钮（Lightbox、桌面/移动画布入口直接 void 调用）。双击或连点会
// 并发创建多条计费生成任务（重复扣费）。按目标 id 加锁，请求完成（成功或失败）后释放，
// 不影响之后再次操作。
const _regenerateInFlight = new Set<string>();
const _upscaleInFlight = new Set<string>();
const _rerollInFlight = new Set<string>();

type SilentGenerationPayload = Omit<
  Parameters<typeof createSilentGeneration>[1],
  "idempotency_key"
>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function validateSilentGenerationOutput(
  value: Awaited<ReturnType<typeof createSilentGeneration>>,
): void {
  if (
    !isRecord(value) ||
    !isRecord(value.assistant_message) ||
    typeof value.assistant_message.id !== "string" ||
    value.assistant_message.id.trim().length === 0 ||
    !Array.isArray(value.generation_ids) ||
    value.generation_ids.length === 0 ||
    value.generation_ids.some(
      (generationId) =>
        typeof generationId !== "string" ||
        generationId.trim().length === 0,
    )
  ) {
    throw new TypeError("malformed silent generation response");
  }
}

type RegenerateOutput = {
  assistant_message_id: string;
  completion_id: string | null;
  generation_ids: string[];
};

async function prepareSemanticSubmission(
  lease: SemanticIdempotencyLease,
  isCurrent: () => boolean,
): Promise<boolean> {
  if (!isCurrent()) {
    await semanticPostIdempotency.discard(lease);
    return false;
  }
  await semanticPostIdempotency.markSubmitted(lease);
  if (isCurrent()) return true;
  await semanticPostIdempotency.discard(lease);
  return false;
}

function validateRegenerateOutput(
  value: RegenerateOutput,
  intent: Exclude<Intent, "auto">,
): void {
  const imageIntent = isImageIntent(intent);
  if (
    !isRecord(value) ||
    typeof value.assistant_message_id !== "string" ||
    value.assistant_message_id.trim().length === 0 ||
    !(
      value.completion_id === null ||
      (typeof value.completion_id === "string" &&
        value.completion_id.trim().length > 0)
    ) ||
    !Array.isArray(value.generation_ids) ||
    value.generation_ids.some(
      (generationId) =>
        typeof generationId !== "string" ||
        generationId.trim().length === 0,
    ) ||
    (imageIntent && value.generation_ids.length === 0) ||
    (!imageIntent &&
      (typeof value.completion_id !== "string" ||
        value.completion_id.trim().length === 0))
  ) {
    throw new TypeError("malformed regenerate response");
  }
}

async function createSemanticSilentGeneration(
  request: GenerationRequestFence,
  operation: "upscale" | "reroll",
  targetImageId: string,
  fallbackIntent: Parameters<typeof adaptBackendAssistantMessage>[2],
  payload: SilentGenerationPayload,
  isCurrent: () => boolean,
) {
  const idempotency = await semanticPostIdempotency.acquire(
    {
      operation: `conversation.generation.${operation}`,
      userId: request.userId,
      conversationId: request.convId,
      targetImageId,
    },
    payload,
  );
  if (!(await prepareSemanticSubmission(idempotency, isCurrent))) return null;
  // The helper's return is another await boundary. Fence again immediately
  // before handing a possibly billable operation to the transport.
  if (!isCurrent()) {
    await semanticPostIdempotency.discard(idempotency);
    return null;
  }
  markGenerationRequestSubmitted(request);
  try {
    const output = await createSilentGeneration(request.convId, {
      ...payload,
      idempotency_key: idempotency.key,
    });
    validateSilentGenerationOutput(output);
    const generationIds = output.generation_ids;
    const assistant = adaptBackendAssistantMessage(
      output.assistant_message,
      payload.parent_message_id,
      fallbackIntent,
      generationIds,
      undefined,
    );
    await semanticPostIdempotency.confirm(idempotency);
    return { assistant, generationIds };
  } catch (err) {
    await semanticPostIdempotency.recordFailure(idempotency, err);
    throw err;
  }
}

async function _runUpscale(
  set: ChatStateSetter,
  get: ChatStateGetter,
  imageId: string,
  request: GenerationRequestFence,
): Promise<void> {
  // upscaleImage 的主体（不含在途去重锁），拆出以控制函数复杂度预算。
  const state = get();
  const { convId } = request;
  const img = state.imagesById[imageId];
  if (!img) return;
  const gen = generationForImage(state, img);
  const upscaleParams = imageParamsForReroll(gen ?? {});
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

  const payload: SilentGenerationPayload = {
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
      ...upscaleParams,
      background: "auto",
      moderation: "low",
    },
  };
  const submission = await createSemanticSilentGeneration(
    request,
    "upscale",
    imageId,
    "image_to_image",
    payload,
    () => generationRequestIsCurrent(get, request),
  );
  if (!submission) return;
  if (!generationRequestIsCurrent(get, request)) return;

  const { assistant: realAssistant, generationIds: genIds } = submission;
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
      requested_params: { ...payload.image_params },
      aspect_ratio: aspect,
      input_image_ids: [img.id],
      primary_input_image_id: img.id,
      status: "queued",
      stage: "queued",
      attempt: 0,
      started_at: 0,
    };
  }
  // 乐观追加新 assistant 与新 generation 后失效会话历史缓存，避免切走切回
  // 短暂显示放大前的旧快照。
  invalidateConversationHistoryCache(convId);
  set((s) => ({
    messages: mergeSubmissionMessages(s.messages, [realAssistant]),
    generations: mergeSubmissionGenerations(s.generations, optimisticGens),
  }));
}

function buildRegenerationPlaceholders(
  get: ChatStateGetter,
  convId: string,
  generationIds: string[],
  source: Omit<Parameters<typeof buildPendingRegenerationGeneration>[0], "newGenerationId">,
): Record<string, Generation> {
  const placeholders: Record<string, Generation> = {};
  for (const generationId of generationIds) {
    const pending = buildPendingRegenerationGeneration({
      ...source,
      newGenerationId: generationId,
    });
    if (!pending) continue;
    placeholders[generationId] = pending;
    setBounded(_generationConvIds, generationId, convId);
    // A late acknowledgement must not replace an already materialized task.
    if (!get().generations[generationId]) {
      rememberGenerationForConversation(convId, pending);
    }
  }
  return placeholders;
}

function buildRerollPayload(
  generation: Generation,
  parentMessageId: string,
  maskImageId: string | null,
): SilentGenerationPayload {
  const fixedSize = generation.size_requested.includes("x");
  return {
    parent_message_id: parentMessageId,
    intent: rerollIntent(generation),
    prompt: clampPromptForRequest(generation.prompt),
    attachment_image_ids: generation.input_image_ids,
    ...(maskImageId ? { mask_image_id: maskImageId } : {}),
    image_params: {
      aspect_ratio: generation.aspect_ratio,
      size_mode: fixedSize ? "fixed" : "auto",
      fixed_size: fixedSize ? generation.size_requested : undefined,
      quality: qualityFromFixedSize(generation.size_requested, generation.aspect_ratio),
      count: 1,
      ...imageParamsForReroll(generation),
      background: "auto",
      moderation: "low",
    },
  };
}

export function createGenerationActions(
  set: ChatStateSetter,
  get: ChatStateGetter,
): Pick<
  ChatState,
  | "retryAssistant"
  | "retryGeneration"
  | "regenerateAssistant"
  | "upscaleImage"
  | "rerollImage"
  | "submitInpaintTask"
> {
  return {
    async retryAssistant(assistantMsgId) {
      // 同一条 assistant 消息的重试在途去重（详见文件头部 _retryInFlightAssistants）：
      // 文本重试双击会发出两个 sendMessage（重复生成/重复计费），图片重试双击会走
      // 下方 retryGeneration（其自身也有去重）。请求完成（含失败）后释放锁。
      if (_retryInFlightAssistants.has(assistantMsgId)) return;
      _retryInFlightAssistants.add(assistantMsgId);
      try {
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
        const retryText = userMsg.text.trim() || "(继续)";

        const current = cloneComposerState(get().composer);
        await get().sendMessage({
          intentOverride: asst.intent_resolved,
          restoreComposerOnFailure: false,
          composerSnapshot: {
            ...current,
            text: retryText,
            attachments: structuredClone(userMsg.attachments),
            params: { ...userMsg.image_params },
            mask: null,
            forceIntent: undefined,
            webSearch: userMsg.web_search ?? current.webSearch,
            fileSearch: userMsg.file_search ?? current.fileSearch,
            codeInterpreter: userMsg.code_interpreter ?? current.codeInterpreter,
            imageGeneration: userMsg.image_generation ?? current.imageGeneration,
          },
        });
      } finally {
        _retryInFlightAssistants.delete(assistantMsgId);
      }
    },

    async retryGeneration(generationId) {
      if (_retryInFlightGenerations.has(generationId)) return;
      _retryInFlightGenerations.add(generationId);
      const before = get().generations[generationId];
      const owner = get().currentUserId;
      const convId = get().currentConvId;
      const fence = _conversationMutationFence.snapshot();
      try {
        await retryTask("generations", generationId);
      } finally {
        _retryInFlightGenerations.delete(generationId);
      }

      // 与 regenerate/upscale/reroll 同类：乐观重新入队后同步失效会话历史缓存，
      // 否则切走切回会短暂显示该 generation 旧的失败/取消状态。
      const retriedGen = get().generations[generationId];
      invalidateConversationHistoryCache(
        retriedGen?.message_id
          ? (_messageConvIds.get(retriedGen.message_id) ??
            get().currentConvId)
          : get().currentConvId,
      );

      set((s) => {
        const gen = s.generations[generationId];
        if (!gen || gen !== before || !convId || s.currentUserId !== owner ||
          !isConversationMutationCurrent(s.currentConvId, convId, fence)) return s;

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
      // 重生成在途去重（详见文件头部 _regenerateInFlight）：双击会并发两次 POST
      // /regenerate，每次都创建新的计费任务并取消旧任务。请求完成（含失败）后释放锁。
      if (_regenerateInFlight.has(messageId)) return;
      _regenerateInFlight.add(messageId);
      let releaseRequest = () => {};
      try {
        const state = get();
        const convId = state.currentConvId;
        if (!convId) {
          throw new ApiError({
            code: "no_conversation",
            message: "当前没有活动会话",
            status: 0,
          });
        }
        const userId = state.currentUserId;
        const activeRequest = createGenerationRequestFence(convId, userId);
        releaseRequest = activeRequest.release;
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

        const payload = { intent: newIntent };
        const idempotency = await semanticPostIdempotency.acquire(
          {
            operation: "conversation.message.regenerate",
            userId,
            conversationId: convId,
            messageId,
          },
          payload,
        );
        if (
          !(await prepareSemanticSubmission(idempotency, () =>
            generationRequestIsCurrent(get, activeRequest),
          ))
        ) {
          return;
        }
        // Scope can change between the helper resolving and this continuation.
        if (!generationRequestIsCurrent(get, activeRequest)) {
          await semanticPostIdempotency.discard(idempotency);
          return;
        }
        // 1) 乐观从 messages 中移除旧 assistant；保存快照用于回滚。
        // 同时失效会话历史缓存：否则切走切回会短暂恢复出已被移除的旧助手消息
        // （旧 generation 也已本地标 canceled，缓存快照仍是旧状态）。
        invalidateConversationHistoryCache(convId);
        set((s) => ({
          messages: s.messages.filter(
            (m) => !(m.role === "assistant" && m.id === messageId),
          ),
        }));
        try {
          const body = {
            ...payload,
            idempotency_key: idempotency.key,
          };
          markGenerationRequestSubmitted(activeRequest);
          const out = await apiFetch<RegenerateOutput>(
            `/conversations/${convId}/messages/${messageId}/regenerate`,
            idempotentPostRequest(body),
          );
          validateRegenerateOutput(out, newIntent);
          await semanticPostIdempotency.confirm(idempotency);
          if (!generationRequestIsCurrent(get, activeRequest)) {
            return;
          }

          const isImage = isImageIntent(newIntent);
          const newGenIds = isImage ? [...new Set(out.generation_ids)] : [];
          const newGenId = newGenIds[0];
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
            generation_ids: newGenIds.length ? newGenIds : undefined,
            completion_id: completionId,
            created_at: now,
          };
          setBounded(_messageConvIds, out.assistant_message_id, convId);
          rememberCompletionMessage(completionId, out.assistant_message_id);

          const pendingGens = buildRegenerationPlaceholders(get, convId, newGenIds, {
            state,
            assistantMessageId: out.assistant_message_id,
            parentUserId,
            newIntent,
            oldGeneration: oldGen,
          });

          // await 期间 loadHistoricalMessages 可能已重写缓存，插入 pending 后再失效一次。
          invalidateConversationHistoryCache(convId);
          set((s) => ({
            messages: mergeSubmissionMessages(s.messages, [pendingAsst]),
            generations: mergeSubmissionGenerations(s.generations, pendingGens),
          }));
          // Old tasks remain governed by backend cancellation/settlement events.
          // In particular, successful historical results are never cancelled here.
        } catch (err) {
          await semanticPostIdempotency.recordFailure(idempotency, err);
          if (!generationRequestIsCurrent(get, activeRequest)) {
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
      } finally {
        releaseRequest();
        _regenerateInFlight.delete(messageId);
      }
    },

    async upscaleImage(imageId) {
      // 放大在途去重（详见文件头部 _upscaleInFlight）：双击会并发创建两条放大生成
      // 任务（每次 4k 放大都会计费）。请求完成（含失败）后释放锁。
      if (_upscaleInFlight.has(imageId)) return;
      _upscaleInFlight.add(imageId);
      let releaseRequest = () => {};
      try {
        const state = get();
        if (!state.currentConvId) return;
        const request = createGenerationRequestFence(
          state.currentConvId,
          state.currentUserId,
        );
        releaseRequest = request.release;
        await _runUpscale(set, get, imageId, request);
      } finally {
        releaseRequest();
        _upscaleInFlight.delete(imageId);
      }
    },

    async rerollImage(imageId) {
      // 重roll 在途去重（详见文件头部 _rerollInFlight）：双击会并发创建两条重roll
      // 生成任务（每次都会计费）。请求完成（含失败）后释放锁。
      if (_rerollInFlight.has(imageId)) return;
      _rerollInFlight.add(imageId);
      let releaseRequest = () => {};
      try {
        const state = get();
        const convId = state.currentConvId;
        if (!convId) return;
        const activeRequest = createGenerationRequestFence(
          convId,
          state.currentUserId,
        );
        releaseRequest = activeRequest.release;
        const img = state.imagesById[imageId];
        if (!img) return;
        const genId = img.from_generation_id;
        if (!genId) return;
        const gen = state.generations[genId];
        if (!gen) return;

        const parentMsgId = generationParentUserMessageId(state, genId);
        if (!parentMsgId) return;

        let maskImageId: string | null;
        try {
          maskImageId = await resolveRerollMask(gen, () =>
            apiFetch<{ id: string; mask_image_id?: string | null }>(
              `/generations/${encodeURIComponent(gen.id)}`,
              { signal: activeRequest.controller.signal },
            ),
          );
        } catch (error) {
          if (activeRequest.controller.signal.aborted ||
              !generationRequestIsCurrent(get, activeRequest)) return;
          throw error;
        }
        if (!generationRequestIsCurrent(get, activeRequest)) return;
        const payload = buildRerollPayload(gen, parentMsgId, maskImageId);
        const intent = payload.intent;
        const submission = await createSemanticSilentGeneration(
          activeRequest,
          "reroll",
          imageId,
          intent,
          payload,
          () => generationRequestIsCurrent(get, activeRequest),
        );
        if (!submission) return;
        if (!generationRequestIsCurrent(get, activeRequest)) return;

        const { assistant: realAssistant, generationIds: genIds } = submission;
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
            mask_image_id: maskImageId,
            requested_params: { ...payload.image_params },
            status: "queued",
            stage: "queued",
            attempt: 0,
            started_at: 0,
          };
        }
        // 乐观追加新 assistant 与新 generation 后失效会话历史缓存，避免切走切回
        // 短暂显示重roll 前的旧快照。
        invalidateConversationHistoryCache(convId);
        set((s) => ({
          messages: mergeSubmissionMessages(s.messages, [realAssistant]),
          generations: mergeSubmissionGenerations(s.generations, optimisticGens),
        }));
      } finally {
        releaseRequest();
        _rerollInFlight.delete(imageId);
      }
    },

    // —— 独立的局部修改提交入口 ——
    // 浏览态（Lightbox / 卡片 / 对话气泡）的"局部修改"会调到这里。
    //
    // 上传 mask 后构造独立请求快照，复用 sendMessage 的幂等提交与身份检查。
    // 不替换、不清空全局 composer，因此用户已有草稿和继续输入都保留。
    // 首次局部修改走消息接口以保留用户指令；结果 reroll 走支持 mask 的 silent 接口。
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
          return { status: "cancelled" };
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
        return { status: "cancelled" };
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

      try {
        await get().sendMessage({
          restoreComposerOnFailure: false,
          throwOnError: true,
          composerSnapshot: {
            ...backup,
            text,
            attachments: [tempAtt],
            mode: "image",
            forceIntent: "image",
            mask: {
              image_id: maskUploaded.id,
              preview_data_url: maskPreviewDataUrl,
              target_attachment_id: tempAttId,
            },
            params: {
              ...backup.params,
              aspect_ratio: inferredAspect ?? backup.params.aspect_ratio,
              count: 1,
            },
          },
        });
      } catch (error) {
        if (!isConversationMutationCurrent(get().currentConvId, convId, mutationFence)) {
          return { status: "cancelled" };
        }
        throw error;
      }

      if (
        !isConversationMutationCurrent(
          get().currentConvId,
          convId,
          mutationFence,
        )
      ) {
        return { status: "cancelled" };
      }
      // The independent send path propagates its own error, not another request's shared UI error.
      return { status: "submitted" } satisfies InpaintSubmissionResult;
    },
  };
}
