"use client";

import { AnimatePresence } from "framer-motion";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useRef,
} from "react";

import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { nearestAspectRatio } from "@/lib/sizing";
import { useChatStore } from "@/store/useChatStore";
import { useInpaintStore } from "@/store/useInpaintStore";

import { InpaintModalView } from "./InpaintModalView";
import { handleInpaintKeyDown } from "./inpaintModalKeyboard";
import { useInpaintModalLifecycle } from "./useInpaintModalLifecycle";
import { useInpaintSubmission } from "./useInpaintSubmission";

const SOFT_PROMPT_LIMIT = 1500;

export function InpaintModal() {
  const open = useInpaintStore((state) => state.open);
  return (
    <AnimatePresence>
      {open ? <InpaintModalInner key="inpaint-modal" /> : null}
    </AnimatePresence>
  );
}

function InpaintModalInner() {
  const source = useInpaintStore((state) => state.source);
  const close = useInpaintStore((state) => state.close);
  const submitting = useInpaintStore((state) => state.submitting);
  const setSubmitting = useInpaintStore((state) => state.setSubmitting);
  const drafts = useInpaintStore((state) => state.drafts);
  const setDraft = useInpaintStore((state) => state.setDraft);
  const clearDraft = useInpaintStore((state) => state.clearDraft);
  const maskDrafts = useInpaintStore((state) => state.maskDrafts);
  const setMaskDraft = useInpaintStore((state) => state.setMaskDraft);
  const clearMaskDraft = useInpaintStore((state) => state.clearMaskDraft);
  const submitInpaintTask = useChatStore((state) => state.submitInpaintTask);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const boardRef = useRef<import("./MaskBoard").MaskBoardHandle | null>(null);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const currentImageId = source?.imageId ?? null;
  const initialDraft = source ? drafts[source.imageId] ?? "" : "";
  const initialStrokes = currentImageId
    ? maskDrafts[currentImageId] ?? null
    : null;

  const lifecycle = useInpaintModalLifecycle({
    source,
    currentImageId,
    initialDraft,
    initialStrokes,
    submitting,
    promptRef,
    close,
    setDraft,
    clearDraft,
    setMaskDraft,
    clearMaskDraft,
    getDraftSnapshot: useInpaintStore.getState,
  });

  const promptValid = lifecycle.promptText.length > 0;
  const derivedAspect =
    source && source.width && source.height
      ? nearestAspectRatio(source.width, source.height)
      : null;
  const promptOverSoftLimit = lifecycle.prompt.length > SOFT_PROMPT_LIMIT;
  const promptOverHardLimit = lifecycle.prompt.length > MAX_PROMPT_CHARS;
  const canSubmit =
    !submitting &&
    lifecycle.hasStroke &&
    promptValid &&
    !promptOverHardLimit &&
    !!source;

  const handleSubmit = useInpaintSubmission({
    boardRef,
    source,
    promptText: lifecycle.promptText,
    canSubmit,
    submittingRef: lifecycle.submittingRef,
    setSubmitting,
    setWarning: lifecycle.setWarning,
    submitInpaintTask,
    clearDraft,
    clearMaskDraft,
    onSubmitSuccess: useCallback(
      () => useInpaintStore.setState({ open: false, source: null }),
      [],
    ),
  });

  const onRootKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      handleInpaintKeyDown(
        event,
        rootRef.current,
        lifecycle.handleClose,
        () => void handleSubmit(),
      );
    },
    [handleSubmit, lifecycle.handleClose],
  );

  return (
    <InpaintModalView
      source={source}
      rootRef={rootRef}
      boardRef={boardRef}
      promptRef={promptRef}
      initialStrokes={initialStrokes}
      submitting={submitting}
      prompt={lifecycle.prompt}
      hasStroke={lifecycle.hasStroke}
      coverage={lifecycle.coverage}
      warning={lifecycle.warning}
      confirmingClose={lifecycle.confirmingClose}
      derivedAspect={derivedAspect}
      promptOverSoftLimit={promptOverSoftLimit}
      promptOverHardLimit={promptOverHardLimit}
      canSubmit={canSubmit}
      onClose={lifecycle.handleClose}
      onKeyDown={onRootKeyDown}
      onPromptChange={lifecycle.handlePromptChange}
      onPointerDownCanvas={lifecycle.cancelConfirmClose}
      onStrokesChange={lifecycle.handleStrokesChange}
      onStatsChange={lifecycle.handleStatsChange}
      onSubmit={() => void handleSubmit()}
    />
  );
}
