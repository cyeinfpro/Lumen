"use client";

// 桌面 Composer：56px 核心输入层 + 执行摘要 + Popover 高级设置。

import { AnimatePresence, motion } from "framer-motion";
import {
  type DragEvent as ReactDragEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
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
import {
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
import { DURATION, EASE, SPRING } from "@/lib/motion";
import {
  DesktopPopover,
} from "./DesktopPopover";
import {
  ComposerExecutionControls,
  COUNT_OPTIONS,
  QUALITY_OPTIONS,
  RENDER_QUALITY_OPTIONS,
} from "./DesktopComposerExecutionControls";
import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { useComposerAttachmentDnd } from "../shared/useComposerAttachmentDnd";
import { useMaskInpaint } from "../shared/useMaskInpaint";
import { AttachmentRoleBadge } from "../shared/AttachmentRoleBadge";
import { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import { buildComposerExecutionSummary } from "../shared/executionSummary";
import { useComposerCostEstimate } from "../shared/useComposerCostEstimate";
import { AspectRatioPicker } from "../shared/AspectRatioPicker";
import { LazyMaskCanvas } from "../LazyMaskCanvas";

interface DesktopComposerPillProps {
  onSubmit: () => void | Promise<void>;
}

type ComposerMode = "chat" | "image";

const REASONING_OPTIONS: { value: ReasoningEffort; label: string; hint: string }[] = [
  { value: "none", label: "最快", hint: "直接回复" },
  { value: "low", label: "低", hint: "轻量思考" },
  { value: "medium", label: "中", hint: "平衡" },
  { value: "high", label: "高", hint: "多想一步" },
  { value: "xhigh", label: "很高", hint: "更慢，适合复杂问题" },
];

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

function composerFrameClass(expanded: boolean, isDragActive: boolean): string {
  return cn(
    "fixed bottom-4 -translate-x-1/2",
    "max-w-[880px]",
    "overflow-visible",
    "rounded-[var(--radius-panel)]",
    "bg-[var(--bg-1)]/97",
    "border transition-[border-color,box-shadow] duration-[var(--dur-normal)]",
    isDragActive
      ? "border-[var(--accent)]"
      : "border-[var(--border-subtle)] focus-within:border-[var(--accent-border)]",
    expanded ? "shadow-[var(--shadow-2)]" : "shadow-[var(--shadow-1)]",
  );
}

function composerFrameWidth(expanded: boolean): string {
  return expanded
    ? "min(880px, calc(100vw - var(--studio-sidebar-offset, 0px) - 40px))"
    : "min(var(--content-composer), calc(100vw - var(--studio-sidebar-offset, 0px) - 40px))";
}

export function DesktopComposerPill({ onSubmit }: DesktopComposerPillProps) {
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
    const q = s.composer.params.render_quality;
    return q === "low" || q === "medium" || q === "high" ? q : "high";
  });
  const setRenderQuality = useChatStore((s) => s.setRenderQuality);
  const composerError = useChatStore((s) => s.composerError);
  const setComposerError = useChatStore((s) => s.setComposerError);

  const [expanded, setExpanded] = useState(false);
  const [isEnhancing, setIsEnhancing] = useState(false);
  const [originalText, setOriginalText] = useState<string | null>(null);
  const enhanceAbortRef = useRef<AbortController | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [shutterBurst, setShutterBurst] = useState(false);
  const [draggingAttachmentId, setDraggingAttachmentId] = useState<string | null>(
    null,
  );
  const { haptic } = useHaptic();
  const promptTooLong = isPromptTooLong(text);
  const shouldShowCount = text.length > MAX_PROMPT_CHARS * 0.8 || promptTooLong;

  const rootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const advancedTriggerRef = useRef<HTMLDivElement | null>(null);
  const isComposingRef = useRef(false);
  const submittingRef = useRef(false);
  const didMountRef = useRef(false);
  const shutterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragDepthRef = useRef(0);
  const draggingAttachmentIdRef = useRef<string | null>(null);

  // 展开/折叠 haptic（桌面无感，保留兼容）
  useEffect(() => {
    if (didMountRef.current) {
      haptic("medium");
    } else {
      didMountRef.current = true;
    }
  }, [expanded, haptic]);

  // ———— 监听外部 "lumen:composer-expand"（SuggestionCard / 全局 / 键触发） ————
  useEffect(() => {
    const onExpand = () => {
      setExpanded(true);
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    window.addEventListener("lumen:composer-expand", onExpand);
    return () => window.removeEventListener("lumen:composer-expand", onExpand);
  }, []);

  // ———— textarea 自动增高（展开态，max 200） ————
  // BUG-008: 用 rAF 批处理 height 读写，避免每次 keystroke 直接触发强制 layout。
  useEffect(() => {
    if (!expanded) return;
    const el = textareaRef.current;
    if (!el) return;
    const raf = requestAnimationFrame(() => {
      if (!el) return;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    });
    return () => cancelAnimationFrame(raf);
  }, [text, expanded]);

  useEffect(() => {
    return () => {
      enhanceAbortRef.current?.abort();
      isComposingRef.current = false;
      submittingRef.current = false;
      dragDepthRef.current = 0;
      draggingAttachmentIdRef.current = null;
      if (shutterTimerRef.current) {
        clearTimeout(shutterTimerRef.current);
        shutterTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!expanded) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (rootRef.current?.contains(target)) return;
      if (
        target instanceof Element &&
        target.closest("[data-lumen-composer-floating]")
      ) {
        return;
      }

      setExpanded(false);
      setAdvancedOpen(false);
      textareaRef.current?.blur();
    };

    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [expanded]);

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

  // ———— 全局键盘快捷键：/ 展开 Composer ————
  useEffect(() => {
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      // 跳过 IME 组合中
      if (e.isComposing) return;

      // "/" 展开 composer（不能在已聚焦输入控件内触发）
      if (e.key === "/") {
        const target = e.target as HTMLElement | null;
        if (target) {
          const tag = target.tagName;
          const editable =
            tag === "INPUT" ||
            tag === "TEXTAREA" ||
            target.isContentEditable ||
            tag === "SELECT";
          if (editable) return;
        }
        // 带修饰符不触发（避免误杀 ctrl+/ 等）
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("lumen:composer-expand"));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // BUG-017: canSubmit 必须反映 store 最新文本，避免闭包陈旧导致发送空消息。
  const canSubmit = (() => {
    if (isSending) return false;
    const latest = useChatStore.getState().composer;
    if (isPromptTooLong(latest.text)) return false;
    return latest.text.trim().length > 0 || latest.attachments.length > 0;
  })();

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
    const snapshot = useChatStore.getState().composer;
    const currentText = snapshot.text;
    if (currentText.trim().length === 0 && snapshot.attachments.length === 0) {
      return;
    }
    if (isPromptTooLong(currentText)) {
      setComposerError(PROMPT_TOO_LONG_MESSAGE);
      pushMobileToast(PROMPT_TOO_LONG_MESSAGE, "danger");
      return;
    }
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
    const parsed = parseSlash(currentText);
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
      // 发送成功后折叠
      setExpanded(false);
    } catch (err) {
      logError(err, { scope: "desktop-composer", code: "submit_failed" });
    } finally {
      submittingRef.current = false;
      setIsSending(false);
    }
  }, [onSubmit, setComposerError, setForceIntent, setText, haptic]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposingRef.current) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
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
      logError(err, { scope: "desktop-composer", code: "enhance_failed" });
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

  const insertImageMention = useCallback(
    (imageNumber: number) => {
      const current = useChatStore.getState().composer.text;
      const el = textareaRef.current;
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
        target.focus();
        target.setSelectionRange(result.selectionStart, result.selectionEnd);
      });
    },
    [setText],
  );

  const handleAttachmentDragStart = useCallback(
    (event: ReactDragEvent<HTMLDivElement>, id: string) => {
      event.stopPropagation();
      draggingAttachmentIdRef.current = id;
      setDraggingAttachmentId(id);
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", id);
    },
    [],
  );

  const handleAttachmentDragOver = useCallback(
    (event: ReactDragEvent<HTMLDivElement>) => {
      if (!draggingAttachmentIdRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "move";
    },
    [],
  );

  const handleAttachmentDrop = useCallback(
    (event: ReactDragEvent<HTMLDivElement>, targetId: string) => {
      event.preventDefault();
      event.stopPropagation();
      const sourceId =
        draggingAttachmentIdRef.current ||
        event.dataTransfer.getData("text/plain");
      if (sourceId && sourceId !== targetId) {
        moveAttachment(sourceId, targetId);
      }
      draggingAttachmentIdRef.current = null;
      setDraggingAttachmentId(null);
    },
    [moveAttachment],
  );

  const handleAttachmentDragEnd = useCallback(() => {
    draggingAttachmentIdRef.current = null;
    setDraggingAttachmentId(null);
  }, []);

  const expandAndFocus = () => {
    setExpanded(true);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const handleModeChange = useCallback(
    (nextMode: ComposerMode) => {
      setAdvancedOpen(false);
      setMode(nextMode);
    },
    [setMode],
  );

  const isImageMode = mode === "image";

  return (
    <>
    <motion.div
      ref={rootRef}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={(e) => void handleDrop(e)}
      initial={false}
      animate={{ height: expanded ? "auto" : 56 }}
      transition={SPRING.sheet}
      className={composerFrameClass(expanded, isDragActive)}
      style={{
        left: "calc(50% + var(--studio-sidebar-offset, 0px) / 2)",
        width: composerFrameWidth(expanded),
        zIndex: expanded
          ? ("var(--z-composer-expanded, 45)" as unknown as number)
          : ("var(--z-composer, 40)" as unknown as number),
      }}
    >
      {/* 折叠态：核心操作保持在一行 */}
      {!expanded && (
        <div className="flex h-14 items-center gap-2 px-2.5">
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
                  "absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1",
                  "rounded-full bg-[var(--amber-400)] text-[9px] font-bold text-[var(--bg-0)]",
                  "flex items-center justify-center tabular-nums",
                )}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {attachments.length}x
              </span>
            )}
          </IconBtn>

          <ModeSegment value={mode} onChange={handleModeChange} />

          <button
            type="button"
            onClick={expandAndFocus}
            aria-label="展开输入框"
            aria-expanded={false}
            className={cn(
              "flex-1 min-w-0 h-10 px-3 text-left rounded-[var(--radius-control)] cursor-text",
              "bg-transparent transition-colors",
              "hover:bg-[var(--bg-2)]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            )}
          >
            <span
              className={cn(
                "text-[14px] line-clamp-1",
                text ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
              )}
            >
              {text || "描述你想创作的内容…"}
            </span>
          </button>

          <SendButton
            canSubmit={canSubmit}
            isSending={isSending}
            burst={shutterBurst}
            onClick={() => void handleSubmit()}
            size="md"
          />
        </div>
      )}

      {/* 展开态 */}
      {expanded && (
        <div className="flex flex-col">
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
                    "mx-3 mt-3 flex items-center justify-center gap-2 rounded-[var(--radius-card)]",
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
                    draggable={attachments.length > 1}
                    onDragStart={(event) =>
                      handleAttachmentDragStart(event, att.id)
                    }
                    onDragOver={handleAttachmentDragOver}
                    onDrop={(event) => handleAttachmentDrop(event, att.id)}
                    onDragEnd={handleAttachmentDragEnd}
                    className={cn(
                      "relative shrink-0 w-16 h-16 rounded-[var(--radius-panel)] overflow-hidden",
                      "border bg-[var(--bg-2)]",
                      attachments.length > 1 &&
                        "cursor-grab active:cursor-grabbing",
                      draggingAttachmentId === att.id && "opacity-55",
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
                      onClick={() => insertImageMention(idx + 1)}
                      aria-label={`插入 @图${idx + 1}`}
                      title={`插入 @图${idx + 1}`}
                      className={cn(
                        "absolute top-0.5 left-0.5 h-5 px-1 rounded-[var(--radius-control)]",
                        "bg-[var(--bg-0)]/80 text-[10px] font-semibold text-[var(--amber-400)]",
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
                      onClick={() => attachmentRoles.cycleRole(att.id)}
                    />
                    <button
                      type="button"
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
              {/* 局部修改按钮：单张参考图 + image 模式时可用 */}
              {isImageMode && (
                <button
                  type="button"
                  onClick={inpaint.openInpaint}
                  disabled={inpaint.disabled}
                  aria-label="局部修改"
                  title={inpaint.tooltip}
                  className={cn(
                    "shrink-0 inline-flex flex-col items-center justify-center gap-0.5",
                    "w-16 h-16 rounded-[var(--radius-panel)] border text-[10px] font-medium",
                    "transition-colors",
                    inpaint.disabled
                      ? "border-[var(--border-subtle)] text-[var(--fg-3)] bg-[var(--bg-2)]/40 cursor-not-allowed"
                      : inpaint.maskActive
                        ? "border-[var(--amber-400)]/70 text-[var(--amber-400)] bg-[var(--amber-400)]/10 hover:bg-[var(--amber-400)]/15"
                        : "border-dashed border-[var(--border-subtle)] text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:border-[var(--border)]",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                  )}
                >
                  <SquareDashedMousePointer
                    className="w-4 h-4"
                    aria-hidden
                  />
                  <span>{inpaint.maskActive ? "重涂" : "局部"}</span>
                </button>
              )}
            </div>
          )}
          {attachmentRoles.hint && (
            <div className="px-3 pt-1 text-[11px] leading-4 text-[var(--fg-2)]">
              {attachmentRoles.hint}
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
                    className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--bg-2)]"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* 提示词已润色 */}
          <AnimatePresence>
            {originalText !== null && !isEnhancing && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: DURATION.quick }}
                className="overflow-hidden"
              >
                <div
                  className={cn(
                    "mx-3 mt-2 flex items-center gap-2 px-2.5 py-1 rounded-[var(--radius-card)]",
                    "bg-[var(--amber-400)]/10 border border-[var(--amber-400)]/25 text-[var(--amber-400)]",
                    "text-xs",
                  )}
                >
                  <Sparkles className="w-3 h-3 shrink-0" />
                  <span className="flex-1">提示词已润色</span>
                  <button
                    type="button"
                    onClick={handleUndoEnhance}
                    className="inline-flex items-center gap-1 text-xs underline decoration-dotted hover:text-[var(--fg-0)] transition-colors"
                  >
                    <Undo2 className="w-3 h-3" />
                    撤销
                  </button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* textarea */}
          <div className="px-3 pt-3">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
              }}
              onBlur={() => {
                isComposingRef.current = false;
              }}
              placeholder="描述画面，或直接提问...（⌘↵ 发送）"
              aria-label="输入提示词"
              maxLength={MAX_PROMPT_CHARS}
              rows={1}
              className={cn(
                "w-full bg-transparent outline-none resize-none",
                "text-body-md text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
                "min-h-11 max-h-[200px]",
              )}
            />
          </div>

          <div ref={advancedTriggerRef}>
            <ComposerExecutionControls
              mode={mode}
              summary={executionSummary}
              count={count}
              onCountChange={setImageCount}
              aspect={aspect}
              onAspectChange={setAspectRatio}
              quality={quality}
              onQualityChange={setQuality}
              renderQuality={renderQuality}
              onRenderQualityChange={setRenderQuality}
              fast={fast}
              onFastChange={setFast}
              attachmentCount={attachments.length}
              costLabel={costEstimate.label}
              costWarning={costEstimate.warning}
              onAdjust={() => setAdvancedOpen((value) => !value)}
            />
          </div>

          {/* 工具条 */}
          <div
            className={cn(
              "flex items-center gap-1.5 overflow-x-auto overflow-y-visible overscroll-x-contain no-scrollbar",
              "px-3 pb-3 pt-1.5",
            )}
          >
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
              label={isEnhancing ? "润色中..." : "润色提示词"}
              onClick={() => void handleEnhance()}
              disabled={isEnhancing || !text.trim()}
            >
              {isEnhancing ? (
                <Loader2 className="w-4 h-4 animate-spin text-[var(--amber-400)]" />
              ) : (
                <Sparkles className="w-4 h-4" />
              )}
            </IconBtn>

            <span className="w-px h-5 bg-[var(--border-subtle)] mx-0.5 shrink-0" aria-hidden />

            <ModeSegment value={mode} onChange={handleModeChange} />

            {shouldShowCount && (
              <span
                data-inline
                className={cn(
                  "text-caption tabular-nums transition-colors duration-200",
                  promptTooLong
                    ? "text-[var(--danger)]"
                    : "text-[var(--amber-400)]",
                )}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {text.length}/{MAX_PROMPT_CHARS}
              </span>
            )}

            <div className="flex-1 min-w-2" />

            <SendButton
              canSubmit={canSubmit}
              isSending={isSending}
              burst={shutterBurst}
              onClick={() => void handleSubmit()}
              size="lg"
            />
          </div>

          <DesktopPopover
            open={advancedOpen}
            onClose={() => setAdvancedOpen(false)}
            anchorRef={advancedTriggerRef}
            ariaLabel="高级执行设置"
            align="right"
            maxHeight="min(72vh, 620px)"
            className="w-[min(720px,calc(100vw-32px))] p-0"
          >
            <AdvancedComposerSettings
              mode={mode}
              quality={quality}
              onQualityChange={setQuality}
              renderQuality={renderQuality}
              onRenderQualityChange={setRenderQuality}
              aspect={aspect}
              onAspectChange={setAspectRatio}
              count={count}
              onCountChange={setImageCount}
              reasoningEffort={reasoningEffort ?? "medium"}
              onReasoningEffortChange={setReasoningEffort}
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
              onClose={() => setAdvancedOpen(false)}
            />
          </DesktopPopover>
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
    </motion.div>

    {/* 局部修改 mask 画布弹窗 */}
    {inpaint.open ? (
      <LazyMaskCanvas
        open={inpaint.open}
        imageSrc={inpaint.sourceImageSrc}
        onClose={inpaint.closeInpaint}
        onConfirm={inpaint.handleConfirm}
        submitting={inpaint.submitting}
      />
    ) : null}
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
        "relative shrink-0 inline-flex items-center justify-center w-10 h-10 rounded-[var(--radius-control)]",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
        "active:opacity-[var(--op-press)] transition-[background-color,color,opacity] duration-[var(--dur-quick)]",
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
  size = "lg",
}: {
  canSubmit: boolean;
  isSending: boolean;
  burst?: boolean;
  onClick: () => void;
  size?: "md" | "lg";
}) {
  const dim = size === "lg" ? "w-10 h-10" : "w-9 h-9";
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
        "shrink-0 inline-flex items-center justify-center rounded-full",
        dim,
        "transition-[background-color,box-shadow,opacity] duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/70",
        canSubmit
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
        <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
      ) : (
        <ArrowUp className="w-4 h-4" aria-hidden />
      )}
    </motion.button>
  );
}

function ModeSegment({
  value,
  onChange,
}: {
  value: ComposerMode;
  onChange: (v: ComposerMode) => void;
}) {
  return (
    <div className="shrink-0">
      <SegmentedControl<ComposerMode>
        value={value}
        onChange={onChange}
        ariaLabel="模式"
        density="compact"
        items={[
          {
            value: "chat",
            label: (
              <span className="inline-flex items-center gap-1.5">
                <MessageSquare className="w-3.5 h-3.5" aria-hidden />
                <span>对话</span>
              </span>
            ),
          },
          {
            value: "image",
            label: (
              <span className="inline-flex items-center gap-1.5">
                <Palette className="w-3.5 h-3.5" aria-hidden />
                <span>生图</span>
              </span>
            ),
          },
        ]}
      />
    </div>
  );
}

function AdvancedComposerSettings({
  mode,
  quality,
  onQualityChange,
  renderQuality,
  onRenderQualityChange,
  aspect,
  onAspectChange,
  count,
  onCountChange,
  reasoningEffort,
  onReasoningEffortChange,
  webSearch,
  onWebSearchChange,
  fileSearch,
  onFileSearchChange,
  codeInterpreter,
  onCodeInterpreterChange,
  imageGeneration,
  onImageGenerationChange,
  fast,
  onFastChange,
  onClose,
}: {
  mode: ComposerMode;
  quality: Quality;
  onQualityChange: (value: Quality) => void;
  renderQuality: RenderQualityChoice;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  aspect: AspectRatio;
  onAspectChange: (value: AspectRatio) => void;
  count: number;
  onCountChange: (value: number) => void;
  reasoningEffort: ReasoningEffort;
  onReasoningEffortChange: (value: ReasoningEffort) => void;
  webSearch: boolean;
  onWebSearchChange: (value: boolean) => void;
  fileSearch: boolean;
  onFileSearchChange: (value: boolean) => void;
  codeInterpreter: boolean;
  onCodeInterpreterChange: (value: boolean) => void;
  imageGeneration: boolean;
  onImageGenerationChange: (value: boolean) => void;
  fast: boolean;
  onFastChange: (value: boolean) => void;
  onClose: () => void;
}) {
  const imageMode = mode === "image";

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="text-[13px] font-semibold text-[var(--fg-0)]">
          执行设置
        </p>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭执行设置"
          className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="min-h-0 overflow-y-auto p-4">
        {imageMode ? (
          <div className="grid gap-5 lg:grid-cols-[minmax(220px,0.72fr)_minmax(360px,1.28fr)]">
            <div className="grid content-start gap-4">
              <section className="grid gap-2" aria-labelledby="image-output-settings">
                <h3
                  id="image-output-settings"
                  className="text-[11px] font-medium text-[var(--fg-2)]"
                >
                  输出
                </h3>
                <div className="grid grid-cols-2 gap-2">
                  <SettingSelect
                    label="尺寸"
                    value={quality}
                    onChange={(value) => onQualityChange(value as Quality)}
                    options={QUALITY_OPTIONS}
                  />
                  <SettingSelect
                    label="质量"
                    value={renderQuality}
                    onChange={(value) =>
                      onRenderQualityChange(value as RenderQualityChoice)
                    }
                    options={RENDER_QUALITY_OPTIONS}
                  />
                  <SettingSelect
                    label="数量"
                    value={String(count)}
                    onChange={(value) => onCountChange(Number(value))}
                    options={COUNT_OPTIONS.map((value) => ({
                      value: String(value),
                      label: `${value} 张`,
                    }))}
                  />
                </div>
              </section>

              <section className="grid gap-2" aria-labelledby="image-speed-settings">
                <h3
                  id="image-speed-settings"
                  className="text-[11px] font-medium text-[var(--fg-2)]"
                >
                  执行
                </h3>
                <ToggleRow
                  active={fast}
                  onClick={() => onFastChange(!fast)}
                  icon={<Zap className="h-4 w-4" aria-hidden />}
                  label="Fast"
                  detail="优先更快完成"
                />
              </section>
            </div>

            <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/56">
              <AspectRatioPicker
                value={aspect}
                onChange={onAspectChange}
                className="w-full max-w-none"
              />
            </div>
          </div>
        ) : (
          <div className="grid gap-5">
            <section className="grid gap-2" aria-labelledby="reasoning-settings">
              <h3
                id="reasoning-settings"
                className="text-[11px] font-medium text-[var(--fg-2)]"
              >
                推理强度
              </h3>
              <div className="grid gap-2 sm:grid-cols-5">
                {REASONING_OPTIONS.map((option) => {
                  const active = option.value === reasoningEffort;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => onReasoningEffortChange(option.value)}
                      aria-pressed={active}
                      className={cn(
                        "min-h-14 rounded-[var(--radius-card)] border px-3 py-2 text-left",
                        "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                        active
                          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
                          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
                      )}
                    >
                      <span className="block text-[12px] font-medium">
                        {option.label}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-[var(--fg-2)]">
                        {option.hint}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="grid gap-2" aria-labelledby="tool-settings">
              <h3
                id="tool-settings"
                className="text-[11px] font-medium text-[var(--fg-2)]"
              >
                工具
              </h3>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                <ToggleRow
                  active={webSearch}
                  onClick={() => onWebSearchChange(!webSearch)}
                  icon={<Globe2 className="h-4 w-4" aria-hidden />}
                  label="联网搜索"
                  detail="获取最新网页信息"
                />
                <ToggleRow
                  active={fileSearch}
                  onClick={() => onFileSearchChange(!fileSearch)}
                  icon={<FileSearch className="h-4 w-4" aria-hidden />}
                  label="文件检索"
                  detail="搜索已配置资料"
                />
                <ToggleRow
                  active={codeInterpreter}
                  onClick={() => onCodeInterpreterChange(!codeInterpreter)}
                  icon={<Code2 className="h-4 w-4" aria-hidden />}
                  label="代码工具"
                  detail="运行分析与计算"
                />
                <ToggleRow
                  active={imageGeneration}
                  onClick={() => onImageGenerationChange(!imageGeneration)}
                  icon={<ImagePlus className="h-4 w-4" aria-hidden />}
                  label="对话生图"
                  detail="允许回答中生成图片"
                />
                <ToggleRow
                  active={fast}
                  onClick={() => onFastChange(!fast)}
                  icon={<Zap className="h-4 w-4" aria-hidden />}
                  label="Fast"
                  detail="优先更快完成"
                />
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function SettingSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<{ value: string; label: string }>;
}) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[10px] text-[var(--fg-2)]">{label}</span>
      <span className="relative">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-10 w-full appearance-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 pr-8 text-[12px] text-[var(--fg-0)] outline-none transition-colors hover:bg-[var(--bg-2)] focus-visible:shadow-[var(--ring)]"
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]"
          aria-hidden
        />
      </span>
    </label>
  );
}

function ToggleRow({
  active,
  onClick,
  icon,
  label,
  detail,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  detail: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex min-h-14 items-center gap-3 rounded-[var(--radius-card)] border px-3 text-left",
        "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
      )}
    >
      <span
        className={cn(
          "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)]",
          active
            ? "bg-[var(--accent)] text-[var(--accent-on)]"
            : "bg-[var(--bg-2)] text-[var(--fg-2)]",
        )}
      >
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block text-[12px] font-medium">{label}</span>
        <span className="mt-0.5 block truncate text-[10px] text-[var(--fg-2)]">
          {detail}
        </span>
      </span>
    </button>
  );
}
