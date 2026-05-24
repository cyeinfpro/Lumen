"use client";

// Darkroom 移动端 Composer Pill：折叠 48px，展开态向上生长。
// 展开态沿用桌面 composer 的工具条顺序，参数区换行展示，避免横向滚动。

import { AnimatePresence, motion } from "framer-motion";
import {
  type ChangeEvent,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ArrowUp,
  ChevronDown,
  Code2,
  FileSearch,
  Globe2,
  ImagePlus,
  Loader2,
  MessageSquare,
  Palette,
  Paperclip,
  SquareDashedMousePointer,
  Sparkles,
  Undo2,
  X,
  Zap,
} from "lucide-react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  BottomSheet,
  Chip,
  SegmentedControl,
  pushMobileToast,
} from "@/components/ui/primitives/mobile";
import { useChatStore, type ReasoningEffort } from "@/store/useChatStore";
import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import { cn } from "@/lib/utils";
import { logError } from "@/lib/logger";
import { enhancePrompt } from "@/lib/apiClient";
import {
  MAX_PROMPT_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  isPromptTooLong,
} from "@/lib/promptLimits";
import { insertImageMentionToken } from "@/lib/promptImageMentions";
import { useHaptic } from "@/hooks/useHaptic";
import { DURATION, EASE } from "@/lib/motion";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { useComposerAttachmentDnd } from "../shared/useComposerAttachmentDnd";
import { useMaskInpaint } from "../shared/useMaskInpaint";
import { AttachmentRoleBadge } from "../shared/AttachmentRoleBadge";
import { ExecutionSummaryBar } from "../shared/ExecutionSummaryBar";
import { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import { buildComposerExecutionSummary } from "../shared/executionSummary";
import { useComposerCostEstimate } from "../shared/useComposerCostEstimate";
import { MaskCanvas } from "../MaskCanvas";

interface MobileComposerPillProps {
  onSubmit: () => void | Promise<void>;
  onMetricsChange?: (metrics: { height: number; bottom: number }) => void;
}

type ComposerMode = "chat" | "image";

const ASPECT_OPTIONS: { value: AspectRatio; label: string; hint: string }[] = [
  { value: "1:1", label: "1:1", hint: "方形" },
  { value: "3:2", label: "3:2", hint: "横向标准" },
  { value: "2:3", label: "2:3", hint: "竖向标准" },
  { value: "4:3", label: "4:3", hint: "横向常规" },
  { value: "3:4", label: "3:4", hint: "竖向常规" },
  { value: "16:9", label: "16:9", hint: "横向宽屏" },
  { value: "9:16", label: "9:16", hint: "竖向宽屏" },
  { value: "21:9", label: "21:9", hint: "超宽电影" },
  { value: "9:21", label: "9:21", hint: "超竖超长" },
  { value: "4:5", label: "4:5", hint: "社交方形偏长" },
];

const REASONING_OPTIONS: { value: ReasoningEffort; label: string; hint: string }[] = [
  { value: "none", label: "最快", hint: "直接回复" },
  { value: "low", label: "低", hint: "轻量思考" },
  { value: "medium", label: "中", hint: "平衡" },
  { value: "high", label: "高", hint: "多想一步" },
  { value: "xhigh", label: "很高", hint: "更慢，适合复杂问题" },
];

const COUNT_OPTIONS = [1, 2, 4] as const;

const QUALITY_OPTIONS: { value: Quality; label: string }[] = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
];

const RENDER_QUALITY_OPTIONS: { value: RenderQualityChoice; label: string }[] = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const ATTACHMENT_REORDER_LONG_PRESS_MS = 220;
const ATTACHMENT_REORDER_MOVE_SLOP_PX = 10;

// 斜杠命令：/ask → chat；/image → image
function parseSlash(text: string): {
  stripped: string;
  force?: "chat" | "image";
} {
  const m = /^\s*\/(ask|image)(\s+|$)/i.exec(text);
  if (!m) return { stripped: text };
  const cmd = m[1].toLowerCase();
  return {
    stripped: text.slice(m[0].length).trim(),
    force: cmd === "ask" ? "chat" : "image",
  };
}

export function MobileComposerPill({
  onSubmit,
  onMetricsChange,
}: MobileComposerPillProps) {
  const text = useChatStore((s) => s.composer.text);
  const setText = useChatStore((s) => s.setText);
  const setForceIntent = useChatStore((s) => s.setForceIntent);
  const mode = useChatStore((s) => s.composer.mode);
  const setMode = useChatStore((s) => s.setMode);
  const attachments = useChatStore((s) => s.composer.attachments);
  const removeAttachment = useChatStore((s) => s.removeAttachment);
  const moveAttachment = useChatStore((s) => s.moveAttachment);
  const aspect = useChatStore((s) => s.composer.params.aspect_ratio);
  const setAspectRatio = useChatStore((s) => s.setAspectRatio);
  const count = useChatStore((s) => s.composer.params.count ?? 1);
  const setImageCount = useChatStore((s) => s.setImageCount);
  const reasoningEffort = useChatStore((s) => s.composer.reasoningEffort);
  const setReasoningEffort = useChatStore((s) => s.setReasoningEffort);
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);
  const webSearch = useChatStore((s) => s.composer.webSearch);
  const setWebSearch = useChatStore((s) => s.setWebSearch);
  const fileSearch = useChatStore((s) => s.composer.fileSearch);
  const setFileSearch = useChatStore((s) => s.setFileSearch);
  const codeInterpreter = useChatStore((s) => s.composer.codeInterpreter);
  const setCodeInterpreter = useChatStore((s) => s.setCodeInterpreter);
  const imageGeneration = useChatStore((s) => s.composer.imageGeneration);
  const setImageGeneration = useChatStore((s) => s.setImageGeneration);
  const quality = useChatStore((s) => s.composer.params.quality ?? "2k");
  const setQuality = useChatStore((s) => s.setQuality);
  const renderQuality = useChatStore((s) => {
    const q = s.composer.params.render_quality;
    return q === "low" || q === "medium" || q === "high" ? q : "medium";
  });
  const setRenderQuality = useChatStore((s) => s.setRenderQuality);
  const composerError = useChatStore((s) => s.composerError);
  const setComposerError = useChatStore((s) => s.setComposerError);

  const [expanded, setExpanded] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [isEnhancing, setIsEnhancing] = useState(false);
  const [originalText, setOriginalText] = useState<string | null>(null);
  const { inset: keyboardInset } = useKeyboardInset();
  const keyboardOffset = keyboardInset > 60 ? keyboardInset : 0;
  const [aspectSheetOpen, setAspectSheetOpen] = useState(false);
  const [reasoningSheetOpen, setReasoningSheetOpen] = useState(false);
  const [shutterBurst, setShutterBurst] = useState(false);
  const [draggingAttachmentId, setDraggingAttachmentId] = useState<string | null>(
    null,
  );
  const [reorderTargetAttachmentId, setReorderTargetAttachmentId] = useState<
    string | null
  >(null);
  const { haptic } = useHaptic();
  const expandedMaxHeight = keyboardOffset
    ? `calc(100dvh - ${keyboardOffset}px - env(safe-area-inset-top, 0px) - var(--system-banner-height, 0px) - 56px)`
    : "calc(100dvh - env(safe-area-inset-top, 0px) - var(--system-banner-height, 0px) - 96px - env(safe-area-inset-bottom, 0px))";
  const promptTooLong = isPromptTooLong(text);
  const shouldShowCount = text.length > MAX_PROMPT_CHARS * 0.8 || promptTooLong;

  const rootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const collapsedTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const isComposingRef = useRef(false);
  const submittingRef = useRef(false);
  const didMountRef = useRef(false);
  const focusExpandedOnOpenRef = useRef(false);
  const enhanceAbortRef = useRef<AbortController | null>(null);
  const shutterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragDepthRef = useRef(0);
  const draggingAttachmentIdRef = useRef<string | null>(null);
  const attachmentReorderStateRef = useRef<{
    pointerId: number;
    sourceId: string;
    startX: number;
    startY: number;
    active: boolean;
    lastTargetId: string | null;
    timer: ReturnType<typeof setTimeout> | null;
  } | null>(null);
  const attachmentReorderListenersRef = useRef<{
    move: ((event: PointerEvent) => void) | null;
    end: ((event: PointerEvent) => void) | null;
  } | null>(null);
  const suppressNextAttachmentClickRef = useRef(false);

  // 展开/折叠 haptic（跳过首次 mount）
  useEffect(() => {
    if (didMountRef.current) {
      haptic("medium");
    } else {
      didMountRef.current = true;
    }
  }, [expanded, haptic]);

  // ———— 监听外部 "lumen:composer-expand"（SuggestionCard 点击后触发） ————
  useEffect(() => {
    const onExpand = () => {
      focusExpandedOnOpenRef.current = true;
      setExpanded(true);
    };
    window.addEventListener("lumen:composer-expand", onExpand);
    return () => window.removeEventListener("lumen:composer-expand", onExpand);
  }, []);

  useLayoutEffect(() => {
    if (!expanded || !focusExpandedOnOpenRef.current) return;
    focusExpandedOnOpenRef.current = false;
    const el = textareaRef.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    const end = el.value.length;
    try {
      el.setSelectionRange(end, end);
    } catch {
      // Some Android WebViews can throw while IME composition is settling.
    }
  }, [expanded]);

  useLayoutEffect(() => {
    if (!onMetricsChange) return;
    const root = rootRef.current;
    if (!root || typeof window === "undefined") return;

    let raf = 0;
    const measure = () => {
      if (raf) window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(() => {
        raf = 0;
        const rect = root.getBoundingClientRect();
        onMetricsChange({
          height: Math.ceil(rect.height),
          bottom: Math.ceil(Math.max(0, window.innerHeight - rect.bottom)),
        });
      });
    };

    const ro = new ResizeObserver(measure);
    ro.observe(root);
    const vv = window.visualViewport;
    window.addEventListener("resize", measure);
    vv?.addEventListener("resize", measure);
    vv?.addEventListener("scroll", measure);
    measure();

    return () => {
      if (raf) window.cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", measure);
      vv?.removeEventListener("resize", measure);
      vv?.removeEventListener("scroll", measure);
    };
  }, [expanded, keyboardOffset, onMetricsChange]);

  // ———— textarea 自动增高（展开态）———— rAF 防抖避免每次击键都强制 reflow
  useEffect(() => {
    if (!expanded) return;
    const raf = window.requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
    });
    return () => window.cancelAnimationFrame(raf);
  }, [text, expanded]);

  useEffect(() => {
    return () => {
      enhanceAbortRef.current?.abort();
      isComposingRef.current = false;
      submittingRef.current = false;
      dragDepthRef.current = 0;
      draggingAttachmentIdRef.current = null;
      const reorder = attachmentReorderStateRef.current;
      if (reorder?.timer) clearTimeout(reorder.timer);
      const listeners = attachmentReorderListenersRef.current;
      if (listeners?.move) {
        window.removeEventListener("pointermove", listeners.move);
      }
      if (listeners?.end) {
        window.removeEventListener("pointerup", listeners.end);
        window.removeEventListener("pointercancel", listeners.end);
      }
      attachmentReorderListenersRef.current = null;
      attachmentReorderStateRef.current = null;
      if (shutterTimerRef.current) {
        clearTimeout(shutterTimerRef.current);
        shutterTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!expanded || aspectSheetOpen || reasoningSheetOpen) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (rootRef.current?.contains(target)) return;

      setExpanded(false);
      textareaRef.current?.blur();
      collapsedTextareaRef.current?.blur();
    };

    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [aspectSheetOpen, expanded, reasoningSheetOpen]);

  useEffect(() => {
    if (promptTooLong) {
      setComposerError(PROMPT_TOO_LONG_MESSAGE);
    } else if (composerError === PROMPT_TOO_LONG_MESSAGE) {
      setComposerError(null);
    }
  }, [composerError, promptTooLong, setComposerError]);

  // ———— 斜杠命令即时设置 forceIntent ————
  useEffect(() => {
    const parsed = parseSlash(text);
    if (parsed.force) {
      setForceIntent(parsed.force);
    } else {
      setForceIntent(undefined);
    }
  }, [text, setForceIntent]);

  const canSubmit =
    !isSending &&
    !isEnhancing &&
    !promptTooLong &&
    (text.trim().length > 0 || attachments.length > 0);

  const {
    handlePaste,
    handleFileInput,
    openFilePicker,
    handleDragEnter,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  } = useComposerAttachmentDnd({
    fileInputRef,
    dragDepthRef,
    setIsUploading,
    setIsDragActive,
    setExpanded,
  });

  const inpaint = useMaskInpaint();
  const attachmentRoles = useComposerAttachmentRoles({
    attachments,
    mode,
    maskTargetAttachmentId: inpaint.maskActive
      ? attachments[0]?.id ?? null
      : null,
  });
  const costEstimate = useComposerCostEstimate({
    mode,
    quality,
    aspect,
    count,
  });
  const executionSummary = buildComposerExecutionSummary({
    mode,
    attachmentCount: attachments.length,
    attachmentRoles: attachmentRoles.entries.map((entry) => entry.role),
    outputCount: count,
    aspect,
    quality,
    renderQuality,
    fast,
    maskActive: inpaint.maskActive,
    costLabel: costEstimate.label,
    costWarning: costEstimate.warning,
    reasoningEffort,
    webSearch,
    fileSearch,
    codeInterpreter,
    imageGeneration,
  });

  const handleSubmit = useCallback(async () => {
    if (submittingRef.current) return;
    if (promptTooLong) {
      setComposerError(PROMPT_TOO_LONG_MESSAGE);
      pushMobileToast(PROMPT_TOO_LONG_MESSAGE, "danger");
      return;
    }
    if (!canSubmit) return;
    submittingRef.current = true;
    // 快门闪：200ms scale 0.92→1 + 光晕
    setShutterBurst(true);
    haptic("medium");
    if (shutterTimerRef.current) clearTimeout(shutterTimerRef.current);
    shutterTimerRef.current = setTimeout(() => {
      shutterTimerRef.current = null;
      setShutterBurst(false);
    }, 200);
    // 斜杠命令最终落地：剥离前缀
    const parsed = parseSlash(text);
    if (parsed.force) {
      setForceIntent(parsed.force);
      setText(parsed.stripped);
    }
    setIsSending(true);
    try {
      const maybe = onSubmit();
      if (maybe && typeof (maybe as Promise<void>).then === "function") {
        await maybe;
      }
      // 折叠 Pill（发送成功后）
      setExpanded(false);
    } catch (err) {
      logError(err, { scope: "mobile-composer", code: "submit_failed" });
    } finally {
      submittingRef.current = false;
      setIsSending(false);
    }
  }, [
    canSubmit,
    promptTooLong,
    text,
    onSubmit,
    setComposerError,
    setForceIntent,
    setText,
    haptic,
  ]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposingRef.current) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (!canSubmit) return;
      void handleSubmit();
    }
  };

  const handleEnhance = useCallback(async () => {
    if (isEnhancing) {
      enhanceAbortRef.current?.abort();
      enhanceAbortRef.current = null;
      setIsEnhancing(false);
      if (!text.trim() && originalText) {
        setText(originalText);
        setOriginalText(null);
      }
      haptic("light");
      pushMobileToast("已取消润色", "success");
      return;
    }
    const current = text.trim();
    if (!current) return;
    setOriginalText(current);
    setIsEnhancing(true);
    haptic("light");
    setText("");
    const ctl = new AbortController();
    enhanceAbortRef.current = ctl;
    let accumulated = "";
    try {
      await enhancePrompt(
        current,
        (delta) => {
          accumulated += delta;
          setText(accumulated);
        },
        ctl.signal,
      );
      haptic("medium");
      pushMobileToast("提示词已润色", "success");
    } catch (err) {
      if (ctl.signal.aborted) return;
      logError(err, { scope: "mobile-composer", code: "enhance_failed" });
      setText(current);
      setOriginalText(null);
      pushMobileToast("润色失败", "danger");
    } finally {
      setIsEnhancing(false);
      enhanceAbortRef.current = null;
    }
  }, [text, isEnhancing, originalText, setText, haptic]);

  const handleUndoEnhance = useCallback(() => {
    if (originalText !== null) {
      setText(originalText);
      setOriginalText(null);
      haptic("light");
    }
  }, [originalText, setText, haptic]);

  const handleTextChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      setText(e.target.value);
      if (originalText !== null && !isEnhancing) {
        setOriginalText(null);
      }
    },
    [setText, originalText, isEnhancing],
  );

  const insertImageMention = useCallback(
    (imageNumber: number) => {
      const current = useChatStore.getState().composer.text;
      const el = textareaRef.current ?? collapsedTextareaRef.current;
      const result = insertImageMentionToken(
        current,
        imageNumber,
        el?.selectionStart,
        el?.selectionEnd,
      );
      setExpanded(true);
      setText(result.text);
      if (originalText !== null && !isEnhancing) {
        setOriginalText(null);
      }
      requestAnimationFrame(() => {
        const target = textareaRef.current;
        if (!target) return;
        target.focus({ preventScroll: true });
        target.setSelectionRange(result.selectionStart, result.selectionEnd);
      });
    },
    [isEnhancing, originalText, setText],
  );

  const resetAttachmentReorder = useCallback(
    (commit: boolean) => {
      const state = attachmentReorderStateRef.current;
      if (!state) return;
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      const listeners = attachmentReorderListenersRef.current;
      if (listeners?.move) {
        window.removeEventListener("pointermove", listeners.move);
      }
      if (listeners?.end) {
        window.removeEventListener("pointerup", listeners.end);
        window.removeEventListener("pointercancel", listeners.end);
      }
      attachmentReorderListenersRef.current = null;
      attachmentReorderStateRef.current = null;
      if (
        commit &&
        state.active &&
        state.lastTargetId &&
        state.lastTargetId !== state.sourceId
      ) {
        moveAttachment(state.sourceId, state.lastTargetId);
      }
      if (state.active) {
        suppressNextAttachmentClickRef.current = true;
      }
      draggingAttachmentIdRef.current = null;
      setDraggingAttachmentId(null);
      setReorderTargetAttachmentId(null);
    },
    [moveAttachment],
  );

  const beginAttachmentReorder = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>, id: string) => {
      if (attachments.length <= 1) return;
      if (!event.isPrimary || event.button !== 0) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("[data-composer-attachment-action='true']")) return;
      if (attachmentReorderStateRef.current) return;

      const state = {
        pointerId: event.pointerId,
        sourceId: id,
        startX: event.clientX,
        startY: event.clientY,
        active: false,
        lastTargetId: null,
        timer: null as ReturnType<typeof setTimeout> | null,
      };
      attachmentReorderStateRef.current = state;

      const moveListener = (nativeEvent: PointerEvent) => {
        const current = attachmentReorderStateRef.current;
        if (!current || nativeEvent.pointerId !== current.pointerId) return;

        const dx = nativeEvent.clientX - current.startX;
        const dy = nativeEvent.clientY - current.startY;
        if (!current.active) {
          if (Math.hypot(dx, dy) > ATTACHMENT_REORDER_MOVE_SLOP_PX) {
            resetAttachmentReorder(false);
          }
          return;
        }

        nativeEvent.preventDefault();
        const element = document.elementFromPoint(
          nativeEvent.clientX,
          nativeEvent.clientY,
        );
        const tile = element instanceof Element
          ? (element.closest("[data-composer-attachment-id]") as HTMLElement | null)
          : null;
        const targetId = tile?.dataset.composerAttachmentId ?? null;
        const nextTargetId =
          targetId && targetId !== current.sourceId ? targetId : null;
        current.lastTargetId = nextTargetId;
        setReorderTargetAttachmentId(nextTargetId);
      };

      const endListener = (nativeEvent: PointerEvent) => {
        const current = attachmentReorderStateRef.current;
        if (!current || nativeEvent.pointerId !== current.pointerId) return;
        if (current.active) nativeEvent.preventDefault();
        resetAttachmentReorder(current.active);
      };

      attachmentReorderListenersRef.current = {
        move: moveListener,
        end: endListener,
      };
      window.addEventListener("pointermove", moveListener, { passive: false });
      window.addEventListener("pointerup", endListener);
      window.addEventListener("pointercancel", endListener);

      state.timer = setTimeout(() => {
        const current = attachmentReorderStateRef.current;
        if (
          !current ||
          current.pointerId !== state.pointerId ||
          current.sourceId !== id
        ) {
          return;
        }
        current.active = true;
        current.timer = null;
        draggingAttachmentIdRef.current = current.sourceId;
        setDraggingAttachmentId(current.sourceId);
        setReorderTargetAttachmentId(null);
        haptic("light");
      }, ATTACHMENT_REORDER_LONG_PRESS_MS);
    },
    [attachments.length, haptic, resetAttachmentReorder],
  );

  const handleAttachmentClickCapture = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      if (!suppressNextAttachmentClickRef.current) return;
      suppressNextAttachmentClickRef.current = false;
      event.preventDefault();
      event.stopPropagation();
    },
    [],
  );

  const handleCollapsedFocus = () => {
    focusExpandedOnOpenRef.current = true;
    setExpanded(true);
  };

  const openAspectSheet = useCallback(() => {
    textareaRef.current?.blur();
    setAspectSheetOpen(true);
  }, []);

  const openReasoningSheet = useCallback(() => {
    textareaRef.current?.blur();
    setReasoningSheetOpen(true);
  }, []);

  const isImageMode = mode === "image";

  return (
    <>
      <div
        ref={rootRef}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={(e) => void handleDrop(e)}
        className={cn(
          "fixed inset-x-3 mx-auto max-w-[616px]",
          "overflow-hidden",
          "rounded-[var(--radius-panel)] backdrop-blur-xl mobile-perf-surface",
          "bg-[var(--bg-1)]/88 supports-[not(backdrop-filter:blur(1px))]:bg-[var(--bg-1)]/95",
          "border transition-[border-color,box-shadow] duration-200",
          isDragActive
            ? "border-[var(--amber-400)] ring-2 ring-[var(--amber-400)]/25"
            : isImageMode
            ? "border-[var(--border-amber)]"
            : "border-[var(--border-subtle)]",
          "shadow-[var(--shadow-2)]",
        )}
        style={{
          bottom: keyboardOffset
            ? `calc(${keyboardOffset}px + 8px)`
            : "calc(48px + 6px + env(safe-area-inset-bottom, 0px))",
          maxHeight: expanded ? expandedMaxHeight : 48,
          zIndex: expanded
            ? ("var(--z-composer-expanded, 45)" as unknown as number)
            : ("var(--z-composer, 40)" as unknown as number),
        }}
      >
        {/* 折叠态：单行 */}
        {!expanded && (
          <div className="flex items-center h-12 px-2.5 gap-1.5">
            <IconBtn
              label="添加参考图"
              onClick={openFilePicker}
              disabled={isUploading}
            >
              {isUploading ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Paperclip className="w-4 h-4" />
              )}
              {attachments.length > 0 && (
                <span
                  aria-hidden
                  className={cn(
                    "absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] px-1",
                    "rounded-full bg-[var(--amber-400)] text-[9px] font-bold text-[var(--bg-0)]",
                    "flex items-center justify-center tabular-nums",
                  )}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {attachments.length}
                </span>
              )}
            </IconBtn>

            <div
              className={cn(
                "flex-1 min-w-0 h-10 px-2 text-left",
                "bg-transparent cursor-text",
                "flex items-center gap-2",
              )}
            >
              <span
                aria-hidden
                data-inline
                className={cn(
                  "shrink-0 inline-flex items-center justify-center h-[18px] px-1.5 rounded-full",
                  "text-[11px] font-semibold tracking-wide leading-none",
                  isImageMode
                    ? "bg-[rgba(242,169,58,0.15)] text-[var(--amber-400)]"
                    : "bg-[rgba(62,158,255,0.12)] text-[var(--info)]",
                )}
              >
                {isImageMode ? "生图" : "对话"}
              </span>
              <textarea
                ref={collapsedTextareaRef}
                value={text}
                onFocus={handleCollapsedFocus}
                onChange={handleTextChange}
                onKeyDown={handleKeyDown}
                onPaste={handlePaste}
                onCompositionStart={() => {
                  isComposingRef.current = true;
                }}
                onCompositionEnd={() => {
                  isComposingRef.current = false;
                }}
                readOnly={isEnhancing}
                placeholder={isImageMode ? "描述画面..." : "直接提问..."}
                aria-label="输入提示词"
                maxLength={MAX_PROMPT_CHARS}
                rows={1}
                enterKeyHint="send"
                className={cn(
                  "min-w-0 flex-1 h-10 resize-none overflow-hidden bg-transparent py-[9px]",
                  "text-[16px] leading-[22px] outline-none placeholder:text-[var(--fg-2)]",
                  text ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
                )}
              />
            </div>

            <SendButton
              canSubmit={canSubmit}
              isSending={isSending}
              burst={shutterBurst}
              onClick={() => void handleSubmit()}
            />
          </div>
        )}

        {/* 展开态 */}
        {expanded && (
          <div
            className="flex max-h-[inherit] min-h-0 flex-col overflow-y-auto overscroll-contain touch-pan-y"
            style={{
              paddingBottom: keyboardOffset
                ? "12px"
                : "calc(env(safe-area-inset-bottom, 0px) + 12px)",
            }}
          >
            {/* 收起把手 */}
            <button
              type="button"
              onPointerDown={(e: ReactPointerEvent) => e.preventDefault()}
              onClick={() => setExpanded(false)}
              className="flex justify-center items-center pt-2.5 pb-1 cursor-pointer active:opacity-60"
              aria-label="收起输入框"
            >
              <div className="w-9 h-1 rounded-full bg-[var(--fg-3)]/40" />
            </button>

            {/* 附件托盘 */}
            <AnimatePresence>
              {isDragActive && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: DURATION.quick }}
                  className="overflow-hidden"
                >
                  <div
                    className={cn(
                      "mx-3 mt-2 flex items-center justify-center gap-2 rounded-[var(--radius-card)]",
                      "border border-dashed border-[var(--amber-400)]/60 bg-[var(--amber-400)]/10",
                      "px-3 py-3 text-xs text-[var(--amber-400)]",
                    )}
                  >
                    <Paperclip className="h-3.5 w-3.5" aria-hidden />
                    <span>松开上传图片，最多 {MAX_COMPOSER_ATTACHMENTS} 张</span>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* 附件托盘 */}
            {attachments.length > 0 && (
              <div
                className={cn(
                  "flex gap-2 overflow-x-auto overscroll-x-contain no-scrollbar",
                  "px-3 pt-3",
                )}
              >
                {attachments.map((att, idx) => {
                  const isFirst = idx === 0;
                  const showMaskBadge = isFirst && inpaint.maskActive;
                  const role = attachmentRoles.getRole(att.id);
                  return (
                    <div
                      key={att.id}
                      data-composer-attachment-id={att.id}
                      onPointerDown={(event) =>
                        beginAttachmentReorder(event, att.id)
                      }
                      onClickCapture={handleAttachmentClickCapture}
                      aria-grabbed={draggingAttachmentId === att.id || undefined}
                      className={cn(
                        "relative shrink-0 w-12 h-12 rounded-[var(--radius-card)] overflow-hidden",
                        "border bg-[var(--bg-2)]",
                        attachments.length > 1 &&
                          "cursor-grab active:cursor-grabbing",
                        draggingAttachmentId === att.id && "opacity-55 scale-[0.98]",
                        reorderTargetAttachmentId === att.id &&
                          "ring-2 ring-[var(--amber-400)]/70",
                        showMaskBadge
                          ? "border-[var(--amber-400)]/70"
                          : "border-[var(--border-subtle)]",
                      )}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={att.data_url}
                        alt=""
                        draggable={false}
                        className="w-full h-full object-cover"
                        loading="lazy"
                      />
                      <button
                        type="button"
                        data-composer-attachment-action="true"
                        onClick={() => insertImageMention(idx + 1)}
                        aria-label={`插入 @图${idx + 1}`}
                        title={`插入 @图${idx + 1}`}
                        className={cn(
                          "absolute top-0.5 left-0.5 h-4 px-1 rounded-[var(--radius-control)]",
                          "bg-[var(--bg-0)]/80 text-[8px] font-semibold text-[var(--amber-400)]",
                          "backdrop-blur-sm leading-none",
                          "active:scale-[0.94] transition-transform",
                        )}
                        style={{ fontFamily: "var(--font-mono)" }}
                      >
                        @图{idx + 1}
                      </button>
                      <AttachmentRoleBadge
                        role={role}
                        imageNumber={idx + 1}
                        compact
                        onClick={() => attachmentRoles.cycleRole(att.id)}
                      />
                      <button
                        type="button"
                        data-composer-attachment-action="true"
                        onClick={() => removeAttachment(att.id)}
                        aria-label="移除参考图"
                        className={cn(
                          "absolute top-0.5 right-0.5 w-5 h-5 rounded-full",
                          "bg-black/70 backdrop-blur-sm text-white",
                          "flex items-center justify-center",
                          "active:scale-[0.92] transition-transform",
                        )}
                      >
                        <X className="w-3 h-3" aria-hidden />
                      </button>
                    </div>
                  );
                })}
                {isImageMode && (
                  <button
                    type="button"
                    onClick={inpaint.openInpaint}
                    disabled={inpaint.disabled}
                    aria-label="局部修改"
                    title={inpaint.tooltip}
                    className={cn(
                      "shrink-0 inline-flex flex-col items-center justify-center gap-0.5",
                      "w-12 h-12 rounded-[var(--radius-card)] border text-[9px] font-medium",
                      "transition-colors",
                      inpaint.disabled
                        ? "border-[var(--border-subtle)] text-[var(--fg-3)] bg-[var(--bg-2)]/40 cursor-not-allowed"
                        : inpaint.maskActive
                          ? "border-[var(--amber-400)]/70 text-[var(--amber-400)] bg-[var(--amber-400)]/10"
                          : "border-dashed border-[var(--border-subtle)] text-[var(--fg-1)]",
                    )}
                  >
                    <SquareDashedMousePointer
                      className="w-3.5 h-3.5"
                      aria-hidden
                    />
                    <span>{inpaint.maskActive ? "重涂" : "局部"}</span>
                  </button>
                )}
              </div>
            )}
            {attachmentRoles.compactHint && (
              <div className="px-3 pt-1 text-[10.5px] leading-4 text-[var(--fg-2)] line-clamp-1">
                {attachmentRoles.compactHint}
              </div>
            )}

            {/* 错误条 */}
            <AnimatePresence>
              {composerError && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: DURATION.quick }}
                  className="overflow-hidden"
                >
                  <div
                    className={cn(
                      "mx-3 mt-2 flex items-start gap-2 px-2.5 py-1.5 rounded-[var(--radius-card)]",
                      "bg-[rgba(229,72,77,0.12)] border border-[rgba(229,72,77,0.4)]",
                      "text-xs text-[var(--danger)]",
                    )}
                  >
                    <span className="flex-1 break-words">{composerError}</span>
                    <button
                      type="button"
                      aria-label="关闭错误提示"
                      onClick={() => setComposerError(null)}
                      className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-[var(--radius-control)] active:bg-[var(--bg-2)]"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* 提示词润色状态条 */}
            <AnimatePresence>
              {(isEnhancing || (originalText !== null && !isEnhancing)) && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: DURATION.quick }}
                  className="overflow-hidden"
                >
                  <div
                    className={cn(
                      "mx-3 mt-2 flex items-center gap-2 px-3 py-2 rounded-[var(--radius-panel)]",
                      "bg-[rgba(242,169,58,0.08)] border border-[rgba(242,169,58,0.18)]",
                      "text-xs",
                    )}
                  >
                    {isEnhancing ? (
                      <Loader2 className="w-3.5 h-3.5 shrink-0 text-[var(--amber-400)] animate-spin" aria-hidden />
                    ) : (
                      <Sparkles className="w-3.5 h-3.5 shrink-0 text-[var(--amber-400)]" aria-hidden />
                    )}
                    <span className="flex-1 text-[var(--fg-1)]">
                      {isEnhancing ? "正在润色..." : "提示词已润色"}
                    </span>
                    {!isEnhancing && (
                      <>
                        <button
                          type="button"
                          data-inline
                          onClick={handleUndoEnhance}
                          className={cn(
                            "inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)]",
                            "text-xs font-medium text-[var(--amber-400)]",
                            "bg-[rgba(242,169,58,0.1)] active:bg-[rgba(242,169,58,0.2)]",
                            "transition-colors",
                          )}
                        >
                          <Undo2 className="w-3 h-3" aria-hidden />
                          撤销
                        </button>
                        <button
                          type="button"
                          data-inline
                          onClick={() => setOriginalText(null)}
                          aria-label="关闭提示"
                          className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)] active:text-[var(--fg-0)] transition-colors"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* textarea */}
            <div
              className={cn(
                "relative px-3 pt-1.5 pb-1",
                isEnhancing && "after:absolute after:left-3 after:right-3 after:bottom-1 after:h-0.5 after:rounded-full after:bg-[var(--amber-400)]/40 after:animate-pulse-soft",
              )}
            >
              <textarea
                ref={textareaRef}
                value={text}
                onChange={handleTextChange}
                onKeyDown={handleKeyDown}
                onPaste={handlePaste}
                onCompositionStart={() => {
                  isComposingRef.current = true;
                }}
                onCompositionEnd={() => {
                  isComposingRef.current = false;
                }}
                readOnly={isEnhancing}
                placeholder={isImageMode ? "描述画面..." : "直接提问..."}
                aria-label="输入提示词"
                maxLength={MAX_PROMPT_CHARS}
                rows={2}
                className={cn(
                  "w-full bg-transparent outline-none resize-none",
                  "text-[16px] leading-relaxed placeholder:text-[var(--fg-2)]",
                  "min-h-[52px] max-h-[168px]",
                  isEnhancing
                    ? "text-[var(--amber-300)] cursor-default"
                    : "text-[var(--fg-0)]",
                )}
              />
            </div>

            <ExecutionSummaryBar summary={executionSummary} compact />

            {/* 分隔线 */}
            <div className="mx-3 h-px bg-[var(--border-subtle)]" />

            {/* 工具区 */}
            <div className="flex items-end gap-2 px-3 pb-3 pt-2">
              <div className="flex-1 min-w-0 flex flex-col gap-2">
                <ModeSegment
                  value={mode}
                  onChange={(v) => setMode(v)}
                  className="w-full"
                />

                <div className="flex flex-wrap items-center gap-1.5">
                  <IconBtn
                    label="添加参考图"
                    onClick={openFilePicker}
                    disabled={isUploading}
                  >
                    {isUploading ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Paperclip className="w-4 h-4" />
                    )}
                  </IconBtn>

                  <IconBtn
                    label={isEnhancing ? "取消润色" : "润色提示词"}
                    onClick={() => void handleEnhance()}
                    disabled={!isEnhancing && !text.trim()}
                  >
                    {isEnhancing ? (
                      <X className="w-4 h-4 text-[var(--danger)]" />
                    ) : (
                      <Sparkles className="w-4 h-4" />
                    )}
                  </IconBtn>

                  <span
                    className="mx-0.5 h-5 w-px shrink-0 bg-[var(--border-subtle)]"
                    aria-hidden
                  />

                  {isImageMode && (
                    <div
                      role="group"
                      aria-label="尺寸选择"
                      className={cn(
                        "shrink-0 inline-flex items-center h-8 p-px rounded-full",
                        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
                      )}
                    >
                      {QUALITY_OPTIONS.map((o) => {
                        const active = quality === o.value;
                        return (
                          <button
                            key={o.value}
                            type="button"
                            data-inline
                            onClick={() => setQuality(o.value)}
                            className={cn(
                              "inline-flex items-center justify-center h-7 min-w-8 px-2 rounded-full",
                              "text-xs tabular-nums transition-colors",
                              active
                                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                                : "text-[var(--fg-1)] active:text-[var(--fg-0)]",
                            )}
                            aria-pressed={active}
                            style={{ fontFamily: "var(--font-mono)" }}
                          >
                            {o.label}
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {isImageMode && (
                    <div
                      role="group"
                      aria-label="渲染质量"
                      className={cn(
                        "shrink-0 inline-flex items-center h-8 p-px rounded-full",
                        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
                      )}
                    >
                      {RENDER_QUALITY_OPTIONS.map((o) => {
                        const active = renderQuality === o.value;
                        return (
                          <button
                            key={o.value}
                            type="button"
                            data-inline
                            onClick={() => setRenderQuality(o.value)}
                            className={cn(
                              "inline-flex items-center justify-center h-7 min-w-7 px-2 rounded-full",
                              "text-xs tabular-nums transition-colors",
                              active
                                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                                : "text-[var(--fg-1)] active:text-[var(--fg-0)]",
                            )}
                            aria-pressed={active}
                          >
                            {o.label}
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {isImageMode && (
                    <button
                      type="button"
                      data-inline
                      onClick={openAspectSheet}
                      className={cn(
                        "shrink-0 inline-flex items-center gap-1 h-8 px-2.5 rounded-full",
                        "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                        "text-xs text-[var(--fg-1)] active:text-[var(--fg-0)]",
                        "whitespace-nowrap active:scale-[0.96] transition-all duration-150",
                      )}
                      aria-label={`宽高比 ${aspect}`}
                      style={{ fontFamily: "var(--font-mono)" }}
                    >
                      {aspect}
                      <ChevronDown className="w-3 h-3" aria-hidden />
                    </button>
                  )}

                  {isImageMode && (
                    <div
                      role="group"
                      aria-label="图像数量"
                      className={cn(
                        "shrink-0 inline-flex items-center h-8 p-px rounded-full",
                        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
                      )}
                    >
                      {COUNT_OPTIONS.map((n) => {
                        const active = count === n;
                        return (
                          <button
                            key={n}
                            type="button"
                            data-inline
                            onClick={() => setImageCount(n)}
                            className={cn(
                              "inline-flex items-center justify-center h-7 min-w-8 px-2 rounded-full",
                              "text-xs tabular-nums transition-colors",
                              active
                                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                                : "text-[var(--fg-1)] active:text-[var(--fg-0)]",
                            )}
                            aria-pressed={active}
                            style={{ fontFamily: "var(--font-mono)" }}
                          >
                            x{n}
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {!isImageMode && (
                    <button
                      type="button"
                      data-inline
                      onClick={openReasoningSheet}
                      className={cn(
                        "shrink-0 inline-flex items-center gap-1 h-8 px-2.5 rounded-full",
                        "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                        "text-xs text-[var(--fg-1)] active:text-[var(--fg-0)]",
                        "whitespace-nowrap active:scale-[0.96] transition-all duration-150",
                      )}
                      aria-label="推理强度"
                    >
                      {REASONING_OPTIONS.find((r) => r.value === reasoningEffort)?.label ??
                        "默认"}
                      <ChevronDown className="w-3 h-3" aria-hidden />
                    </button>
                  )}

                  {!isImageMode && (
                    <>
                      <Chip
                        active={webSearch}
                        onClick={() => setWebSearch(!webSearch)}
                        icon={<Globe2 className="w-3.5 h-3.5" aria-hidden />}
                      >
                        搜索
                      </Chip>
                      <Chip
                        active={fileSearch}
                        onClick={() => setFileSearch(!fileSearch)}
                        icon={<FileSearch className="w-3.5 h-3.5" aria-hidden />}
                        title="需要配置 vector store"
                      >
                        文件
                      </Chip>
                      <Chip
                        active={codeInterpreter}
                        onClick={() => setCodeInterpreter(!codeInterpreter)}
                        icon={<Code2 className="w-3.5 h-3.5" aria-hidden />}
                      >
                        代码
                      </Chip>
                      <Chip
                        active={imageGeneration}
                        onClick={() => setImageGeneration(!imageGeneration)}
                        icon={<ImagePlus className="w-3.5 h-3.5" aria-hidden />}
                      >
                        生图
                      </Chip>
                    </>
                  )}

                  <Chip
                    active={fast}
                    onClick={() => setFast(!fast)}
                    icon={<Zap className="w-3.5 h-3.5" aria-hidden />}
                  >
                    Fast
                  </Chip>

                  {(text.length > 0 || shouldShowCount) && (
                    <span
                      data-inline
                      className={cn(
                        "text-caption tabular-nums transition-colors duration-200",
                        promptTooLong
                          ? "text-[var(--danger)]"
                          : shouldShowCount || text.length > 500
                            ? "text-[var(--amber-400)]"
                            : "text-[var(--fg-3)]",
                      )}
                      style={{ fontFamily: "var(--font-mono)" }}
                    >
                      {shouldShowCount
                        ? `${text.length}/${MAX_PROMPT_CHARS}`
                        : text.length}
                    </span>
                  )}
                </div>
              </div>

              <SendButton
                canSubmit={canSubmit}
                isSending={isSending}
                burst={shutterBurst}
                onClick={() => void handleSubmit()}
              />
            </div>
          </div>
        )}

        {/* 隐藏文件输入 */}
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          disabled={attachments.length >= MAX_COMPOSER_ATTACHMENTS}
          hidden
          onChange={handleFileInput}
        />
      </div>

      {/* 宽高比 BottomSheet */}
      <BottomSheet
        open={aspectSheetOpen}
        onClose={() => setAspectSheetOpen(false)}
        ariaLabel="选择宽高比"
      >
        <SheetList
          title="宽高比"
          items={ASPECT_OPTIONS.map((o) => ({
            key: o.value,
            label: o.label,
            hint: o.hint,
            selected: o.value === aspect,
            onSelect: () => {
              setAspectRatio(o.value);
              setAspectSheetOpen(false);
            },
          }))}
        />
      </BottomSheet>

      {/* 推理强度 BottomSheet */}
      <BottomSheet
        open={reasoningSheetOpen}
        onClose={() => setReasoningSheetOpen(false)}
        ariaLabel="选择推理强度"
      >
        <SheetList
          title="推理强度"
          items={REASONING_OPTIONS.map((o) => ({
            key: o.value,
            label: o.label,
            hint: o.hint,
            selected: o.value === reasoningEffort,
            onSelect: () => {
              setReasoningEffort(o.value);
              setReasoningSheetOpen(false);
            },
          }))}
        />
      </BottomSheet>

      {/* 局部修改 mask 画布弹窗 */}
      <MaskCanvas
        open={inpaint.open}
        imageSrc={inpaint.sourceImageSrc}
        onClose={inpaint.closeInpaint}
        onConfirm={inpaint.handleConfirm}
        submitting={inpaint.submitting}
      />
    </>
  );
}

// ———————————————————————————————————————————————————
// 子组件
// ———————————————————————————————————————————————————

function IconBtn({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        "relative shrink-0 inline-flex items-center justify-center w-10 h-10 rounded-full",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
        "active:scale-[0.94] transition-all duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        "disabled:opacity-40 disabled:cursor-not-allowed",
      )}
    >
      {children}
    </button>
  );
}

function SendButton({
  canSubmit,
  isSending,
  burst,
  onClick,
}: {
  canSubmit: boolean;
  isSending: boolean;
  burst?: boolean;
  onClick: () => void;
}) {
  return (
    <motion.button
      type="button"
      onClick={onClick}
      disabled={!canSubmit}
      aria-label="发送"
      whileTap={canSubmit ? { scale: 0.92 } : undefined}
      animate={burst ? { scale: [0.92, 1] } : { scale: 1 }}
      transition={{ duration: DURATION.normal, ease: EASE.shutter }}
      className={cn(
        "shrink-0 inline-flex items-center justify-center w-10 h-10 rounded-full",
        "transition-[background-color,box-shadow,opacity] duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/70",
        canSubmit
          ? [
              "bg-[var(--amber-400)] text-[var(--bg-0)]",
              burst
                ? "shadow-[var(--shadow-amber)]"
                : "shadow-[var(--shadow-shutter)]",
            ].join(" ")
          : "bg-[var(--bg-3)] text-[var(--fg-3)] cursor-not-allowed",
      )}
    >
      {isSending ? (
        <Loader2 className="w-[18px] h-[18px] animate-spin" aria-hidden />
      ) : (
        <ArrowUp className="w-[18px] h-[18px]" aria-hidden />
      )}
    </motion.button>
  );
}

function ModeSegment({
  value,
  onChange,
  className,
}: {
  value: ComposerMode;
  onChange: (v: ComposerMode) => void;
  className?: string;
}) {
  return (
    <SegmentedControl<ComposerMode>
      value={value}
      onChange={onChange}
      ariaLabel="模式"
      className={className}
      items={[
        {
          value: "chat",
          label: (
            <>
              <MessageSquare className="w-3.5 h-3.5 shrink-0" aria-hidden />
              <span>对话</span>
            </>
          ),
        },
        {
          value: "image",
          label: (
            <>
              <Palette className="w-3.5 h-3.5 shrink-0" aria-hidden />
              <span>生图</span>
            </>
          ),
        },
      ]}
    />
  );
}

// BottomSheet 列表
function SheetList({
  title,
  items,
}: {
  title: string;
  items: Array<{
    key: string;
    label: string;
    hint?: string;
    selected: boolean;
    onSelect: () => void;
  }>;
}) {
  return (
    <div className="px-4 pb-5">
      <div className="py-3.5 text-center text-[15px] font-semibold text-[var(--fg-0)] border-b border-[var(--border-subtle)]">
        {title}
      </div>
      <ul className="flex flex-col">
        {items.map((it) => (
          <li
            key={it.key}
            className="border-b border-[var(--border-subtle)] last:border-b-0"
          >
            <button
              type="button"
              onClick={it.onSelect}
              className={cn(
                "w-full min-h-[48px] flex items-center gap-3 px-3 py-2 text-left",
                "text-[15px] rounded-[var(--radius-card)] active:bg-[var(--bg-2)] transition-colors",
                it.selected ? "text-[var(--amber-300)] font-medium" : "text-[var(--fg-0)]",
              )}
            >
              <span className="flex-1">{it.label}</span>
              {it.hint && (
                <span className="text-body-sm text-[var(--fg-2)]">{it.hint}</span>
              )}
              {it.selected && (
                <span
                  aria-hidden
                  className="w-2.5 h-2.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                />
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
