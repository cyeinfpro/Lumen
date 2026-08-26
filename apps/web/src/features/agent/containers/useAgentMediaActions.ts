"use client";

import { useCallback } from "react";
import type { GenerationSummary } from "@/features/assets";
import { lightboxItemForConversationImage } from "@/components/ui/chat/ConversationVisualAtoms";
import { imageBinaryUrl, imageVariantUrl, uploadImage } from "@/lib/apiClient";
import { MAX_UPLOAD_SOURCE_BYTES, maxUploadSourceMessage } from "@/lib/uploadLimits";
import type { Generation } from "@/lib/types";
import { useAgentStore } from "@/store/agent/useAgentStore";
import { useUiStore } from "@/store/useUiStore";
import type { AgentDraftAttachment } from "../model/contracts";


export function useAgentMediaActions(
  addDraftAttachment: (
    sessionId: string | null,
    attachment: AgentDraftAttachment,
  ) => boolean,
  setComposerError: (message: string | null) => void,
) {
  const openLightboxFromItems = useUiStore((state) => state.openLightboxFromItems);
  const upload = useCallback(
    async (file: File, signal: AbortSignal): Promise<AgentDraftAttachment> => {
      if (!file.type.startsWith("image/")) throw new Error("格式不正确");
      if (file.size > MAX_UPLOAD_SOURCE_BYTES) {
        throw new Error(maxUploadSourceMessage());
      }
      const image = await uploadImage(file, { signal });
      return {
        imageId: image.id,
        role: "reference",
        label: file.name.replace(/\.[^.]+$/, "").slice(0, 40) || null,
        name: file.name || "上传参考图",
        previewUrl:
          image.thumb_url ??
          image.preview_url ??
          image.url ??
          imageVariantUrl(image.id, "thumb256"),
        width: image.width,
        height: image.height,
        mime: image.mime,
      };
    },
    [],
  );
  const previewAttachment = useCallback(
    (attachment: AgentDraftAttachment) => {
      openLightboxFromItems(
        [
          {
            id: attachment.imageId,
            url: imageBinaryUrl(attachment.imageId),
            previewUrl: attachment.previewUrl,
            thumbUrl: attachment.previewUrl,
            prompt: attachment.name,
            width: attachment.width,
            height: attachment.height,
          },
        ],
        attachment.imageId,
      );
    },
    [openLightboxFromItems],
  );
  const previewGeneration = useCallback(
    (generation: Generation) => {
      if (!generation.image) return;
      const item = lightboxItemForConversationImage(generation, generation.image);
      openLightboxFromItems([item], item.id);
    },
    [openLightboxFromItems],
  );
  const addAttachment = useCallback(
    (attachment: AgentDraftAttachment) => {
      const added = addDraftAttachment(
        useAgentStore.getState().currentSessionId,
        attachment,
      );
      if (!added) {
        setComposerError("最多添加 16 张参考图，且不能重复添加");
      }
    },
    [addDraftAttachment, setComposerError],
  );
  const addGenerationReference = useCallback(
    (generation: Generation) => {
      if (!generation.image) return;
      addAttachment({
        imageId: generation.image.id,
        role: "reference",
        label: "Agent 结果",
        name: "Agent 结果图",
        previewUrl:
          generation.image.thumb_url ??
          generation.image.preview_url ??
          generation.image.data_url,
        width: generation.image.width,
        height: generation.image.height,
        mime: generation.image.mime,
      });
    },
    [addAttachment],
  );
  const pickAsset = useCallback(
    (item: GenerationSummary) => {
      addAttachment({
        imageId: item.image.id,
        role: "reference",
        label: "素材图",
        name: item.prompt.slice(0, 40) || "素材图",
        previewUrl:
          item.image.thumb_url ?? item.image.preview_url ?? item.image.url,
        width: item.image.width,
        height: item.image.height,
        mime: item.image.mime,
      });
    },
    [addAttachment],
  );
  return {
    upload,
    previewAttachment,
    previewGeneration,
    addGenerationReference,
    pickAsset,
  };
}
