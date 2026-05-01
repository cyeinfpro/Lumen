"use client";

// DESIGN §12.1 / §22.3：Composer 单一来源 = useChatStore.composer。
// - ⌘/Ctrl+Enter 发送，Enter 换行
// - 粘贴图 / 拖拽图 → 参考图
// - 顶部色条：image 模式 amber，其余中性
// - 发送动作由父组件 onSubmit 触发

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Globe2, Paperclip, Send, Sparkles, Loader2, Undo2, X } from "lucide-react";
import { useChatStore } from "@/store/useChatStore";
import { enhancePrompt } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { logError } from "@/lib/logger";
import { AttachmentTray } from "./AttachmentTray";
import { AspectRatioPicker } from "./AspectRatioPicker";
import { ModeSwitcher } from "./ModeSwitcher";
import { ReasoningEffortPicker } from "./ReasoningEffortPicker";
import { ImageCountPicker } from "./ImageCountPicker";
import { QualityPicker, RenderQualityPicker } from "./QualityPicker";

interface PromptComposerProps {
  onSubmit: () => void | Promise<void>;
}

// 斜杠命令：/ask → chat / vision_qa；/image → text_to_image / image_to_image
// 精确匹配 /ask 或 /image 后接空白或字符串结束，避免误判 /asky /imageless。
function parseSlashCommand(text: string): {
  stripped: string;
  force?: "chat" | "image";
} {
  const m = /^\s*\/(ask|image)(\s+|$)/i.exec(text);
  if (!m) return { stripped: text };
  const cmd = m[1].toLowerCase();
  const stripped = text.slice(m[0].length).trim();
  return {
    stripped,
    force: cmd === "ask" ? "chat" : "image",
  };
}

const LONG_TEXT_THRESHOLD = 200;

export function PromptComposer({ onSubmit }: PromptComposerProps) {
  const text = useChatStore((s) => s.composer.text);
  const setText = useChatStore((s) => s.setText);
  const setForceIntent = useChatStore((s) => s.setForceIntent);
  const mode = useChatStore((s) => s.composer.mode);
  const attachments = useChatStore((s) => s.composer.attachments);
  const addAttachment = useChatStore((s) => s.addAttachment);
  const uploadAttachment = useChatStore((s) => s.uploadAttachment);
  const composerError = useChatStore((s) => s.composerError);
  const setComposerError = useChatStore((s) => s.setComposerError);

  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [keyboardOffset, setKeyboardOffset] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // 粘贴图 toast：本地最简实现，避免依赖 Agent A 的 Toast 原语
  const [toast, setToast] = useState<string | null>(null);
  // AI 增强提示词
  const [isEnhancing, setIsEnhancing] = useState(false);
  const [originalText, setOriginalText] = useState<string | null>(null);
  const enhanceAbortRef = useRef<AbortController | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  // 中文 IME 输入期间忽略 Enter 类快捷键，避免选词时被发送/换行误触发
  const isComposingRef = useRef(false);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // HIGH #1：re-entry guard —— 比 setTimeout 更稳；submit Promise 完成后再放开
  const submittingRef = useRef(false);

  // HIGH #2：虚拟键盘弹出时调整 composer 位置（iOS Safari / Android Chrome）
  // 用 visualViewport.height + offsetTop 精确计算遮挡量；阈值放宽到 60px 以处理部分 Android 浅键盘
  useEffect(() => {
    if (typeof window === "undefined") return;
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => {
      const occluded = Math.max(
        0,
        window.innerHeight - vv.height - vv.offsetTop,
      );
      const next = occluded > 60 ? occluded : 0;
      setKeyboardOffset(next);
      if (next > 0) {
        window.requestAnimationFrame(() => {
          textareaRef.current?.scrollIntoView({
            block: "center",
            inline: "nearest",
          });
        });
      }
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);

  // textarea 自动高度：基于 scrollHeight；上限通过 maxRows 换算
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    // 最大高度 ~ 6 行（按 24px line-height 估算 + padding）
    const max = window.innerWidth >= 768 ? 280 : 168;
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
  }, [text]);

  // toast 自动消失
  // P1-2：cleanup 必须 clearTimeout 旧 timer，避免高频触发（如批量粘贴）造成 timer 堆积/竞争
  useEffect(() => {
    if (!toast) return;
    if (toastTimerRef.current) {
      clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    toastTimerRef.current = setTimeout(() => {
      toastTimerRef.current = null;
      setToast(null);
    }, 1800);
    return () => {
      if (toastTimerRef.current) {
        clearTimeout(toastTimerRef.current);
        toastTimerRef.current = null;
      }
    };
  }, [toast]);

  // React 19 规则：render 阶段不能访问 ref。re-entry guard 的拦截发生在事件 handler 内。
  const canSubmit =
    !isSending && (text.trim().length > 0 || attachments.length > 0);
  const disabledTitle = !canSubmit
    ? isSending
      ? "发送中…"
      : "请先输入文字或添加参考图"
    : undefined;

  const ingestFile = async (file: File): Promise<boolean> => {
    if (!file.type.startsWith("image/")) return false;
    try {
      setUploadError(null);
      setIsUploading(true);
      const att = await uploadAttachment(file);
      addAttachment(att);
      return true;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "上传失败";
      setUploadError(msg);
      setComposerError(msg);
      logError(err, { scope: "composer", code: "upload_attachment_failed" });
      return false;
    } finally {
      setIsUploading(false);
    }
  };

  const ingestMany = async (files: File[]): Promise<number> => {
    let ok = 0;
    for (const f of files) {
      if (await ingestFile(f)) ok += 1;
    }
    return ok;
  };

  const handleSubmit = async () => {
    // HIGH #1：re-entry guard —— 慢网下固定 160ms 解锁会漏挡，这里以 submit promise 完成为准
    if (submittingRef.current) return;
    if (!canSubmit) return;
    submittingRef.current = true;
    const parsed = parseSlashCommand(text);
    if (parsed.force) {
      setForceIntent(parsed.force);
      setText(parsed.stripped);
    } else {
      setForceIntent(undefined);
    }
    setIsSending(true);
    try {
      // onSubmit 返回 Promise 时等其完成；page.tsx 已传入会触发 sendMessage 的 handler
      const maybe = onSubmit();
      if (maybe && typeof (maybe as Promise<void>).then === "function") {
        await maybe;
      }
    } catch (err) {
      // 保底：async handler 不应让 loading 卡死
      logError(err, { scope: "composer", code: "submit_failed" });
    } finally {
      submittingRef.current = false;
      setIsSending(false);
    }
  };

  const handleEnhance = async () => {
    const current = text.trim();
    if (!current || isEnhancing) return;
    setOriginalText(current);
    setIsEnhancing(true);
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
    } catch (err) {
      if (ctl.signal.aborted) return;
      logError(err, { scope: "composer", code: "enhance_failed" });
      setText(current);
      setOriginalText(null);
      setToast("提示词优化失败");
    } finally {
      setIsEnhancing(false);
      enhanceAbortRef.current = null;
    }
  };

  const handleUndoEnhance = () => {
    if (originalText !== null) {
      setText(originalText);
      setOriginalText(null);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposingRef.current) return;
    // ⌘/Ctrl+Enter 发送；单独 Enter 换行
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      // HIGH #10：快捷键也要检查 canSubmit，避免绕过按钮 disabled；
      // submittingRef 的 re-entry 检查在 handleSubmit 开头完成
      if (!canSubmit) return;
      void handleSubmit();
    }
  };

  const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
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
      const ok = await ingestMany(files);
      if (ok > 0) setToast(`已添加 ${ok} 张参考图`);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    if (Array.from(e.dataTransfer.types).includes("Files")) {
      e.preventDefault();
      setIsDragging(true);
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    // 仅当离开 composer 容器本身（而不是跨越子元素）才清除高亮
    const related = e.relatedTarget as Node | null;
    if (related && (e.currentTarget as Node).contains(related)) return;
    setIsDragging(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const files = Array.from(e.dataTransfer.files ?? []);
    const ok = await ingestMany(files);
    if (ok > 0) setToast(`已添加 ${ok} 张参考图`);
  };

  const handleFileInputChange = async (
    e: React.ChangeEvent<HTMLInputElement>,
  ) => {
    const files = Array.from(e.target.files ?? []);
    const ok = await ingestMany(files);
    if (ok > 0) setToast(`已添加 ${ok} 张参考图`);
    // 允许相同文件再次触发 change
    e.target.value = "";
  };

  const barActive = mode === "image";
  const charCount = text.length;
  const showCount = charCount > LONG_TEXT_THRESHOLD;

  return (
    <>
      {/* 粘贴 / 拖拽 toast（本地最简实现） */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.96 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            role="status"
            className={cn(
              "fixed left-[calc(50%+var(--sidebar-w)/2)] -translate-x-1/2 z-[60]",
              "bottom-[calc(var(--composer-bottom,9rem)+env(safe-area-inset-bottom))]",
              "px-3.5 py-1.5 rounded-full text-xs font-medium",
              "bg-neutral-900/95 border border-white/15 text-neutral-100",
              "shadow-lg shadow-black/40 backdrop-blur-md",
            )}
          >
            {toast}
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div
        ref={composerRef}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        initial={{ y: 40, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ type: "spring", damping: 26, stiffness: 240 }}
        style={
          {
            "--keyboard-offset": `${keyboardOffset}px`,
          } as React.CSSProperties
        }
        className={cn(
          // 跟随主内容区居中：main 区域 = 视口去掉 Sidebar 宽度（--sidebar-w，桌面端 18rem，移动端 0）
          "fixed left-[calc(50%+var(--sidebar-w)/2)] -translate-x-1/2 z-50",
          "w-[min(calc(100vw-var(--sidebar-w)-1rem),48rem)] lg:w-[min(calc(100vw-var(--sidebar-w)-2rem),56rem)]",
          "bottom-[calc(0.75rem+env(safe-area-inset-bottom)+var(--keyboard-offset,0px))] sm:bottom-[calc(1.5rem+env(safe-area-inset-bottom)+var(--keyboard-offset,0px))]",
          "rounded-[1.7rem] shadow-[0_24px_80px_-26px_rgba(0,0,0,0.75),0_0_0_1px_rgba(255,255,255,0.04)]",
          "border transition-[border-color,background-color,box-shadow] duration-300",
          // focus 不再改描边/背景 —— 避免"点进去冒出一个亮框"。
          // amber 只保留给拖拽态（用户正在往里拖图）。
          isDragging ? "border-[var(--color-lumen-amber)]/70" : "border-white/10",
          "bg-neutral-950/70 backdrop-blur-2xl supports-[backdrop-filter]:bg-neutral-950/62",
          // 注意：不要加 overflow-hidden —— 会裁掉 AspectRatioPicker / ModeSwitcher 的 popover
        )}
      >
        {/* 顶部模式色条：仅在 image 模式出现，让 chat 态把竖向空间让给输入文本 */}
        <AnimatePresence initial={false}>
          {barActive && (
            <motion.div
              key="mode-bar"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 20 }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
              className="px-4 flex items-center overflow-hidden"
            >
              <div className="h-[3px] w-full rounded-full bg-[var(--color-lumen-amber)] opacity-90" />
            </motion.div>
          )}
        </AnimatePresence>

        {/* 错误提示 */}
        <AnimatePresence>
          {uploadError && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <div className="mx-4 mb-1 flex items-center gap-2 px-2.5 py-1.5 text-[11px] rounded-lg bg-red-500/15 border border-red-500/30 text-red-200">
                <span className="flex-1">附件上传失败：{uploadError}</span>
                <button
                  type="button"
                  aria-label="关闭提示"
                  onClick={() => setUploadError(null)}
                  className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-md hover:bg-red-500/30 transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            </motion.div>
          )}
          {composerError && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <div className="mx-4 mb-1 flex items-start gap-2 px-2.5 py-1.5 text-[11px] rounded-lg bg-red-500/15 border border-red-500/30 text-red-200">
                <span className="flex-1 break-words">{composerError}</span>
                <button
                  type="button"
                  aria-label="关闭错误提示"
                  title="关闭"
                  onClick={() => setComposerError(null)}
                  className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-md text-red-300 hover:text-white hover:bg-red-500/30 transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* 附件托盘（仅在有附件时渲染） */}
        {attachments.length > 0 && (
          <div className="px-4 pb-2">
            <AttachmentTray />
          </div>
        )}

        {/* AI 已优化提示 */}
        <AnimatePresence>
          {originalText !== null && !isEnhancing && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <div className="mx-4 mt-2 flex items-center gap-2 px-2.5 py-1 text-[11px] rounded-lg bg-[var(--color-lumen-amber)]/10 border border-[var(--color-lumen-amber)]/25 text-[var(--color-lumen-amber)]">
                <Sparkles className="w-3 h-3 shrink-0" />
                <span className="flex-1">AI 已优化提示词</span>
                <button
                  type="button"
                  onClick={handleUndoEnhance}
                  className="inline-flex items-center gap-1 text-[11px] underline decoration-dotted hover:text-white transition-colors"
                >
                  <Undo2 className="w-3 h-3" />
                  撤销
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* 文本输入区：放大 min-height，让用户感觉"有足够的地方写东西" */}
        <div className="px-4 pt-3 md:pt-4">
          <div className="relative">
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
              placeholder="描述你想生成的画面，或和 Lumen 对话…"
              aria-label="输入提示词"
              className={cn(
                "w-full bg-transparent resize-none outline-none",
                "text-[15px] leading-6 text-neutral-50 placeholder:text-neutral-500",
                "py-1 min-h-[56px]",
              )}
              rows={2}
            />
            <AnimatePresence>
              {showCount && (
                <motion.span
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 4 }}
                  className="absolute right-0 bottom-0 text-[10px] font-mono tabular-nums text-neutral-500 pointer-events-none"
                  aria-live="polite"
                >
                  {charCount}
                </motion.span>
              )}
            </AnimatePresence>
          </div>
        </div>

        {/* 底部工具条（MED #8：小屏允许换行，同时保底横向滚动） */}
        <div className="scrollbar-thin flex flex-wrap items-center gap-2 md:gap-3 overflow-x-auto overscroll-x-contain px-3 pb-2.5 pt-2">
          <IconButton
            label="添加参考图"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
          >
            {isUploading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Paperclip className="w-4 h-4" />
            )}
          </IconButton>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={handleFileInputChange}
          />

          <IconButton
            label={isEnhancing ? "正在优化…" : "AI 优化提示词"}
            onClick={() => void handleEnhance()}
            disabled={isEnhancing || !text.trim()}
          >
            {isEnhancing ? (
              <Loader2 className="w-4 h-4 animate-spin text-[var(--color-lumen-amber)]" />
            ) : (
              <Sparkles className="w-4 h-4" />
            )}
          </IconButton>

          {/* 分隔竖线，提升工具条层级感 */}
          <div className="w-px h-5 bg-white/10 mx-0.5" aria-hidden />

          <ModeSwitcher />
          {mode !== "chat" && <AspectRatioPicker />}
          {mode !== "chat" && <QualityPicker />}
          {mode !== "chat" && <RenderQualityPicker />}
          {mode !== "chat" && <ImageCountPicker />}
          {mode !== "image" && <ReasoningEffortPicker />}
          {mode !== "image" && <WebSearchToggle />}
          <FastToggle />

          {/* 右侧发送区（弹性填充） */}
          <div className="min-w-3 flex-1" />

          <SendButton
            canSubmit={canSubmit}
            isSending={isSending}
            onClick={() => void handleSubmit()}
            title={disabledTitle}
          />
        </div>

        {/* 拖拽高亮层 */}
        <AnimatePresence>
          {isDragging && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.14 }}
              className={cn(
                "pointer-events-none absolute inset-0 rounded-3xl",
                "border-2 border-dashed border-[var(--color-lumen-amber)]",
                "bg-[var(--color-lumen-amber)]/8",
                "flex items-center justify-center",
              )}
            >
              <motion.span
                initial={{ scale: 0.94 }}
                animate={{ scale: [0.98, 1.02, 0.98] }}
                transition={{
                  duration: 1.6,
                  repeat: Infinity,
                  ease: "easeInOut",
                }}
                className="text-sm font-medium text-[var(--color-lumen-amber)]"
              >
                松开手以附加图片
              </motion.span>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </>
  );
}

// ——————————————————— 子原语 ———————————————————

function IconButton({
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
    <motion.button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-disabled={disabled}
      aria-label={label}
      title={label}
      whileHover={disabled ? undefined : { scale: 1.05 }}
      whileTap={disabled ? undefined : { scale: 0.92 }}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
      className={cn(
        "inline-flex items-center justify-center w-9 h-9 rounded-full",
        "text-neutral-300 hover:text-white hover:bg-white/8",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
        "aria-disabled:pointer-events-none disabled:opacity-40 disabled:cursor-not-allowed",
      )}
    >
      {children}
    </motion.button>
  );
}

function FastToggle() {
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);
  return (
    <motion.button
      type="button"
      onClick={() => setFast(!fast)}
      aria-label={fast ? "Fast 模式已开" : "Fast 模式已关"}
      title={fast ? "Fast 模式：更快响应" : "Fast 模式：已关闭"}
      whileHover={{ scale: 1.03 }}
      whileTap={{ scale: 0.94 }}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
      className={cn(
        "inline-flex items-center gap-1 px-2.5 h-7 rounded-full",
        "text-xs font-medium border",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
        fast
          ? "bg-emerald-500/12 border-emerald-500/40 text-emerald-400"
          : "bg-white/5 border-white/10 text-neutral-300 hover:bg-white/10 hover:text-white",
      )}
    >
      <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
      </svg>
      <span>Fast</span>
    </motion.button>
  );
}

function WebSearchToggle() {
  const webSearch = useChatStore((s) => s.composer.webSearch);
  const setWebSearch = useChatStore((s) => s.setWebSearch);
  return (
    <motion.button
      type="button"
      onClick={() => setWebSearch(!webSearch)}
      aria-label={webSearch ? "网络搜索已开" : "网络搜索已关"}
      title={webSearch ? "网络搜索：模型会按需搜索" : "网络搜索：已关闭"}
      whileHover={{ scale: 1.03 }}
      whileTap={{ scale: 0.94 }}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
      className={cn(
        "inline-flex items-center gap-1 px-2.5 h-7 rounded-full",
        "text-xs font-medium border",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
        webSearch
          ? "bg-sky-500/12 border-sky-500/40 text-sky-300"
          : "bg-white/5 border-white/10 text-neutral-300 hover:bg-white/10 hover:text-white",
      )}
    >
      <Globe2 className="w-3.5 h-3.5" aria-hidden />
      <span>搜索</span>
    </motion.button>
  );
}

function SendButton({
  canSubmit,
  isSending,
  onClick,
  title,
}: {
  canSubmit: boolean;
  isSending: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <motion.button
      type="button"
      onClick={onClick}
      disabled={!canSubmit}
      aria-disabled={!canSubmit}
      aria-label="发送"
      title={title ?? "发送 (⌘↵)"}
      whileHover={canSubmit ? { scale: 1.02 } : undefined}
      whileTap={canSubmit ? { scale: 0.94 } : undefined}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
      className={cn(
        "inline-flex items-center gap-1.5 pl-3 pr-2.5 h-9 rounded-full",
        "text-sm font-medium",
        "aria-disabled:pointer-events-none",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70 focus-visible:ring-offset-0",
        canSubmit
          ? [
              "bg-[var(--color-lumen-amber)] text-black",
              "hover:brightness-110",
              "shadow-[0_0_18px_rgba(242,169,58,0.35)] hover:shadow-[0_0_22px_rgba(242,169,58,0.5)]",
              "cursor-pointer",
            ].join(" ")
          : "bg-white/6 text-neutral-500 cursor-not-allowed shadow-none",
      )}
    >
      {isSending ? (
        <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
      ) : (
        <Send className="w-4 h-4" aria-hidden />
      )}
      <span className="hidden sm:inline">发送</span>
      <kbd
        className={cn(
          "hidden sm:inline-flex items-center h-5 px-1 rounded font-mono text-[10px] border",
          canSubmit
            ? "bg-black/20 border-black/20 text-black/70"
            : "bg-white/5 border-white/10 text-neutral-500",
        )}
        aria-hidden
      >
        ⌘↵
      </kbd>
    </motion.button>
  );
}
