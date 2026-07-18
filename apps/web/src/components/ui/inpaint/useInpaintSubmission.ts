"use client";

import { type RefObject, useCallback } from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { logError } from "@/lib/logger";
import type { InpaintSource } from "@/store/useInpaintStore";

import type { MaskBoardHandle, MaskExport } from "./MaskBoard";

const FULL_COVERAGE_WARN = 0.95;

export interface InpaintTaskPayload {
  sourceImageId: string;
  sourceSrc: string;
  sourceWidth?: number;
  sourceHeight?: number;
  maskBlob: Blob;
  maskPreviewDataUrl: string;
  prompt: string;
}

type SubmitInpaintTask = (payload: InpaintTaskPayload) => Promise<void>;

interface UseInpaintSubmissionOptions {
  boardRef: RefObject<MaskBoardHandle | null>;
  source: InpaintSource | null;
  promptText: string;
  canSubmit: boolean;
  submittingRef: RefObject<boolean>;
  setSubmitting: (value: boolean) => void;
  setWarning: (value: string | null) => void;
  submitInpaintTask: SubmitInpaintTask;
  clearDraft: (imageId: string) => void;
  clearMaskDraft: (imageId: string) => void;
  onSubmitSuccess: () => void;
}

async function exportMask(
  boardRef: RefObject<MaskBoardHandle | null>,
): Promise<MaskExport | null> {
  return (await boardRef.current?.exportMask()) ?? null;
}

export function useInpaintSubmission({
  boardRef,
  source,
  promptText,
  canSubmit,
  submittingRef,
  setSubmitting,
  setWarning,
  submitInpaintTask,
  clearDraft,
  clearMaskDraft,
  onSubmitSuccess,
}: UseInpaintSubmissionOptions) {
  return useCallback(async () => {
    if (!canSubmit || !source || submittingRef.current) return;

    // React state updates are deferred; lock before the first await so two
    // clicks cannot export and submit the same mask concurrently.
    submittingRef.current = true;
    setSubmitting(true);
    setWarning(null);

    try {
      let mask: MaskExport | null;
      try {
        mask = await exportMask(boardRef);
      } catch (err) {
        logError(err, { scope: "inpaint", code: "mask_export_failed" });
        setWarning("蒙版导出失败");
        return;
      }
      if (!mask) {
        setWarning("画布未就绪或未涂抹");
        return;
      }
      if (mask.coverage > FULL_COVERAGE_WARN) {
        setWarning(
          `涂抹 ${(mask.coverage * 100).toFixed(0)}%，接近整图重画`,
        );
      }

      await submitInpaintTask({
        sourceImageId: source.imageId,
        sourceSrc: source.src,
        // source.width/height 缺失时退到导出蒙版带回的实际尺寸。
        sourceWidth: source.width ?? mask.width,
        sourceHeight: source.height ?? mask.height,
        maskBlob: mask.blob,
        maskPreviewDataUrl: mask.preview_data_url,
        prompt: promptText,
      });
      pushMobileToast("已加入生成 · 在对话中查看进度", "success");
      clearDraft(source.imageId);
      clearMaskDraft(source.imageId);
      onSubmitSuccess();
    } catch (err) {
      logError(err, { scope: "inpaint", code: "submit_failed" });
      const msg = err instanceof Error ? err.message : "提交失败";
      setWarning(`提交失败 · ${msg}`);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }, [
    boardRef,
    canSubmit,
    clearDraft,
    clearMaskDraft,
    onSubmitSuccess,
    promptText,
    setSubmitting,
    setWarning,
    source,
    submitInpaintTask,
    submittingRef,
  ]);
}
