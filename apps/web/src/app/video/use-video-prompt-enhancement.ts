"use client";

import { useCallback } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";

import { toast } from "@/components/ui/primitives";
import { enhanceVideoPrompt } from "@/lib/apiClient";
import type { VideoAction } from "@/lib/types";
import { isVideoRequestFenceCurrent } from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  displayPromptReferenceMentions,
  referencePayloadForVideoAction,
  referencesForVideoAction,
  promptForVideoAction,
} from "./video-reference-domain";
import {
  applyPromptEnhanceCandidateState,
  buildPromptEnhanceCandidates,
  canEnhanceVideoPrompt,
  inputImageForVideoAction,
  interruptedPromptEnhanceDescription,
  notifyCompletedPromptEnhancement,
  VIDEO_PROMPT_VARIANT_COUNT,
} from "./video-page-domain";
import {
  cleanPromptEnhanceText,
} from "./video-workbench-ui";
import type {
  PromptEnhanceCandidate,
  ReferenceDraft,
} from "./video-workbench-ui";

type UseVideoPromptEnhancementOptions = {
  action: VideoAction;
  aspectRatio: string;
  draftFenceRef: MutableRefObject<VideoRequestFence>;
  durationS: number;
  generateAudio: boolean;
  hasActiveUpload: () => boolean;
  inputImageId: string;
  isEnhancingPrompt: boolean;
  model: string;
  prompt: string;
  promptEnhanceAbortRef: MutableRefObject<AbortController | null>;
  promptEnhanceEpochRef: MutableRefObject<number>;
  referenceMedia: ReferenceDraft[];
  referenceUploadPending: boolean;
  resolution: string;
  setIsEnhancingPrompt: Dispatch<SetStateAction<boolean>>;
  setPrompt: Dispatch<SetStateAction<string>>;
  setPromptEnhanceCandidates: Dispatch<
    SetStateAction<PromptEnhanceCandidate[]>
  >;
  setPromptEnhancePreview: Dispatch<SetStateAction<string>>;
  setSelectedPromptEnhanceCandidateId: Dispatch<SetStateAction<string>>;
  uploadPending: boolean;
  clearPromptEnhanceChoices: () => void;
};

export function useVideoPromptEnhancement({
  action,
  aspectRatio,
  clearPromptEnhanceChoices,
  draftFenceRef,
  durationS,
  generateAudio,
  hasActiveUpload,
  inputImageId,
  isEnhancingPrompt,
  model,
  prompt,
  promptEnhanceAbortRef,
  promptEnhanceEpochRef,
  referenceMedia,
  referenceUploadPending,
  resolution,
  setIsEnhancingPrompt,
  setPrompt,
  setPromptEnhanceCandidates,
  setPromptEnhancePreview,
  setSelectedPromptEnhanceCandidateId,
  uploadPending,
}: UseVideoPromptEnhancementOptions) {
  const canEnhancePrompt = canEnhanceVideoPrompt({
    uploadPending,
    referenceUploadPending,
    prompt,
    action,
    inputImageId,
    referenceCount: referenceMedia.length,
  });

  const enhancePromptAction = useCallback(async () => {
    if (isEnhancingPrompt || !canEnhancePrompt || hasActiveUpload()) return;

    const original = prompt;
    const activeReferenceMedia = referencesForVideoAction(
      action,
      referenceMedia,
    );
    const current = promptForVideoAction(action, prompt, activeReferenceMedia);
    const controller = new AbortController();
    promptEnhanceAbortRef.current?.abort();
    const requestEpoch = promptEnhanceEpochRef.current + 1;
    const requestDraftFence = { ...draftFenceRef.current };
    promptEnhanceEpochRef.current = requestEpoch;
    promptEnhanceAbortRef.current = controller;
    clearPromptEnhanceChoices();
    setIsEnhancingPrompt(true);
    let accumulated = "";
    const isCurrentRequest = () =>
      !controller.signal.aborted &&
      promptEnhanceAbortRef.current === controller &&
      promptEnhanceEpochRef.current === requestEpoch &&
      isVideoRequestFenceCurrent(draftFenceRef.current, requestDraftFence);

    try {
      await enhanceVideoPrompt(
        {
          text: current,
          action,
          model,
          duration_s: durationS,
          resolution,
          aspect_ratio: aspectRatio,
          generate_audio: generateAudio,
          input_image_id: inputImageForVideoAction(action, inputImageId),
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media: referencePayloadForVideoAction(
            action,
            referenceMedia,
          ),
        },
        (delta) => {
          if (!isCurrentRequest()) return;
          accumulated += delta;
          setPromptEnhancePreview(
            displayPromptReferenceMentions(accumulated, activeReferenceMedia),
          );
        },
        controller.signal,
      );
      if (!isCurrentRequest()) return;
      const candidates = buildPromptEnhanceCandidates(
        accumulated,
        current,
        activeReferenceMedia,
      );
      const applied = applyPromptEnhanceCandidateState(
        candidates,
        setPrompt,
        setPromptEnhanceCandidates,
        setSelectedPromptEnhanceCandidateId,
      );
      if (applied) {
        setPromptEnhancePreview("");
        notifyCompletedPromptEnhancement(
          applied.recommended,
          applied.autoApply,
          candidates.length,
        );
      } else {
        setPromptEnhancePreview("");
        toast.error("优化失败", { description: "没有收到有效提示词" });
        setPrompt(original);
      }
    } catch (error) {
      if (isCurrentRequest()) {
        const description =
          error instanceof Error ? error.message : undefined;
        if (accumulated.trim()) {
          const candidates = buildPromptEnhanceCandidates(
            accumulated,
            current,
            activeReferenceMedia,
          );
          const applied = applyPromptEnhanceCandidateState(
            candidates,
            setPrompt,
            setPromptEnhanceCandidates,
            setSelectedPromptEnhanceCandidateId,
          );
          if (!applied) {
            setPrompt(
              displayPromptReferenceMentions(
                cleanPromptEnhanceText(accumulated),
                activeReferenceMedia,
              ),
            );
          }
          setPromptEnhancePreview("");
          toast.error("优化中断", {
            description: interruptedPromptEnhanceDescription(description),
          });
        } else {
          toast.error("优化失败", { description });
          setPrompt(original);
        }
      }
    } finally {
      if (
        promptEnhanceAbortRef.current === controller &&
        promptEnhanceEpochRef.current === requestEpoch
      ) {
        promptEnhanceAbortRef.current = null;
        setIsEnhancingPrompt(false);
      }
    }
  }, [
    action,
    aspectRatio,
    canEnhancePrompt,
    clearPromptEnhanceChoices,
    draftFenceRef,
    durationS,
    generateAudio,
    hasActiveUpload,
    inputImageId,
    isEnhancingPrompt,
    model,
    prompt,
    promptEnhanceAbortRef,
    promptEnhanceEpochRef,
    referenceMedia,
    resolution,
    setIsEnhancingPrompt,
    setPrompt,
    setPromptEnhanceCandidates,
    setPromptEnhancePreview,
    setSelectedPromptEnhanceCandidateId,
  ]);

  return {
    canEnhancePrompt,
    enhancePromptAction,
  };
}
