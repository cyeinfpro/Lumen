import { uuid } from "@/lib/utils";
import { logWarn } from "@/lib/logger";
import { appendPromptWithinLimit, clampPromptForRequest } from "@/lib/promptLimits";
import { PRESET } from "@/lib/sizing";
import type {
  AspectRatio,
  AttachmentImage,
  AssistantMessage,
  Generation,
  UserMessage,
} from "@/lib/types";
import {
  ApiError,
  apiFetch,
  createSilentGeneration,
  retryTask,
} from "@/lib/apiClient";
import { uploadImage as apiUploadImage } from "@/lib/api/images";
import { adaptBackendAssistantMessage } from "./messageAdapters";
import {
  cloneComposerState,
  didPromptNeedTrimming,
  inpaintAspectRatio,
  inpaintValidationError,
  isResetComposerDraft,
  isRetryComposerDraft,
  isTemporaryInpaintComposerDraft,
} from "./composerSlice";
import {
  aggregateGenerationStatus,
  assistantHasGeneration,
  generationIdsOfMessage,
} from "./generationSlice";
import { DEFAULT_PARAMS } from "./imageParams";
import type { ChatState, ChatStateGetter, ChatStateSetter } from "./types";
import {
  buildPendingRegenerationGeneration,
  generationForImage,
  generationParentUserMessageId,
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

type GenerationActionDependencies = {
  runtimeFastDefault: () => boolean | null;
};

export function createGenerationActions(
  set: ChatStateSetter,
  get: ChatStateGetter,
  dependencies: GenerationActionDependencies,
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
          fast: dependencies.runtimeFastDefault() ?? false,
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
          fast: dependencies.runtimeFastDefault() ?? false,
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
  };
}
