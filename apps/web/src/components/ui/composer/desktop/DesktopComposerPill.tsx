"use client";

// Darkroom 桌面端 Composer Pill（≥768px）：
// - fixed bottom-6 中间居中，max-w 920，折叠 60 / 展开 ≤320
// - 宽高比 & 推理强度用 Popover（不是 BottomSheet）
// - 键盘快捷键：⌘↵ 发送、/ 展开、⌘K 切换 Sidebar
// - 蓝本：../mobile/MobileComposerPill.tsx

import { AnimatePresence, motion } from "framer-motion";
import {
  type ChangeEvent,
  type ClipboardEvent,
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
  Sparkles,
  Undo2,
  X,
  Zap,
} from "lucide-react";
import {
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
import { useHaptic } from "@/hooks/useHaptic";
import { DURATION, EASE, SPRING } from "@/lib/motion";
import {
  DesktopPopover,
  PopoverList,
} from "./DesktopPopover";

interface DesktopComposerPillProps {
  onSubmit: () => void | Promise<void>;
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

export function DesktopComposerPill({ onSubmit }: DesktopComposerPillProps) {
  const text = useChatStore((s) => s.composer.text);
  const setText = useChatStore((s) => s.setText);
  const setForceIntent = useChatStore((s) => s.setForceIntent);
  const mode = useChatStore((s) => s.composer.mode);
  const setMode = useChatStore((s) => s.setMode);
  const attachments = useChatStore((s) => s.composer.attachments);
  const addAttachment = useChatStore((s) => s.addAttachment);
  const removeAttachment = useChatStore((s) => s.removeAttachment);
  const uploadAttachment = useChatStore((s) => s.uploadAttachment);
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
  const [isEnhancing, setIsEnhancing] = useState(false);
  const [originalText, setOriginalText] = useState<string | null>(null);
  const enhanceAbortRef = useRef<AbortController | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [aspectPopoverOpen, setAspectPopoverOpen] = useState(false);
  const [reasoningPopoverOpen, setReasoningPopoverOpen] = useState(false);
  const [shutterBurst, setShutterBurst] = useState(false);
  const { haptic } = useHaptic();
  const promptTooLong = isPromptTooLong(text);
  const shouldShowCount = text.length > MAX_PROMPT_CHARS * 0.8 || promptTooLong;

  const rootRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const aspectTriggerRef = useRef<HTMLDivElement | null>(null);
  const reasoningTriggerRef = useRef<HTMLDivElement | null>(null);
  const isComposingRef = useRef(false);
  const submittingRef = useRef(false);
  const didMountRef = useRef(false);
  const shutterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
      setAspectPopoverOpen(false);
      setReasoningPopoverOpen(false);
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

  // ———— 全局键盘快捷键：⌘K 切换 Sidebar、/ 展开 Composer ————
  useEffect(() => {
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      // 跳过 IME 组合中
      if (e.isComposing) return;

      // ⌘K / Ctrl+K → 派发 sidebar-toggle
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("lumen:sidebar-toggle"));
        return;
      }

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

  const ingestFile = useCallback(
    async (file: File): Promise<boolean> => {
      if (!file.type.startsWith("image/")) return false;
      try {
        setIsUploading(true);
        const att = await uploadAttachment(file);
        addAttachment(att);
        return true;
      } catch (err) {
        const msg = err instanceof Error ? err.message : "上传失败";
        setComposerError(msg);
        pushMobileToast(msg, "danger");
        return false;
      } finally {
        setIsUploading(false);
      }
    },
    [uploadAttachment, addAttachment, setComposerError],
  );

  const ingestMany = useCallback(
    async (files: File[]) => {
      let ok = 0;
      for (const f of files) {
        if (await ingestFile(f)) ok += 1;
      }
      if (ok > 0) pushMobileToast(`已添加 ${ok} 张参考图`, "success");
    },
    [ingestFile],
  );

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

  const handlePaste = async (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f && f.type.startsWith("image/")) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      await ingestMany(files);
    }
  };

  const handleFileInput = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    await ingestMany(files);
    e.target.value = "";
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

  const expandAndFocus = () => {
    setExpanded(true);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const isImageMode = mode === "image";

  return (
    <motion.div
      ref={rootRef}
      initial={false}
      animate={{ height: expanded ? "auto" : 48 }}
      transition={SPRING.sheet}
      className={cn(
        "fixed bottom-4 left-1/2 -translate-x-1/2",
        "w-[calc(100%-40px)] max-w-[860px]",
        "overflow-visible",
        "rounded-xl backdrop-blur-xl",
        "bg-[var(--bg-1)]/88 supports-[not(backdrop-filter:blur(1px))]:bg-[var(--bg-1)]/95",
        "border transition-[border-color,box-shadow] duration-200",
        isImageMode
          ? "border-[var(--border-amber)]"
          : "border-[var(--border-subtle)]",
        "shadow-[var(--shadow-2)]",
      )}
      style={{
        zIndex: expanded
          ? ("var(--z-composer-expanded, 45)" as unknown as number)
          : ("var(--z-composer, 40)" as unknown as number),
      }}
    >
      {/* 折叠态：单行 60px */}
      {!expanded && (
        <div className="flex items-center h-[48px] px-3 gap-2">
          <IconBtn
            label="添加参考图"
            onClick={() => fileInputRef.current?.click()}
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

          <button
            type="button"
            onClick={expandAndFocus}
            aria-label="展开输入框"
            aria-expanded={false}
            className={cn(
              "flex-1 min-w-0 h-8 px-3 text-left rounded-lg cursor-text",
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
              {text || "给 Lumen 一句话… (按 / 展开)"}
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
          {attachments.length > 0 && (
            <div
              className={cn(
                "flex gap-2 overflow-x-auto overscroll-x-contain no-scrollbar",
                "px-3 pt-3",
              )}
            >
              {attachments.map((att) => (
                <div
                  key={att.id}
                  className={cn(
                    "relative shrink-0 w-16 h-16 rounded-xl overflow-hidden",
                    "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                  )}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={att.data_url}
                    alt=""
                    className="w-full h-full object-cover"
                    loading="lazy"
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
              ))}
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
                    "mx-3 mt-2 flex items-start gap-2 px-2.5 py-1.5 rounded-lg",
                    "bg-[rgba(229,72,77,0.12)] border border-[rgba(229,72,77,0.4)]",
                    "text-xs text-[var(--danger)]",
                  )}
                >
                  <span className="flex-1 break-words">{composerError}</span>
                  <button
                    type="button"
                    aria-label="关闭错误提示"
                    onClick={() => setComposerError(null)}
                    className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-md hover:bg-black/30"
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
                    "mx-3 mt-2 flex items-center gap-2 px-2.5 py-1 rounded-lg",
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

          {/* 工具条 */}
          <div
            className={cn(
              "flex items-center gap-1.5 overflow-x-auto overflow-y-visible overscroll-x-contain no-scrollbar",
              "px-3 pb-3 pt-1.5",
            )}
          >
            <IconBtn
              label="添加参考图"
              onClick={() => fileInputRef.current?.click()}
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

            <ModeSegment value={mode} onChange={(v) => setMode(v)} />

            {/* 尺寸选择（image mode） */}
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
                      onClick={() => setQuality(o.value)}
                      className={cn(
                        "inline-flex items-center justify-center h-7 min-w-8 px-2 rounded-full",
                        "text-[11px] tabular-nums transition-colors",
                        active
                          ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
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

            {/* 渲染质量（image mode） */}
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
                      onClick={() => setRenderQuality(o.value)}
                      className={cn(
                        "inline-flex items-center justify-center h-7 min-w-7 px-2 rounded-full",
                        "text-[11px] tabular-nums transition-colors",
                        active
                          ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                      )}
                      aria-pressed={active}
                    >
                      {o.label}
                    </button>
                  );
                })}
              </div>
            )}

            {/* 宽高比 Popover trigger（image mode） */}
            {isImageMode && (
              <div ref={aspectTriggerRef} className="relative shrink-0">
                <button
                  type="button"
                  onClick={() => setAspectPopoverOpen((v) => !v)}
                  aria-haspopup="dialog"
                  aria-expanded={aspectPopoverOpen}
                  aria-label={`宽高比 ${aspect}`}
                  className={cn(
                    "inline-flex items-center gap-1 h-8 px-2.5 rounded-full",
                    "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                    "text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-3)]",
                    "whitespace-nowrap transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                  )}
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {aspect}
                  <ChevronDown className="w-3 h-3" aria-hidden />
                </button>
                <DesktopPopover
                  open={aspectPopoverOpen}
                  onClose={() => setAspectPopoverOpen(false)}
                  anchorRef={aspectTriggerRef}
                  ariaLabel="选择宽高比"
                  align="left"
                >
                  <PopoverList
                    title="宽高比"
                    items={ASPECT_OPTIONS.map((o) => ({
                      key: o.value,
                      label: o.label,
                      hint: o.hint,
                      selected: o.value === aspect,
                      onSelect: () => {
                        setAspectRatio(o.value);
                        setAspectPopoverOpen(false);
                      },
                    }))}
                  />
                </DesktopPopover>
              </div>
            )}

            {/* 图像数量 segmented（image mode） */}
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
                      onClick={() => setImageCount(n)}
                      className={cn(
                        "inline-flex items-center justify-center h-7 min-w-8 px-2 rounded-full",
                        "text-[11px] tabular-nums transition-colors",
                        active
                          ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
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

            {/* 推理强度 Popover trigger（chat mode） */}
            {!isImageMode && (
              <div ref={reasoningTriggerRef} className="relative shrink-0">
                <button
                  type="button"
                  onClick={() => setReasoningPopoverOpen((v) => !v)}
                  aria-haspopup="dialog"
                  aria-expanded={reasoningPopoverOpen}
                  aria-label="推理强度"
                  className={cn(
                    "inline-flex items-center gap-1 h-8 px-2.5 rounded-full",
                    "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                    "text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-3)]",
                    "whitespace-nowrap transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                  )}
                >
                  {REASONING_OPTIONS.find((r) => r.value === reasoningEffort)?.label ??
                    "默认"}
                  <ChevronDown className="w-3 h-3" aria-hidden />
                </button>
                <DesktopPopover
                  open={reasoningPopoverOpen}
                  onClose={() => setReasoningPopoverOpen(false)}
                  anchorRef={reasoningTriggerRef}
                  ariaLabel="选择推理强度"
                  align="left"
                >
                  <PopoverList
                    title="推理强度"
                    items={REASONING_OPTIONS.map((o) => ({
                      key: o.value,
                      label: o.label,
                      hint: o.hint,
                      selected: o.value === reasoningEffort,
                      onSelect: () => {
                        setReasoningEffort(o.value);
                        setReasoningPopoverOpen(false);
                      },
                    }))}
                  />
                </DesktopPopover>
              </div>
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
        </div>
      )}

      {/* 隐藏文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        onChange={handleFileInput}
      />
    </motion.div>
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
                ? "shadow-[0_0_0_1px_rgba(242,169,58,0.8),0_0_36px_8px_var(--amber-glow-strong)]"
                : "shadow-[var(--shadow-shutter)]",
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
