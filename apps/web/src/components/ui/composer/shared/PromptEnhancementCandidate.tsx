"use client";

import { Check, Loader2, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/primitives/Button";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import type { HapticKind } from "@/hooks/useHaptic";
import { enhancePrompt } from "@/lib/apiClient";
import { logError } from "@/lib/logger";

type PromptEnhancementStatus = "idle" | "streaming" | "ready";
type ComposerScope = "desktop-composer" | "mobile-composer";

interface PromptEnhancementState {
  status: PromptEnhancementStatus;
  candidate: string;
}

interface UsePromptEnhancementCandidateOptions {
  currentText: string;
  onApply: (candidate: string) => void;
  haptic: (kind: HapticKind) => void;
  scope: ComposerScope;
}

const IDLE_STATE: PromptEnhancementState = {
  status: "idle",
  candidate: "",
};

export function usePromptEnhancementCandidate({
  currentText,
  onApply,
  haptic,
  scope,
}: UsePromptEnhancementCandidateOptions) {
  const [state, setState] = useState<PromptEnhancementState>(IDLE_STATE);
  const abortRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);
  const sourceTextRef = useRef<string | null>(null);

  const reset = useCallback(() => {
    requestIdRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    sourceTextRef.current = null;
    setState(IDLE_STATE);
  }, []);

  useEffect(() => {
    if (
      state.status !== "idle" &&
      sourceTextRef.current !== null &&
      currentText !== sourceTextRef.current
    ) {
      reset();
    }
  }, [currentText, reset, state.status]);

  useEffect(() => {
    return () => {
      requestIdRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
      sourceTextRef.current = null;
    };
  }, []);

  const start = useCallback(
    async (sourceText: string) => {
      const source = sourceText.trim();
      if (!source) return;

      requestIdRef.current += 1;
      abortRef.current?.abort();
      const requestId = requestIdRef.current;
      const controller = new AbortController();
      abortRef.current = controller;
      sourceTextRef.current = sourceText;
      setState({ status: "streaming", candidate: "" });
      haptic("light");

      let accumulated = "";
      const isCurrentRequest = () =>
        requestIdRef.current === requestId &&
        abortRef.current === controller &&
        !controller.signal.aborted;

      try {
        await enhancePrompt(
          source,
          (delta) => {
            if (!isCurrentRequest()) return;
            accumulated += delta;
            setState({ status: "streaming", candidate: accumulated });
          },
          controller.signal,
        );
        if (!isCurrentRequest()) return;
        if (!accumulated.trim()) {
          throw new Error("Prompt enhancement returned an empty candidate");
        }
        setState({ status: "ready", candidate: accumulated });
        haptic("medium");
      } catch (error) {
        if (!isCurrentRequest() || controller.signal.aborted) return;
        logError(error, { scope, code: "enhance_failed" });
        sourceTextRef.current = null;
        setState(IDLE_STATE);
        haptic("error");
        pushMobileToast("润色失败，原文未改动", "danger");
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
      }
    },
    [haptic, scope],
  );

  const cancel = useCallback(() => {
    reset();
    haptic("light");
    pushMobileToast("已取消润色，原文未改动", "info");
  }, [haptic, reset]);

  const discard = useCallback(() => {
    reset();
    haptic("light");
    pushMobileToast("已放弃候选，原文未改动", "info");
  }, [haptic, reset]);

  const apply = useCallback(() => {
    if (state.status !== "ready" || !state.candidate.trim()) return;
    const candidate = state.candidate;
    reset();
    onApply(candidate);
    haptic("medium");
    pushMobileToast("已应用润色候选", "success");
  }, [haptic, onApply, reset, state]);

  const trigger = useCallback(
    (sourceText: string) => {
      if (state.status === "streaming") {
        cancel();
        return;
      }
      return start(sourceText);
    },
    [cancel, start, state.status],
  );

  return {
    status: state.status,
    candidate: state.candidate,
    isEnhancing: state.status === "streaming",
    triggerLabel:
      state.status === "streaming"
        ? "取消润色"
        : state.status === "ready"
          ? "重新润色"
          : "润色提示词",
    trigger,
    cancel,
    discard,
    apply,
  };
}

interface PromptEnhancementCandidateProps {
  status: PromptEnhancementStatus;
  candidate: string;
  onApply: () => void;
  onCancel: () => void;
  onDiscard: () => void;
}

export function PromptEnhancementCandidate({
  status,
  candidate,
  onApply,
  onCancel,
  onDiscard,
}: PromptEnhancementCandidateProps) {
  if (status === "idle") return null;

  const streaming = status === "streaming";

  return (
    <section
      aria-label="润色候选"
      aria-busy={streaming}
      className="mx-3 mt-2 overflow-hidden rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--bg-0)] shadow-[var(--shadow-1)]"
    >
      <header className="flex items-start gap-2.5 bg-[var(--accent-soft)] px-3 py-2.5">
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]">
          {streaming ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span
            role="status"
            aria-live="polite"
            className="block text-[13px] font-semibold text-[var(--fg-0)]"
          >
            {streaming ? "正在生成润色候选" : "润色候选已就绪"}
          </span>
          <span className="mt-0.5 block text-[11px] leading-4 text-[var(--fg-2)]">
            原文保留在输入框中，只有应用后才会替换。
          </span>
        </span>
        <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--bg-0)] px-2 py-1 text-[10px] font-medium text-[var(--accent)]">
          {streaming ? "生成中" : "待确认"}
        </span>
      </header>

      <div className="min-h-[72px] max-h-40 overflow-y-auto whitespace-pre-wrap break-words border-y border-[var(--border-subtle)] px-3 py-2.5 text-[13px] leading-6 text-[var(--fg-1)]">
        {candidate || "等待模型返回候选内容…"}
      </div>

      <footer className="grid grid-cols-2 gap-2 bg-[var(--bg-1)]/72 px-3 py-2">
        {streaming ? (
          <Button
            variant="secondary"
            size="sm"
            fullWidth
            className="col-span-2"
            leftIcon={<X className="h-3.5 w-3.5" aria-hidden />}
            onClick={onCancel}
          >
            取消润色
          </Button>
        ) : (
          <>
            <Button
              variant="secondary"
              size="sm"
              fullWidth
              leftIcon={<X className="h-3.5 w-3.5" aria-hidden />}
              onClick={onDiscard}
            >
              放弃
            </Button>
            <Button
              variant="primary"
              size="sm"
              fullWidth
              leftIcon={<Check className="h-3.5 w-3.5" aria-hidden />}
              onClick={onApply}
            >
              应用
            </Button>
          </>
        )}
      </footer>
    </section>
  );
}
