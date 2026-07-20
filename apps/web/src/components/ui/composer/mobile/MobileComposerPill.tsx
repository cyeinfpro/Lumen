"use client";

// 移动 Composer：56px 核心输入层 + 生图快捷参数 + BottomSheet 低频设置。

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
  AtSign,
  ArrowUp,
  Loader2,
  MessageSquare,
  Palette,
  Paperclip,
  RefreshCw,
  SquareDashedMousePointer,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  ActionSheet,
  BottomSheet,
  SegmentedControl,
  pushMobileToast,
} from "@/components/ui/primitives/mobile";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import { logError } from "@/lib/logger";
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
import {
  attachmentRoleLabel,
  useComposerAttachmentRoles,
} from "../shared/attachmentRoles";
import { buildComposerExecutionSummary } from "../shared/executionSummary";
import {
  allFlags,
  anyFlag,
  coalesceValue,
  firstAttachmentId,
  renderWhen,
  selectValue,
} from "../shared/composerViewState";
import { useComposerCostEstimate } from "../shared/useComposerCostEstimate";
import { AspectRatioPicker } from "../shared/AspectRatioPicker";
import {
  PromptEnhancementCandidate,
  usePromptEnhancementCandidate,
} from "../shared/PromptEnhancementCandidate";
import { LazyMaskCanvas } from "../LazyMaskCanvas";
import {
  MOBILE_REASONING_OPTIONS,
  MobileAdvancedSettings,
} from "./MobileAdvancedSettings";
import { MobileComposerExecutionControls } from "./MobileComposerExecutionControls";
import {
  canSubmitMobileComposer,
  deriveMobileComposerLayout,
  promptCounterColor,
  promptCounterText,
  shouldShowPromptCount,
} from "./mobileComposerViewState";

interface MobileComposerPillProps {
  onSubmit: () => void | Promise<void>;
  onMetricsChange?: (metrics: { height: number; bottom: number }) => void;
}

type ComposerMode = "chat" | "image";
type ComposerPanel = "none" | "advanced" | "aspect" | "reasoning";

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

function normalizedRenderQuality(value: unknown): "low" | "medium" | "high" {
  return value === "low" || value === "medium" || value === "high"
    ? value
    : "high";
}

function resolveAttachmentMenuRole(
  index: number,
  id: string | null,
  getRole: ReturnType<typeof useComposerAttachmentRoles>["getRole"],
) {
  if (index < 0 || !id) return null;
  return getRole(id);
}

function attachmentMenuDescription(
  role: ReturnType<typeof useComposerAttachmentRoles>["entries"][number]["role"] | null,
): string | undefined {
  return role ? `当前用途：${attachmentRoleLabel(role)}` : undefined;
}

function buildAttachmentMenuActions(input: {
  index: number;
  id: string | null;
  insertMention: (imageNumber: number) => void;
  cycleRole: (id: string) => void;
  removeAttachment: (id: string) => void;
}): React.ComponentProps<typeof ActionSheet>["actions"] {
  if (input.index < 0 || !input.id) return [];
  const imageNumber = input.index + 1;
  const id = input.id;
  return [
    {
      key: "mention",
      label: `插入 @图${imageNumber}`,
      icon: <AtSign className="h-5 w-5" aria-hidden />,
      onSelect: () => input.insertMention(imageNumber),
    },
    {
      key: "role",
      label: "切换图片用途",
      icon: <RefreshCw className="h-5 w-5" aria-hidden />,
      onSelect: () => input.cycleRole(id),
    },
    {
      key: "remove",
      label: "移除参考图",
      icon: <Trash2 className="h-5 w-5" aria-hidden />,
      destructive: true,
      onSelect: () => input.removeAttachment(id),
    },
  ];
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
  const quality = useChatStore((s) => s.composer.params.quality ?? "4k");
  const setQuality = useChatStore((s) => s.setQuality);
  const renderQuality = useChatStore((s) => {
    return normalizedRenderQuality(s.composer.params.render_quality);
  });
  const setRenderQuality = useChatStore((s) => s.setRenderQuality);
  const composerError = useChatStore((s) => s.composerError);
  const setComposerError = useChatStore((s) => s.setComposerError);

  const [expanded, setExpanded] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const {
    inset: keyboardInset,
    viewportBottom,
    viewportHeight,
  } = useKeyboardInset();
  const { keyboardOffset, expandedMaxHeight } = deriveMobileComposerLayout(
    keyboardInset,
    viewportHeight,
  );
  const [panel, setPanel] = useState<ComposerPanel>("none");
  const [shutterBurst, setShutterBurst] = useState(false);
  const [draggingAttachmentId, setDraggingAttachmentId] = useState<string | null>(
    null,
  );
  const [reorderTargetAttachmentId, setReorderTargetAttachmentId] = useState<
    string | null
  >(null);
  const [attachmentMenuId, setAttachmentMenuId] = useState<string | null>(null);
  const { haptic } = useHaptic();
  const promptEnhancement = usePromptEnhancementCandidate({
    currentText: text,
    onApply: setText,
    haptic,
    scope: "mobile-composer",
  });
  const isEnhancing = promptEnhancement.isEnhancing;
  const promptTooLong = isPromptTooLong(text);
  const shouldShowCount = shouldShowPromptCount(text, promptTooLong);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const collapsedTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const isComposingRef = useRef(false);
  const submittingRef = useRef(false);
  const didMountRef = useRef(false);
  const focusExpandedOnOpenRef = useRef(false);
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
        const visualBottom =
          viewportBottom > 0 ? viewportBottom : window.innerHeight;
        onMetricsChange({
          height: Math.ceil(rect.height),
          bottom: Math.ceil(Math.max(0, visualBottom - rect.bottom)),
        });
      });
    };

    const ro = new ResizeObserver(measure);
    ro.observe(root);
    window.addEventListener("resize", measure);
    measure();

    return () => {
      if (raf) window.cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [
    expanded,
    keyboardOffset,
    onMetricsChange,
    viewportBottom,
    viewportHeight,
  ]);

  // ———— textarea 自动增高（展开态）———— rAF 防抖避免每次击键都强制 reflow
  useEffect(() => {
    if (!expanded) return;
    const raf = window.requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.removeProperty("height");
      el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
    });
    return () => window.cancelAnimationFrame(raf);
  }, [text, expanded]);

  useEffect(() => {
    return () => {
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
    if (!expanded || panel !== "none") {
      return;
    }

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
  }, [expanded, panel]);

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

  const canSubmit = canSubmitMobileComposer({
    isSending,
    isEnhancing,
    promptTooLong,
    text,
    attachmentCount: attachments.length,
  });

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
    maskTargetAttachmentId: selectValue(
      inpaint.maskActive,
      firstAttachmentId(attachments),
      null,
    ),
  });
  const attachmentMenuIndex = attachments.findIndex(
    (attachment) => attachment.id === attachmentMenuId,
  );
  const attachmentMenuRole = resolveAttachmentMenuRole(
    attachmentMenuIndex,
    attachmentMenuId,
    attachmentRoles.getRole,
  );
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

  const handleSubmit = async () => {
    if (submittingRef.current) return;
    if (promptTooLong) {
      setComposerError(PROMPT_TOO_LONG_MESSAGE);
      pushMobileToast(PROMPT_TOO_LONG_MESSAGE, "danger");
      return;
    }
    if (!canSubmit) return;
    submittingRef.current = true;
    // 发送反馈仅保留短暂光晕；指针缩放由 Pressable 自己处理。
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
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposingRef.current) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (!canSubmit) return;
      void handleSubmit();
    }
  };

  const handleTextChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      setText(e.target.value);
    },
    [setText],
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
      requestAnimationFrame(() => {
        const target = textareaRef.current;
        if (!target) return;
        target.focus({ preventScroll: true });
        target.setSelectionRange(result.selectionStart, result.selectionEnd);
      });
    },
    [setText],
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
    collapsedTextareaRef.current?.blur();
    setPanel("aspect");
  }, [setPanel]);

  const openReasoningSheet = useCallback(() => {
    textareaRef.current?.blur();
    collapsedTextareaRef.current?.blur();
    setPanel("reasoning");
  }, [setPanel]);

  const openAdvancedSheet = useCallback(() => {
    textareaRef.current?.blur();
    collapsedTextareaRef.current?.blur();
    setPanel("advanced");
  }, [setPanel]);
  const closePanel = useCallback(() => setPanel("none"), [setPanel]);

  const isImageMode = mode === "image";
  const composerBottom = selectValue(
    Boolean(keyboardOffset),
    `calc(${keyboardOffset}px + 8px)`,
    "calc(var(--mobile-tabbar-height) + 6px)",
  );
  const composerMaxHeight = selectValue(expanded, expandedMaxHeight, "56px");
  const composerZIndex = selectValue(
    expanded,
    "var(--z-composer-expanded, 45)" as unknown as number,
    "var(--z-composer, 40)" as unknown as number,
  );
  const expandedPaddingBottom = selectValue(
    Boolean(keyboardOffset),
    "12px",
    "calc(env(safe-area-inset-bottom, 0px) + 12px)",
  );

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
          "rounded-[var(--radius-sheet)] mobile-perf-surface",
          "bg-[var(--bg-1)]/96",
          "border transition-[border-color,box-shadow] duration-[var(--dur-normal)]",
          selectValue(
            isDragActive,
            "border-[var(--accent)]",
            "border-[var(--border)] focus-within:border-[var(--accent-border)]",
          ),
          "shadow-[var(--shadow-2)]",
        )}
        style={{
          bottom: composerBottom,
          maxHeight: composerMaxHeight,
          zIndex: composerZIndex,
        }}
      >
        {/* 折叠态：单行 */}
        {renderWhen(!expanded, (
          <div className="flex h-14 items-center gap-1.5 px-2.5">
            <IconBtn
              label="添加参考图"
              onClick={openFilePicker}
              disabled={isUploading}
            >
              {selectValue(
                isUploading,
                <Loader2 className="w-4 h-4 animate-spin" />,
                <Paperclip className="w-4 h-4" />,
              )}
              {renderWhen(attachments.length > 0, (
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
              ))}
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
                  selectValue(
                    isImageMode,
                    "bg-[rgba(242,169,58,0.15)] text-[var(--amber-400)]",
                    "bg-[rgba(62,158,255,0.12)] text-[var(--info)]",
                  ),
                )}
              >
                {selectValue(isImageMode, "生图", "对话")}
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
                placeholder={selectValue(
                  isImageMode,
                  "描述画面...",
                  "直接提问...",
                )}
                aria-label="输入提示词"
                maxLength={MAX_PROMPT_CHARS}
                rows={1}
                enterKeyHint="send"
                className={cn(
                  "min-w-0 flex-1 h-10 resize-none overflow-hidden bg-transparent py-[9px]",
                  "text-[16px] leading-[22px] outline-none placeholder:text-[var(--fg-2)]",
                  selectValue(
                    Boolean(text),
                    "text-[var(--fg-0)]",
                    "text-[var(--fg-2)]",
                  ),
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
        ))}

        {/* 展开态 */}
        {renderWhen(expanded, (
          <div
            className="flex max-h-[inherit] min-h-0 flex-col overflow-y-auto overscroll-contain touch-pan-y"
            style={{ paddingBottom: expandedPaddingBottom }}
          >
            {/* 收起把手 */}
            <button
              type="button"
              onPointerDown={(e: ReactPointerEvent) => e.preventDefault()}
              onClick={() => setExpanded(false)}
              className="flex min-h-11 w-full items-center justify-center py-2 cursor-pointer active:opacity-60"
              aria-label="收起输入框"
            >
              <div className="w-9 h-1 rounded-full bg-[var(--fg-3)]/40" />
            </button>

            {/* 附件托盘 */}
            <AnimatePresence>
              {renderWhen(isDragActive, (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: DURATION.quick, ease: EASE.shutter }}
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
              ))}
            </AnimatePresence>

            {/* 附件托盘 */}
            {renderWhen(attachments.length > 0, (
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
                        "relative h-16 w-16 shrink-0 overflow-hidden rounded-[var(--radius-card)]",
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
                        onClick={() => setAttachmentMenuId(att.id)}
                        aria-label={`打开图 ${idx + 1} 操作`}
                        aria-haspopup="dialog"
                        className="absolute inset-0 z-10 rounded-[var(--radius-card)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--focus-ring)]"
                      >
                        <span className="sr-only">打开附件操作</span>
                      </button>
                      <span
                        aria-hidden
                        className="pointer-events-none absolute left-1 top-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-1 text-[9px] font-semibold leading-none text-[var(--media-control-fg)] backdrop-blur-sm"
                        style={{ fontFamily: "var(--font-mono)" }}
                      >
                        @图{idx + 1}
                      </span>
                      <span className="pointer-events-none absolute inset-x-1 bottom-1 truncate rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/88 px-1.5 py-1 text-center text-[9px] font-semibold leading-none text-[var(--fg-0)] backdrop-blur-sm">
                        {attachmentRoleLabel(role)}
                      </span>
                    </div>
                  );
                })}
                {renderWhen(isImageMode, (
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
                      selectValue(
                        inpaint.disabled,
                        "border-[var(--border-subtle)] text-[var(--fg-3)] bg-[var(--bg-2)]/40 cursor-not-allowed",
                        selectValue(
                          inpaint.maskActive,
                          "border-[var(--amber-400)]/70 text-[var(--amber-400)] bg-[var(--amber-400)]/10",
                          "border-dashed border-[var(--border-subtle)] text-[var(--fg-1)]",
                        ),
                      ),
                    )}
                  >
                    <SquareDashedMousePointer
                      className="w-3.5 h-3.5"
                      aria-hidden
                    />
                    <span>
                      {selectValue(inpaint.maskActive, "重涂", "局部")}
                    </span>
                  </button>
                ))}
              </div>
            ))}
            {renderWhen(Boolean(attachmentRoles.compactHint), (
              <div className="px-3 pt-1 text-[10.5px] leading-4 text-[var(--fg-2)] line-clamp-1">
                {attachmentRoles.compactHint}
              </div>
            ))}

            {/* 错误条 */}
            <AnimatePresence>
              {renderWhen(Boolean(composerError), (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: DURATION.quick, ease: EASE.shutter }}
                >
                  <div
                    role="alert"
                    className={cn(
                      "mx-3 mt-2 flex items-start gap-2 px-2.5 py-1.5 rounded-[var(--radius-card)]",
                      "bg-danger-soft border border-danger-border",
                      "type-caption text-[var(--danger-fg)]",
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
              ))}
            </AnimatePresence>

            <PromptEnhancementCandidate
              status={promptEnhancement.status}
              candidate={promptEnhancement.candidate}
              onApply={promptEnhancement.apply}
              onCancel={promptEnhancement.cancel}
              onDiscard={promptEnhancement.discard}
            />

            {/* textarea */}
            <div className="relative px-3 pt-1.5 pb-1">
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
                placeholder={selectValue(
                  isImageMode,
                  "描述画面...",
                  "直接提问...",
                )}
                aria-label="输入提示词"
                maxLength={MAX_PROMPT_CHARS}
                rows={2}
                className={cn(
                  "w-full bg-transparent outline-none resize-none",
                  "text-[16px] leading-relaxed text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
                  "min-h-[52px] max-h-[168px]",
                  selectValue(isEnhancing, "cursor-wait", undefined),
                )}
              />
            </div>

            <MobileComposerExecutionControls
              mode={mode}
              summary={executionSummary}
              count={count}
              onCountChange={setImageCount}
              aspect={aspect}
              onOpenAspect={openAspectSheet}
              quality={quality}
              onQualityChange={setQuality}
              renderQuality={renderQuality}
              onRenderQualityChange={setRenderQuality}
              fast={fast}
              onFastChange={setFast}
              attachmentCount={attachments.length}
              costLabel={costEstimate.label}
              costWarning={costEstimate.warning}
              onAdjust={openAdvancedSheet}
            />

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
                    {selectValue(
                      isUploading,
                      <Loader2 className="w-4 h-4 animate-spin" />,
                      <Paperclip className="w-4 h-4" />,
                    )}
                  </IconBtn>

                  <IconBtn
                    label={promptEnhancement.triggerLabel}
                    onClick={() => void promptEnhancement.trigger(text)}
                    disabled={allFlags(!isEnhancing, !text.trim())}
                  >
                    {selectValue(
                      isEnhancing,
                      <X className="w-4 h-4 text-[var(--danger)]" />,
                      <Sparkles className="w-4 h-4" />,
                    )}
                  </IconBtn>

                  <span
                    className="mx-0.5 h-5 w-px shrink-0 bg-[var(--border-subtle)]"
                    aria-hidden
                  />

                  {renderWhen(anyFlag(text.length > 0, shouldShowCount), (
                    <span
                      data-inline
                      className={cn(
                        "text-caption tabular-nums transition-colors duration-200",
                        promptCounterColor(
                          promptTooLong,
                          shouldShowCount,
                          text.length,
                        ),
                      )}
                      style={{ fontFamily: "var(--font-mono)" }}
                    >
                      {promptCounterText(shouldShowCount, text.length)}
                    </span>
                  ))}
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
        ))}

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

      <MobileAttachmentActionSheet
        index={attachmentMenuIndex}
        id={attachmentMenuId}
        role={attachmentMenuRole}
        onClose={() => setAttachmentMenuId(null)}
        onInsertMention={insertImageMention}
        onCycleRole={attachmentRoles.cycleRole}
        onRemove={removeAttachment}
      />

      <BottomSheet
        open={panel === "advanced"}
        onClose={closePanel}
        ariaLabel="执行设置"
        snapPoints={["80%"]}
      >
        <MobileAdvancedSettings
          mode={mode}
          quality={quality}
          onQualityChange={setQuality}
          renderQuality={renderQuality}
          onRenderQualityChange={setRenderQuality}
          aspect={aspect}
          onOpenAspect={openAspectSheet}
          count={count}
          onCountChange={setImageCount}
          reasoningEffort={coalesceValue(reasoningEffort, "medium")}
          onOpenReasoning={openReasoningSheet}
          webSearch={webSearch}
          onWebSearchChange={setWebSearch}
          fileSearch={fileSearch}
          onFileSearchChange={setFileSearch}
          codeInterpreter={codeInterpreter}
          onCodeInterpreterChange={setCodeInterpreter}
          imageGeneration={imageGeneration}
          onImageGenerationChange={setImageGeneration}
          fast={fast}
          onFastChange={setFast}
        />
      </BottomSheet>

      {/* 宽高比 BottomSheet */}
      <BottomSheet
        open={panel === "aspect"}
        onClose={closePanel}
        ariaLabel="选择宽高比"
      >
        <AspectRatioPicker
          value={aspect}
          onChange={setAspectRatio}
          onClose={closePanel}
          variant="sheet"
        />
      </BottomSheet>

      {/* 推理强度 BottomSheet */}
      <BottomSheet
        open={panel === "reasoning"}
        onClose={closePanel}
        ariaLabel="选择推理强度"
      >
        <SheetList
          title="推理强度"
          items={MOBILE_REASONING_OPTIONS.map((o) => ({
            key: o.value,
            label: o.label,
            hint: o.hint,
            selected: o.value === reasoningEffort,
            onSelect: () => {
              setReasoningEffort(o.value);
              setPanel("none");
            },
          }))}
        />
      </BottomSheet>

      {/* 局部修改 mask 画布弹窗 */}
      {renderWhen(inpaint.open, (
        <LazyMaskCanvas
          open={inpaint.open}
          imageSrc={inpaint.sourceImageSrc}
          onClose={inpaint.closeInpaint}
          onConfirm={inpaint.handleConfirm}
          submitting={inpaint.submitting}
        />
      ))}
    </>
  );
}

// ———————————————————————————————————————————————————
// 子组件
// ———————————————————————————————————————————————————

function MobileAttachmentActionSheet({
  index,
  id,
  role,
  onClose,
  onInsertMention,
  onCycleRole,
  onRemove,
}: {
  index: number;
  id: string | null;
  role: ReturnType<
    typeof useComposerAttachmentRoles
  >["entries"][number]["role"] | null;
  onClose: () => void;
  onInsertMention: (imageNumber: number) => void;
  onCycleRole: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  const title = selectValue<string | undefined>(
    index >= 0,
    `图 ${index + 1}`,
    undefined,
  );
  const actions = buildAttachmentMenuActions({
    index,
    id,
    insertMention: onInsertMention,
    cycleRole: onCycleRole,
    removeAttachment: onRemove,
  });
  return (
    <ActionSheet
      open={index >= 0}
      onClose={onClose}
      title={title}
      description={attachmentMenuDescription(role)}
      actions={actions}
    />
  );
}

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
        "relative shrink-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-[var(--radius-control)]",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
        "active:opacity-[var(--op-press)] transition-[background-color,color,opacity] duration-[var(--dur-quick)] motion-reduce:transition-none",
        "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
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
  const isActive = canSubmit || isSending;
  return (
    <Pressable
      size="inline"
      minHit={false}
      pressScale="tight"
      haptic={false}
      onPress={onClick}
      disabled={!canSubmit}
      aria-label="发送"
      aria-busy={isSending || undefined}
      className={cn(
        "shrink-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-full",
        "transition-[background-color,box-shadow,opacity] duration-200 motion-reduce:transition-none",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/70",
        isActive
          ? [
              "bg-[var(--amber-400)] text-[var(--bg-0)]",
              burst
                ? "shadow-[var(--shadow-amber)]"
                : "shadow-[var(--shadow-1)]",
            ].join(" ")
          : "bg-[var(--bg-3)] text-[var(--fg-3)] cursor-not-allowed",
      )}
    >
      {isSending ? (
        <Loader2 className="w-[18px] h-[18px] animate-spin" aria-hidden />
      ) : (
        <ArrowUp className="w-[18px] h-[18px]" aria-hidden />
      )}
    </Pressable>
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
                  className="h-2.5 w-2.5 rounded-full bg-[var(--accent)]"
                />
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
