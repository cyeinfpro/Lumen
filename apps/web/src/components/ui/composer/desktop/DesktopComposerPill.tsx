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
  Loader2,
  Paperclip,
  Sparkles,
  X,
} from "lucide-react";
import {
  pushMobileToast,
} from "@/components/ui/primitives/mobile";
import { Badge, Button, IconButton } from "@/components/ui/primitives";
import { useChatStore } from "@/store/useChatStore";
import type { ComposerMode } from "@/store/chat/types";
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
import {
  DesktopPopover,
} from "./DesktopPopover";
import {
  ComposerExecutionControls,
} from "./DesktopComposerExecutionControls";
import {
  IconBtn,
  ModeSegment,
  SendButton,
} from "./DesktopComposerButtons";
import { AdvancedComposerSettings } from "./DesktopComposerAdvancedSettings";
import { DesktopComposerAttachmentTray } from "./DesktopComposerAttachmentTray";
import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { useComposerAttachmentDnd } from "../shared/useComposerAttachmentDnd";
import { useMaskInpaint } from "../shared/useMaskInpaint";
import { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import { buildComposerExecutionSummary } from "../shared/executionSummary";
import {
  allFlags,
  anyFlag,
  coalesceValue,
  fallbackText,
  firstAttachmentId,
  renderWhen,
  selectValue,
} from "../shared/composerViewState";
import { useComposerCostEstimate } from "../shared/useComposerCostEstimate";
import {
  PromptEnhancementCandidate,
  usePromptEnhancementCandidate,
} from "../shared/PromptEnhancementCandidate";
import { LazyMaskCanvas } from "../LazyMaskCanvas";
import {
  desktopComposerFrameClass,
  desktopComposerFrameWidth,
  parseDesktopComposerSlash,
} from "./desktopComposerPresentation";

interface DesktopComposerPillProps {
  onSubmit: () => void | Promise<void>;
  onMetricsChange?: (metrics: { height: number; bottom: number }) => void;
}

export function DesktopComposerPill({
  onSubmit,
  onMetricsChange,
}: DesktopComposerPillProps) {
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
  const transparentBackground = useChatStore(
    (s) => s.composer.params.background === "transparent",
  );
  const setTransparentBackground = useChatStore(
    (s) => s.setTransparentBackground,
  );
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
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [shutterBurst, setShutterBurst] = useState(false);
  const [draggingAttachmentId, setDraggingAttachmentId] = useState<string | null>(
    null,
  );
  const { haptic } = useHaptic();
  const promptEnhancement = usePromptEnhancementCandidate({
    currentText: text,
    onApply: setText,
    haptic,
    scope: "desktop-composer",
  });
  const isEnhancing = promptEnhancement.isEnhancing;
  const promptTooLong = isPromptTooLong(text);
  const shouldShowCount = anyFlag(
    text.length > MAX_PROMPT_CHARS * 0.8,
    promptTooLong,
  );

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

  useEffect(() => {
    const node = rootRef.current;
    if (!node || !onMetricsChange) return;

    const measure = () => {
      const rect = node.getBoundingClientRect();
      onMetricsChange({
        height: Math.ceil(rect.height),
        bottom: Math.max(0, Math.ceil(window.innerHeight - rect.bottom)),
      });
    };
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(node);
    window.addEventListener("resize", measure);
    measure();

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [onMetricsChange]);

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
      el.style.removeProperty("height");
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    });
    return () => cancelAnimationFrame(raf);
  }, [text, expanded]);

  useEffect(() => {
    return () => {
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
    const parsed = parseDesktopComposerSlash(text);
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
    if (isSending || isEnhancing) return false;
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
    maskTargetAttachmentId: selectValue(
      inpaint.maskActive,
      firstAttachmentId(attachments),
      null,
    ),
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
    transparentBackground,
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
    // 发送反馈仅保留短暂光晕；指针缩放由 Pressable 自己处理。
    setShutterBurst(true);
    haptic("medium");
    if (shutterTimerRef.current) clearTimeout(shutterTimerRef.current);
    shutterTimerRef.current = setTimeout(() => {
      shutterTimerRef.current = null;
      setShutterBurst(false);
    }, 200);
    // 斜杠命令最终落地：剥离前缀
    const parsed = parseDesktopComposerSlash(currentText);
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
    if (isComposingRef.current || !canSubmit) return;
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void handleSubmit();
    }
  };

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
    <div
      ref={rootRef}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={(e) => void handleDrop(e)}
      className={desktopComposerFrameClass(expanded, isDragActive)}
      style={{
        left: "calc(50% + var(--studio-sidebar-offset, 0px) / 2)",
        width: desktopComposerFrameWidth(),
        zIndex: selectValue(
          expanded,
          "var(--z-composer-expanded, 45)" as unknown as number,
          "var(--z-composer, 40)" as unknown as number,
        ),
      }}
    >
      {/* 折叠态：核心操作保持在一行 */}
      {renderWhen(!expanded, (
        <div className="flex h-14 items-center gap-2 px-2.5">
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
              <Badge
                tone="accent"
                aria-hidden
                className={cn(
                  "absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1",
                  "justify-center border-0 type-overline leading-none text-[var(--accent-on)] tabular-nums",
                )}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {attachments.length}x
              </Badge>
            ))}
          </IconBtn>

          <ModeSegment value={mode} onChange={handleModeChange} />

          <Button
            variant="ghost"
            size="md"
            onClick={expandAndFocus}
            aria-label="展开输入框"
            aria-expanded={false}
            className={cn(
              "h-10 min-w-0 flex-1 justify-start px-3 text-left cursor-text",
              "bg-transparent transition-colors",
              "hover:bg-[var(--bg-2)]",
            )}
          >
            <span
              className={cn(
                "type-body-sm line-clamp-1",
                selectValue(
                  Boolean(text),
                  "text-[var(--fg-0)]",
                  "text-[var(--fg-2)]",
                ),
              )}
            >
              {fallbackText(text, "描述你想创作的内容…")}
            </span>
          </Button>

          <SendButton
            canSubmit={canSubmit}
            isSending={isSending}
            burst={shutterBurst}
            onClick={() => void handleSubmit()}
            size="md"
          />
        </div>
      ))}

      {/* 展开态 */}
      {renderWhen(expanded, (
        <div className="flex flex-col">
          {/* 附件托盘 */}
          <DesktopComposerAttachmentTray
            attachments={attachments}
            attachmentRoles={attachmentRoles}
            draggingAttachmentId={draggingAttachmentId}
            inpaint={inpaint}
            isDragActive={isDragActive}
            isImageMode={isImageMode}
            onAttachmentDragStart={handleAttachmentDragStart}
            onAttachmentDragOver={handleAttachmentDragOver}
            onAttachmentDrop={handleAttachmentDrop}
            onAttachmentDragEnd={handleAttachmentDragEnd}
            onInsertImageMention={insertImageMention}
            onRemoveAttachment={removeAttachment}
          />

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
                  <IconButton
                    size="sm"
                    aria-label="关闭错误提示"
                    onClick={() => setComposerError(null)}
                    className="shrink-0 hover:bg-[var(--bg-2)]"
                  >
                    <X className="h-3.5 w-3.5" aria-hidden />
                  </IconButton>
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
              readOnly={isEnhancing}
              rows={1}
              className={cn(
                "w-full bg-transparent outline-none resize-none",
                "type-body text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
                "min-h-11 max-h-[200px]",
                selectValue(isEnhancing, "cursor-wait", undefined),
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
              transparentBackground={transparentBackground}
              onTransparentBackgroundChange={setTransparentBackground}
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

            <span className="w-px h-5 bg-[var(--border-subtle)] mx-0.5 shrink-0" aria-hidden />

            <ModeSegment value={mode} onChange={handleModeChange} />

            {renderWhen(shouldShowCount, (
              <span
                data-inline
                className={cn(
                  "type-caption tabular-nums transition-colors duration-200",
                  selectValue(
                    promptTooLong,
                    "text-[var(--danger)]",
                    "text-accent",
                  ),
                )}
                style={{ fontFamily: "var(--font-mono)" }}
              >
                {text.length}/{MAX_PROMPT_CHARS}
              </span>
            ))}

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
              reasoningEffort={coalesceValue(reasoningEffort, "medium")}
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
              transparentBackground={transparentBackground}
              onTransparentBackgroundChange={setTransparentBackground}
              onClose={() => setAdvancedOpen(false)}
            />
          </DesktopPopover>
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
