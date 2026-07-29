"use client";

import { useCallback } from "react";
import type {
  Dispatch,
  SetStateAction,
} from "react";

import { toast } from "@/components/ui/primitives";
import type { VideoGenerationOut } from "@/lib/types";

import { hasVideo } from "./video-task-model";
import type { ReferenceDraft } from "./video-workbench-ui";

type UseVideoPageViewActionsOptions = {
  deleteVideoById: (videoId: string) => void;
  insertReferenceTag: (item: ReferenceDraft) => void;
  loadAsDraft: (item: VideoGenerationOut) => void;
  referencePreviewItem: ReferenceDraft | null;
  setIsTaskPanelOpen: Dispatch<SetStateAction<boolean>>;
  setReferencePreviewItem: Dispatch<SetStateAction<ReferenceDraft | null>>;
  setSelectedVideoId: Dispatch<SetStateAction<string>>;
};

export function useVideoPageViewActions({
  deleteVideoById,
  insertReferenceTag,
  loadAsDraft,
  referencePreviewItem,
  setIsTaskPanelOpen,
  setReferencePreviewItem,
  setSelectedVideoId,
}: UseVideoPageViewActionsOptions) {
  const copyVideoPrompt = useCallback((item: VideoGenerationOut) => {
    void navigator.clipboard?.writeText(item.prompt);
    toast.success("描述已复制");
  }, []);

  const useTaskAsDraft = useCallback(
    (item: VideoGenerationOut) => {
      loadAsDraft(item);
      setIsTaskPanelOpen(false);
    },
    [loadAsDraft, setIsTaskPanelOpen],
  );

  const deleteTaskVideo = useCallback(
    (item: VideoGenerationOut) => {
      if (item.video) deleteVideoById(item.video.id);
    },
    [deleteVideoById],
  );

  const previewTaskVideo = useCallback(
    (item: VideoGenerationOut) => {
      if (!hasVideo(item)) return;
      setSelectedVideoId(item.video.id);
      setIsTaskPanelOpen(false);
    },
    [setIsTaskPanelOpen, setSelectedVideoId],
  );

  const closeReferencePreview = useCallback(
    () => setReferencePreviewItem(null),
    [setReferencePreviewItem],
  );

  const insertReferencePreview = useCallback(() => {
    if (!referencePreviewItem) return;
    insertReferenceTag(referencePreviewItem);
    setReferencePreviewItem(null);
  }, [
    insertReferenceTag,
    referencePreviewItem,
    setReferencePreviewItem,
  ]);

  return {
    closeReferencePreview,
    copyVideoPrompt,
    deleteTaskVideo,
    insertReferencePreview,
    previewTaskVideo,
    useTaskAsDraft,
  };
}
