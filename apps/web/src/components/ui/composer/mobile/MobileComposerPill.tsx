"use client";

// 移动 Composer：56px 核心输入层 + 生图快捷参数 + BottomSheet 低频设置。

import {
  type ChangeEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Loader2, Paperclip } from "lucide-react";
import { Badge } from "@/components/ui/primitives";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
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
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { MAX_COMPOSER_ATTACHMENTS } from "../shared/attachments";
import { useComposerAttachmentDnd } from "../shared/useComposerAttachmentDnd";
import { useMaskInpaint } from "../shared/useMaskInpaint";
import { useComposerAttachmentRoles } from "../shared/attachmentRoles";
import { buildComposerExecutionSummary } from "../shared/executionSummary";
import {
  firstAttachmentId,
  renderWhen,
  selectValue,
} from "../shared/composerViewState";
import { useComposerCostEstimate } from "../shared/useComposerCostEstimate";
import { usePromptEnhancementCandidate } from "../shared/PromptEnhancementCandidate";
import {
  canSubmitMobileComposer,
  deriveMobileComposerLayout,
  shouldShowPromptCount,
} from "./mobileComposerViewState";
import {
  MobileComposerIconButton,
  MobileComposerSendButton,
} from "./MobileComposerButtons";
import { MobileComposerExpanded } from "./MobileComposerExpanded";
import {
  MobileComposerOverlays,
  type MobileComposerPanel,
} from "./MobileComposerOverlays";
import { useMobileAttachmentReorder } from "./useMobileAttachmentReorder";

interface MobileComposerPillProps {
  onSubmit: () => void | Promise<void>;
  onMetricsChange?: (metrics: { height: number; bottom: number }) => void;
}

type ComposerPanel = MobileComposerPanel;

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
  const [attachmentMenuId, setAttachmentMenuId] = useState<string | null>(null);
  const { haptic } = useHaptic();
  const {
    beginAttachmentReorder,
    handleAttachmentClickCapture,
    draggingAttachmentId,
    reorderTargetAttachmentId,
  } = useMobileAttachmentReorder({
    attachmentCount: attachments.length,
    moveAttachment,
    haptic,
  });
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
  const attachmentMenuRole =
    attachmentMenuIndex >= 0 && attachmentMenuId
      ? attachmentRoles.getRole(attachmentMenuId)
      : null;
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
          "bg-[var(--bg-1)]",
          "border transition-[border-color,box-shadow] duration-[var(--dur-normal)]",
          selectValue(
            isDragActive,
            "border-accent-border",
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
            <MobileComposerIconButton
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
                    "absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] px-1",
                    "justify-center border-0 type-overline leading-none text-[var(--accent-on)] tabular-nums",
                  )}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {attachments.length}
                </Badge>
              ))}
            </MobileComposerIconButton>

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
                  "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                  "type-overline leading-none text-[var(--fg-1)]",
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
                  "type-body outline-none placeholder:text-[var(--fg-2)]",
                  selectValue(
                    Boolean(text),
                    "text-[var(--fg-0)]",
                    "text-[var(--fg-2)]",
                  ),
                )}
              />
            </div>

            <MobileComposerSendButton
              canSubmit={canSubmit}
              isSending={isSending}
              burst={shutterBurst}
              onClick={() => void handleSubmit()}
            />
          </div>
        ))}

        {renderWhen(expanded, (
          <MobileComposerExpanded
            textareaRef={textareaRef}
            text={text}
            mode={mode}
            attachments={attachments}
            isUploading={isUploading}
            isDragActive={isDragActive}
            isEnhancing={isEnhancing}
            isSending={isSending}
            shutterBurst={shutterBurst}
            canSubmit={canSubmit}
            shouldShowCount={shouldShowCount}
            promptTooLong={promptTooLong}
            composerError={composerError}
            expandedPaddingBottom={expandedPaddingBottom}
            draggingAttachmentId={draggingAttachmentId}
            reorderTargetAttachmentId={reorderTargetAttachmentId}
            attachmentRoles={attachmentRoles}
            inpaint={inpaint}
            promptEnhancement={promptEnhancement}
            executionSummary={executionSummary}
            onCollapse={() => setExpanded(false)}
            onTextChange={handleTextChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onCompositionStart={() => {
              isComposingRef.current = true;
            }}
            onCompositionEnd={() => {
              isComposingRef.current = false;
            }}
            onOpenFilePicker={openFilePicker}
            onOpenAttachmentMenu={setAttachmentMenuId}
            onBeginAttachmentReorder={beginAttachmentReorder}
            onAttachmentClickCapture={handleAttachmentClickCapture}
            onClearComposerError={() => setComposerError(null)}
            onOpenAdvanced={openAdvancedSheet}
            onModeChange={setMode}
            onSubmit={() => void handleSubmit()}
          />
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

      <MobileComposerOverlays
        panel={panel}
        attachmentMenuIndex={attachmentMenuIndex}
        attachmentMenuId={attachmentMenuId}
        attachmentMenuRole={attachmentMenuRole}
        mode={mode}
        quality={quality}
        renderQuality={renderQuality}
        aspect={aspect}
        count={count}
        reasoningEffort={reasoningEffort}
        webSearch={webSearch}
        fileSearch={fileSearch}
        codeInterpreter={codeInterpreter}
        imageGeneration={imageGeneration}
        fast={fast}
        inpaint={inpaint}
        onCloseAttachmentMenu={() => setAttachmentMenuId(null)}
        onInsertMention={insertImageMention}
        onCycleRole={attachmentRoles.cycleRole}
        onRemoveAttachment={removeAttachment}
        onClosePanel={closePanel}
        onQualityChange={setQuality}
        onRenderQualityChange={setRenderQuality}
        onAspectChange={setAspectRatio}
        onOpenAspect={openAspectSheet}
        onCountChange={setImageCount}
        onOpenReasoning={openReasoningSheet}
        onReasoningEffortChange={(value) => {
          setReasoningEffort(value);
          setPanel("none");
        }}
        onWebSearchChange={setWebSearch}
        onFileSearchChange={setFileSearch}
        onCodeInterpreterChange={setCodeInterpreter}
        onImageGenerationChange={setImageGeneration}
        onFastChange={setFast}
      />
    </>
  );
}
