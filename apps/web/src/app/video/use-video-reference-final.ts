"use client";

import { useCallback } from "react";
import type { RefObject } from "react";
import { toast } from "@/components/ui/primitives";
import type { VideoAction, VideoGenerationOut, VideoOptionsOut } from "@/lib/types";
import { referenceFinalMedia, referenceFinalResolution, type VideoDraftMode } from "./video-draft-mode";
import { firstModelForAction, videoModelsForAction, resolutionOptionsForModel } from "./video-options-model";
import { displayPromptReferenceMentions } from "./video-reference-domain";
import { referenceDraftFromHistory } from "./video-page-domain";
import type { ReferenceDraft } from "./video-workbench-ui";

type VideoDraftActionsOptions = {
  options: VideoOptionsOut | undefined;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  focusPromptTarget: (target: HTMLTextAreaElement) => void;
  switchDraftContext: (taskId: string, action: VideoAction) => void;
  loadDraftMedia: (imageId: string, label: string, media: ReferenceDraft[]) => void;
  setAction: (value: VideoAction) => void;
  setDraftMode: (value: VideoDraftMode) => void;
  setReferenceSourceId: (value: string | null) => void;
  setModel: (value: string) => void;
  setDurationS: (value: number) => void;
  setResolution: (value: string) => void;
  setAspectRatio: (value: string) => void;
  setGenerateAudio: (value: boolean) => void;
  setSeed: (value: string) => void;
  setPrompt: (value: string) => void;
  setSelectedVideoId: (value: string) => void;
  setIsTaskPanelOpen: (value: boolean) => void;
};

/** Restore editable inputs or prepare reference-based final creation, never submit. */
export function useVideoDraftActions({
  options, promptRef, focusPromptTarget, switchDraftContext, loadDraftMedia,
  setAction, setDraftMode, setReferenceSourceId, setModel, setDurationS,
  setResolution, setAspectRatio, setGenerateAudio, setSeed, setPrompt,
  setSelectedVideoId, setIsTaskPanelOpen,
}: VideoDraftActionsOptions) {
  const loadAsDraft = useCallback(
    (item: VideoGenerationOut) => {
      switchDraftContext(item.id, item.action);
      setAction(item.action);
      setDraftMode(item.draft ? "official" : item.reference_draft ? "reference" : "direct");
      setReferenceSourceId(null);
      setModel(item.model);
      setDurationS(item.duration_s);
      setResolution(item.resolution);
      setAspectRatio(item.aspect_ratio);
      setGenerateAudio(item.generate_audio);
      setSeed(item.seed != null ? String(item.seed) : "");
      const draftReferenceMedia = item.reference_media.map((reference, index) =>
        referenceDraftFromHistory(reference, index, item.reference_media),
      );
      loadDraftMedia(
        item.input_image_id ?? "",
        item.input_image_id ? "已从历史任务载入" : "",
        draftReferenceMedia,
      );
      setPrompt(
        displayPromptReferenceMentions(item.prompt, draftReferenceMedia),
      );
      requestAnimationFrame(() => {
        const target = promptRef.current;
        if (target) focusPromptTarget(target);
      });
      toast.success("已套用参数");
    },
    [
      focusPromptTarget,
      promptRef,
      loadDraftMedia,
      setDraftMode,
      setReferenceSourceId,
      setAction,
      setAspectRatio,
      setDurationS,
      setGenerateAudio,
      setModel,
      setPrompt,
      setResolution,
      setSeed,
      switchDraftContext,
    ],
  );

  const prepareReferenceFinal = useCallback((item: VideoGenerationOut) => {
    try {
      const refs = referenceFinalMedia(item);
      const counts = { image: 0, video: 0, audio: 0 };
      for (const ref of refs) counts[ref.kind] += 1;
      const models = videoModelsForAction(options, "reference", counts);
      if (!models.length) throw new Error("原素材与参考草稿超过可用成片模型上限");
      const targetModel = firstModelForAction(options, "reference", counts);
      switchDraftContext(`reference-final:${item.id}`, "reference");
      setAction("reference");
      setDraftMode("direct");
      setReferenceSourceId(item.id);
      setModel(targetModel);
      const finalResolution = referenceFinalResolution(resolutionOptionsForModel(options, targetModel, "reference"));
      setResolution(finalResolution);
      setDurationS(-1);
      setAspectRatio("adaptive");
      setGenerateAudio(item.generate_audio);
      setSeed(item.seed != null ? String(item.seed) : "");
      const media = refs.map((ref, index) => referenceDraftFromHistory(ref, index, refs));
      loadDraftMedia("", "", media);
      setPrompt(displayPromptReferenceMentions(item.prompt, media));
      setSelectedVideoId("");
      setIsTaskPanelOpen(false);
      requestAnimationFrame(() => promptRef.current?.scrollIntoView({ block: "center" }));
      toast.success("已载入参考草稿", { description: `${finalResolution ? "已预选1080p。" : "当前模型不支持1080p，使用模型默认分辨率。"}可修改原提示词与成片模型，确认后手动生成；另行计费，画面不保证一致。` });
    } catch (error) {
      toast.error("参考草稿不可用", { description: error instanceof Error ? error.message : undefined });
    }
  }, [options, promptRef, switchDraftContext, loadDraftMedia, setSelectedVideoId, setIsTaskPanelOpen,
    setAction, setDraftMode, setReferenceSourceId, setModel, setResolution,
    setDurationS, setAspectRatio, setGenerateAudio, setSeed, setPrompt]);


  return { loadAsDraft, prepareReferenceFinal };
}
