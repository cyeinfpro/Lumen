"use client";

import { type RefObject, useCallback } from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { logError } from "@/lib/logger";
import type { InpaintSubmissionResult } from "@/store/chat/types";
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

type SubmitInpaintTask = (
  payload: InpaintTaskPayload,
) => Promise<InpaintSubmissionResult>;

interface UseInpaintSubmissionOptions {
  ownerUserId: string | null;
  identityEpoch: number;
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

type MaskExportAttempt =
  | { ok: true; mask: MaskExport | null }
  | { ok: false; error: unknown };

async function tryExportMask(
  boardRef: RefObject<MaskBoardHandle | null>,
): Promise<MaskExportAttempt> {
  try {
    return { ok: true, mask: await exportMask(boardRef) };
  } catch (error) {
    return { ok: false, error };
  }
}

function canStartSubmission(
  canSubmit: boolean,
  source: InpaintSource | null,
  submitting: boolean,
  identityIsCurrent: boolean,
): source is InpaintSource {
  return canSubmit && source !== null && !submitting && identityIsCurrent;
}

export function useInpaintSubmission({
  ownerUserId,
  identityEpoch,
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
    const identity = {
      userId: ownerUserId,
      epoch: identityEpoch,
    };
    const identityIsCurrent = () =>
      isPrivateIdentitySnapshotCurrent(identity);
    if (
      !canStartSubmission(
        canSubmit,
        source,
        submittingRef.current,
        identityIsCurrent(),
      )
    ) {
      return;
    }

    // React state updates are deferred; lock before the first await so two
    // clicks cannot export and submit the same mask concurrently.
    submittingRef.current = true;
    setSubmitting(true);
    setWarning(null);

    try {
      const exportAttempt = await tryExportMask(boardRef);
      if (!exportAttempt.ok) {
        logError(exportAttempt.error, {
          scope: "inpaint",
          code: "mask_export_failed",
        });
        if (!identityIsCurrent()) return;
        setWarning("蒙版导出失败");
        return;
      }
      if (!identityIsCurrent()) return;
      const mask = exportAttempt.mask;
      if (!mask) {
        setWarning("画布未就绪或未涂抹");
        return;
      }
      if (mask.coverage > FULL_COVERAGE_WARN) {
        setWarning(
          `涂抹 ${(mask.coverage * 100).toFixed(0)}%，接近整图重画`,
        );
      }

      const result = await submitInpaintTask({
        sourceImageId: source.imageId,
        sourceSrc: source.src,
        // source.width/height 缺失时退到导出蒙版带回的实际尺寸。
        sourceWidth: source.width ?? mask.width,
        sourceHeight: source.height ?? mask.height,
        maskBlob: mask.blob,
        maskPreviewDataUrl: mask.preview_data_url,
        prompt: promptText,
      });
      if (!identityIsCurrent() || result.status !== "submitted") return;
      pushMobileToast("已加入生成 · 在对话中查看进度", "success");
      clearDraft(source.imageId);
      clearMaskDraft(source.imageId);
      onSubmitSuccess();
    } catch (err) {
      logError(err, { scope: "inpaint", code: "submit_failed" });
      if (!identityIsCurrent()) return;
      const msg = err instanceof Error ? err.message : "提交失败";
      setWarning(`提交失败 · ${msg}`);
    } finally {
      submittingRef.current = false;
      if (identityIsCurrent()) {
        setSubmitting(false);
      }
    }
  }, [
    boardRef,
    canSubmit,
    clearDraft,
    clearMaskDraft,
    onSubmitSuccess,
    identityEpoch,
    ownerUserId,
    promptText,
    setSubmitting,
    setWarning,
    source,
    submitInpaintTask,
    submittingRef,
  ]);
}
