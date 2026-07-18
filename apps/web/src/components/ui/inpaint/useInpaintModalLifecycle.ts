"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { InpaintSource } from "@/store/useInpaintStore";

import type { Stroke } from "./types";

const WARNING_AUTO_DISMISS_MS = 5_000;
const CLOSE_CONFIRM_TIMEOUT_MS = 2_500;

interface DraftSnapshot {
  drafts: Record<string, string>;
  maskDrafts: Record<string, Stroke[]>;
}

interface UseInpaintModalLifecycleOptions {
  source: InpaintSource | null;
  currentImageId: string | null;
  initialDraft: string;
  initialStrokes: Stroke[] | null;
  submitting: boolean;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  close: () => void;
  setDraft: (imageId: string, prompt: string) => void;
  clearDraft: (imageId: string) => void;
  setMaskDraft: (imageId: string, strokes: Stroke[]) => void;
  clearMaskDraft: (imageId: string) => void;
  getDraftSnapshot: () => DraftSnapshot;
}

export function useInpaintModalLifecycle({
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
  getDraftSnapshot,
}: UseInpaintModalLifecycleOptions) {
  const [prompt, setPrompt] = useState(initialDraft);
  const [hasStroke, setHasStroke] = useState(
    () => (initialStrokes?.length ?? 0) > 0,
  );
  const [coverage, setCoverage] = useState(0);
  const [warning, setWarning] = useState<string | null>(null);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const previousImageIdRef = useRef(currentImageId);
  const confirmCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const submittingRef = useRef(submitting);

  useBodyScrollLock(true);

  useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);

  // source 直接切换时，modal 不会重 mount；从 store 读取最新草稿并重置本地统计。
  useEffect(() => {
    if (previousImageIdRef.current === currentImageId) return;
    previousImageIdRef.current = currentImageId;
    const snapshot = getDraftSnapshot();
    setPrompt(currentImageId ? snapshot.drafts[currentImageId] ?? "" : "");
    setHasStroke(
      currentImageId
        ? (snapshot.maskDrafts[currentImageId]?.length ?? 0) > 0
        : false,
    );
    setCoverage(0);
    setWarning(null);
    setConfirmingClose(false);
  }, [currentImageId, getDraftSnapshot]);

  useEffect(() => {
    return () => {
      if (confirmCloseTimerRef.current) {
        clearTimeout(confirmCloseTimerRef.current);
        confirmCloseTimerRef.current = null;
      }
    };
  }, [currentImageId]);

  // modal 打开期间隔离 main，避免辅助技术和鼠标穿透到背景页面。
  useEffect(() => {
    const mainEls = Array.from(document.querySelectorAll("main"));
    const restore: Array<() => void> = [];
    for (const element of mainEls) {
      const previousInert = element.getAttribute("inert");
      const previousAriaHidden = element.getAttribute("aria-hidden");
      element.setAttribute("inert", "");
      element.setAttribute("aria-hidden", "true");
      restore.push(() => {
        if (previousInert === null) element.removeAttribute("inert");
        else element.setAttribute("inert", previousInert);
        if (previousAriaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", previousAriaHidden);
      });
    }
    return () => restore.forEach((restoreElement) => restoreElement());
  }, []);

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const animationFrame = requestAnimationFrame(() => {
      if (initialDraft.length === 0) {
        promptRef.current?.focus({ preventScroll: true });
      }
    });
    return () => {
      cancelAnimationFrame(animationFrame);
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        try {
          previouslyFocused.focus({ preventScroll: true });
        } catch {
          /* noop */
        }
      }
    };
    // 仅 mount 时执行；initialDraft 是首帧打开时的草稿。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cancelConfirmClose = useCallback(() => {
    if (!confirmingClose) return;
    setConfirmingClose(false);
    if (confirmCloseTimerRef.current) {
      clearTimeout(confirmCloseTimerRef.current);
      confirmCloseTimerRef.current = null;
    }
  }, [confirmingClose]);

  const handlePromptChange = useCallback(
    (value: string) => {
      setPrompt(value);
      cancelConfirmClose();
    },
    [cancelConfirmClose],
  );

  const handleStatsChange = useCallback(
    (stats: { coverage: number; strokeCount: number }) => {
      setCoverage(stats.coverage);
      setHasStroke(stats.strokeCount > 0);
    },
    [],
  );

  const handleStrokesChange = useCallback(
    (strokes: Stroke[]) => {
      if (!currentImageId) return;
      if (strokes.length === 0) clearMaskDraft(currentImageId);
      else setMaskDraft(currentImageId, strokes);
    },
    [clearMaskDraft, currentImageId, setMaskDraft],
  );

  useEffect(() => {
    if (!source) return;
    const timer = setTimeout(() => {
      setDraft(source.imageId, prompt);
    }, 350);
    return () => clearTimeout(timer);
  }, [prompt, setDraft, source]);

  useEffect(() => {
    if (!warning) return;
    const timer = setTimeout(() => setWarning(null), WARNING_AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [warning]);

  const dirty = hasStroke || prompt.trim().length > 0;
  const handleClose = useCallback(() => {
    if (submittingRef.current) return;
    if (!dirty) {
      close();
      return;
    }
    if (confirmingClose) {
      if (confirmCloseTimerRef.current) {
        clearTimeout(confirmCloseTimerRef.current);
        confirmCloseTimerRef.current = null;
      }
      setConfirmingClose(false);
      if (source) {
        clearDraft(source.imageId);
        clearMaskDraft(source.imageId);
      }
      close();
      return;
    }
    setConfirmingClose(true);
    if (confirmCloseTimerRef.current) {
      clearTimeout(confirmCloseTimerRef.current);
    }
    confirmCloseTimerRef.current = setTimeout(() => {
      setConfirmingClose(false);
      confirmCloseTimerRef.current = null;
    }, CLOSE_CONFIRM_TIMEOUT_MS);
  }, [
    clearDraft,
    clearMaskDraft,
    close,
    confirmingClose,
    dirty,
    source,
  ]);

  return {
    prompt,
    promptText: prompt.trim(),
    hasStroke,
    coverage,
    warning,
    setWarning,
    confirmingClose,
    submittingRef,
    handlePromptChange,
    handleStatsChange,
    handleStrokesChange,
    cancelConfirmClose,
    handleClose,
  };
}
