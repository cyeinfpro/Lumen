"use client";

// 全局 InpaintModal —— 浏览态（Lightbox / 卡片 / 对话气泡 / Gallery）
// 点击"局部修改"会打开本组件。内嵌 MaskBoard + prompt 输入 + 提交按钮，
// 全流程不依赖 Composer state。
//
// 加固/优化：
//   - AnimatePresence 包到外层：open=false 时能播 exit 动画
//   - focus trap：Tab 在 modal 内循环；初次 focus prompt
//   - main inert：屏蔽辅助技术 / 鼠标穿透
//   - 双次确认关闭：有 stroke 或 prompt 时，第一次点取消变红色"确认放弃"，2.5s 内再点真关
//   - 错误自动消失：warning 5s 淡出
//   - 草稿持久化：prompt 输入会存到 useInpaintStore.drafts[imageId]，二次打开同图自动回填
//   - iOS svh + safe-area：keyboard 弹起时画板自适应
//   - 移动端按钮 h-11、prompt 输入区 sticky 底部、画板可滚动
//
// 提交：
//   board.exportMask() → useChatStore.submitInpaintTask({ ..., maskBlob, prompt })

import { AnimatePresence, motion } from "framer-motion";
import { Loader2, Sparkles, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button, IconButton, Textarea, Tooltip } from "@/components/ui/primitives";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { logError } from "@/lib/logger";
import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import { nearestAspectRatio } from "@/lib/sizing";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/useChatStore";
import { useInpaintStore } from "@/store/useInpaintStore";

import { MaskBoard, type MaskBoardHandle } from "./MaskBoard";

const FULL_COVERAGE_WARN = 0.95;
const WARNING_AUTO_DISMISS_MS = 5_000;
const CLOSE_CONFIRM_TIMEOUT_MS = 2_500;
// prompt 输入限制：UI 层按字符计 1500 提示用户，提交时走全局 MAX_PROMPT_CHARS（10000）才硬挡
const SOFT_PROMPT_LIMIT = 1500;

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function InpaintModal() {
  const open = useInpaintStore((s) => s.open);
  // AnimatePresence 包外层，open false 时仍能播 exit
  return (
    <AnimatePresence>
      {open ? <InpaintModalInner key="inpaint-modal" /> : null}
    </AnimatePresence>
  );
}

function InpaintModalInner() {
  const source = useInpaintStore((s) => s.source);
  const close = useInpaintStore((s) => s.close);
  const submitting = useInpaintStore((s) => s.submitting);
  const setSubmitting = useInpaintStore((s) => s.setSubmitting);
  const drafts = useInpaintStore((s) => s.drafts);
  const setDraft = useInpaintStore((s) => s.setDraft);
  const clearDraft = useInpaintStore((s) => s.clearDraft);
  const maskDrafts = useInpaintStore((s) => s.maskDrafts);
  const setMaskDraft = useInpaintStore((s) => s.setMaskDraft);
  const clearMaskDraft = useInpaintStore((s) => s.clearMaskDraft);
  const submitInpaintTask = useChatStore((s) => s.submitInpaintTask);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const boardRef = useRef<MaskBoardHandle | null>(null);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  const currentImageId = source?.imageId ?? null;
  const initialDraft = source ? drafts[source.imageId] ?? "" : "";
  // initialStrokes 给 MaskBoard 当 mount 默认值；source 变化时 MaskBoard 内部用 imageSrc prev-check 接管 reset
  const initialStrokes = currentImageId
    ? maskDrafts[currentImageId] ?? null
    : null;

  // —— 全部 useState / useRef 一次性声明（Hooks 顺序稳定） ——
  const [prompt, setPrompt] = useState(initialDraft);
  const prevImageIdRef = useRef(currentImageId);
  const [hasStroke, setHasStroke] = useState(
    () => (initialStrokes?.length ?? 0) > 0,
  );
  const [coverage, setCoverage] = useState(0);
  const [warning, setWarning] = useState<string | null>(null);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const confirmCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const submittingRef = useRef(submitting);
  useBodyScrollLock(true);
  useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);

  // —— imageId 切换时同步 state（modal 不重 mount 的边界场景：openInpaint(B) 直接覆盖）——
  // React 19 strict mode 下不能在 render 阶段连发 setState；切换同步放进 effect。
  // deps 只放 currentImageId：drafts/maskDrafts 是 zustand store 引用，每次写入引用变会让
  // effect 重跑然后早 return（prevImageIdRef 守卫），白白浪费。effect 内部用 getState() 读最新值。
  useEffect(() => {
    if (prevImageIdRef.current === currentImageId) return;
    prevImageIdRef.current = currentImageId;
    const snapshot = useInpaintStore.getState();
    setPrompt(currentImageId ? snapshot.drafts[currentImageId] ?? "" : "");
    setHasStroke(
      currentImageId
        ? (snapshot.maskDrafts[currentImageId]?.length ?? 0) > 0
        : false,
    );
    setCoverage(0);
    setWarning(null);
    setConfirmingClose(false);
  }, [currentImageId]);

  // imageId 切换时清掉 confirmClose timer（ref 操作必须在 effect 里）
  useEffect(() => {
    return () => {
      if (confirmCloseTimerRef.current) {
        clearTimeout(confirmCloseTimerRef.current);
        confirmCloseTimerRef.current = null;
      }
    };
  }, [currentImageId]);

  // ———— body 滚动锁 + main inert（无障碍：辅助技术不会跳到背景） ————
  useEffect(() => {
    const mainEls = Array.from(document.querySelectorAll("main"));
    const restore: Array<() => void> = [];
    for (const el of mainEls) {
      const prevInert = el.getAttribute("inert");
      const prevAria = el.getAttribute("aria-hidden");
      el.setAttribute("inert", "");
      el.setAttribute("aria-hidden", "true");
      restore.push(() => {
        if (prevInert === null) el.removeAttribute("inert");
        else el.setAttribute("inert", prevInert);
        if (prevAria === null) el.removeAttribute("aria-hidden");
        else el.setAttribute("aria-hidden", prevAria);
      });
    }

    return () => {
      restore.forEach((f) => f());
    };
  }, []);

  // 记录原 focus，关闭时还原
  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    const id = requestAnimationFrame(() => {
      // 没有草稿时直接 focus prompt；有草稿时不抢 focus（避免覆盖用户已选区）
      if (initialDraft.length === 0) {
        promptRef.current?.focus({ preventScroll: true });
      }
    });
    return () => {
      cancelAnimationFrame(id);
      const prev = previouslyFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus({ preventScroll: true });
        } catch {
          /* noop */
        }
      }
    };
    // 仅 mount 时执行（initialDraft 在首帧 derive；后续不重跑）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 用户继续操作（输入/涂抹）时取消"确认放弃"红色态——避免误导
  const cancelConfirmClose = useCallback(() => {
    if (!confirmingClose) return;
    setConfirmingClose(false);
    if (confirmCloseTimerRef.current) {
      clearTimeout(confirmCloseTimerRef.current);
      confirmCloseTimerRef.current = null;
    }
  }, [confirmingClose]);

  const handleStatsChange = useCallback(
    (stats: { coverage: number; strokeCount: number }) => {
      setCoverage(stats.coverage);
      setHasStroke(stats.strokeCount > 0);
    },
    [],
  );

  // strokes 持久化：MaskBoard 去抖 380ms 后回调；写入 store maskDrafts
  const handleStrokesChange = useCallback(
    (strokes: import("./types").Stroke[]) => {
      if (!currentImageId) return;
      if (strokes.length === 0) clearMaskDraft(currentImageId);
      else setMaskDraft(currentImageId, strokes);
    },
    [currentImageId, setMaskDraft, clearMaskDraft],
  );

  const promptText = prompt.trim();
  const promptValid = promptText.length > 0;
  // 提交时会按这个比例生成（submitInpaintTask 用 nearestAspectRatio 推断），让用户在涂抹前就知情
  const derivedAspect =
    source && source.width && source.height
      ? nearestAspectRatio(source.width, source.height)
      : null;
  const promptOverSoftLimit = prompt.length > SOFT_PROMPT_LIMIT;
  const promptOverHardLimit = prompt.length > MAX_PROMPT_CHARS;
  const canSubmit =
    !submitting &&
    hasStroke &&
    promptValid &&
    !promptOverHardLimit &&
    !!source;

  // prompt 草稿持久化（去抖 350ms 写 store，避免逐字符写）
  const draftDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!source) return;
    if (draftDebounceRef.current) clearTimeout(draftDebounceRef.current);
    draftDebounceRef.current = setTimeout(() => {
      setDraft(source.imageId, prompt);
    }, 350);
    return () => {
      if (draftDebounceRef.current) clearTimeout(draftDebounceRef.current);
    };
  }, [prompt, source, setDraft]);

  // warning 自动消失
  useEffect(() => {
    if (!warning) return;
    const id = setTimeout(() => setWarning(null), WARNING_AUTO_DISMISS_MS);
    return () => clearTimeout(id);
  }, [warning]);

  const dirty = hasStroke || prompt.trim().length > 0;

  const handleClose = useCallback(() => {
    if (submittingRef.current) return;
    if (!dirty) {
      close();
      return;
    }
    if (confirmingClose) {
      // 第二次点：真关
      if (confirmCloseTimerRef.current) {
        clearTimeout(confirmCloseTimerRef.current);
        confirmCloseTimerRef.current = null;
      }
      setConfirmingClose(false);
      // 关闭时清掉草稿（用户明确放弃 prompt + mask）
      if (source) {
        clearDraft(source.imageId);
        clearMaskDraft(source.imageId);
      }
      close();
      return;
    }
    // 第一次点：进入确认态，2.5s 后自动回退
    setConfirmingClose(true);
    if (confirmCloseTimerRef.current) {
      clearTimeout(confirmCloseTimerRef.current);
    }
    confirmCloseTimerRef.current = setTimeout(() => {
      setConfirmingClose(false);
      confirmCloseTimerRef.current = null;
    }, CLOSE_CONFIRM_TIMEOUT_MS);
  }, [dirty, confirmingClose, close, source, clearDraft, clearMaskDraft]);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit || !source) return;
    setWarning(null);
    let m;
    try {
      m = await boardRef.current?.exportMask();
    } catch (err) {
      logError(err, { scope: "inpaint", code: "mask_export_failed" });
      setWarning("蒙版导出失败");
      return;
    }
    if (!m) {
      setWarning("画布未就绪或未涂抹");
      return;
    }
    if (m.coverage > FULL_COVERAGE_WARN) {
      setWarning(
        `涂抹 ${(m.coverage * 100).toFixed(0)}%，接近整图重画`,
      );
    }
    setSubmitting(true);
    try {
      await submitInpaintTask({
        sourceImageId: source.imageId,
        sourceSrc: source.src,
        // source.width/height 是入口透传的（imagesById），缺失时退到 mask 导出回带的实测尺寸
        // —— 后者从 imgEl.naturalWidth/Height 取，必定有值（exportMask 早返回了）
        sourceWidth: source.width ?? m.width,
        sourceHeight: source.height ?? m.height,
        maskBlob: m.blob,
        maskPreviewDataUrl: m.preview_data_url,
        prompt: promptText,
      });
      pushMobileToast("已加入生成 · 在对话中查看进度", "success");
      // 提交成功：清掉 prompt + mask 草稿，再关闭 modal
      clearDraft(source.imageId);
      clearMaskDraft(source.imageId);
      // close 守门 submitting，先把状态清掉再关
      useInpaintStore.setState({
        open: false,
        source: null,
        submitting: false,
      });
    } catch (err) {
      logError(err, { scope: "inpaint", code: "submit_failed" });
      const msg = err instanceof Error ? err.message : "提交失败";
      setWarning(`提交失败 · ${msg}`);
      setSubmitting(false);
    }
  }, [
    canSubmit,
    source,
    submitInpaintTask,
    promptText,
    setSubmitting,
    clearDraft,
    clearMaskDraft,
  ]);

  // ———— 全局键盘事件：Esc 关闭 + Tab focus trap + ⌘/Ctrl+Enter 提交 ————
  const onRootKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        handleClose();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        void handleSubmit();
        return;
      }
      if (e.key === "Tab") {
        const root = rootRef.current;
        if (!root) return;
        const focusables = Array.from(
          root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
        ).filter(
          (el) =>
            !el.hasAttribute("data-focus-skip") && el.offsetParent !== null,
        );
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !root.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (active === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    },
    [handleClose, handleSubmit],
  );

  // 取消按钮文案 / 样式
  const inConfirmClose = confirmingClose;

  // 计算限制状态文案
  const promptCounterClass = cn(
    "tabular-nums",
    promptOverHardLimit
      ? "text-danger"
      : promptOverSoftLimit
        ? "text-warning"
        : "text-[var(--fg-1)]/80",
  );

  return (
    <motion.div
      key="inpaint-overlay"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.16 }}
      className={cn(
        "fixed inset-0 z-[var(--z-dialog)]",
        "bg-black/76 backdrop-blur-md",
        "mobile-dialog-shell",
        "flex items-end justify-center sm:items-center",
      )}
      role="presentation"
      onPointerDown={(e) => {
        // 背景点击仅在没 dirty 时才关；dirty 时强制走双次确认逻辑
        if (e.target === e.currentTarget) {
          handleClose();
        }
      }}
    >
      <motion.div
        ref={rootRef}
        role="dialog"
        aria-modal="true"
        aria-label="局部修改"
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
        onKeyDown={onRootKeyDown}
        className={cn(
          "mobile-dialog-panel",
          "w-full max-w-[1100px]",
          // dvh = 浏览器栏 + 键盘动态视口；svh fallback 在更小视口
          // 桌面给固定高度（760px clamp 到视口），让 body flex-1 链路有撑开依据；
          // 之前 sm:h-auto 内容驱动 + MaskBoard fit 模式互相塌缩，整个弹窗缩成只有 header
          "h-[var(--mobile-dialog-max-height)] sm:h-[760px] sm:max-h-[calc(100dvh-3rem)]",
          "flex flex-col overflow-hidden",
          "max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none sm:rounded-[var(--radius-dialog)]",
          "border border-[var(--border)] bg-[var(--bg-1)]",
          "shadow-[var(--shadow-2)]",
        )}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--border-subtle)]">
          <div className="flex items-center gap-3 min-w-0">
            {source ? (
              <div
                className={cn(
                  "shrink-0 w-9 h-9 sm:w-10 sm:h-10 rounded-[var(--radius-control)] overflow-hidden",
                  "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                )}
                aria-hidden
                title={source.alt ?? "源图"}
              >
                {/* 缩略图 40x40，无需 next/image 的 LCP 优化；data: URL 在 next/image 下还要 unoptimized 配置 */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={source.src}
                  alt=""
                  className="w-full h-full object-cover"
                  draggable={false}
                />
              </div>
            ) : null}
            <div className="flex flex-col min-w-0">
              <h2 className="type-card-title">局部修改</h2>
              <p className="type-body-sm text-[var(--fg-1)] truncate">
                涂抹要修改的区域 · 描述要替换成什么
              </p>
            </div>
          </div>
          <IconButton
            variant={inConfirmClose ? "danger" : "ghost"}
            onClick={handleClose}
            disabled={submitting}
            aria-label={inConfirmClose ? "确认放弃涂抹" : "关闭"}
            tooltip={inConfirmClose ? "再点一次确认放弃" : "关闭 (Esc)"}
            className="rounded-full"
          >
            <X className="w-4 h-4" />
          </IconButton>
        </div>

        {/* Body：桌面左右分栏，移动上下分栏 */}
        <div
          className={cn(
            "flex-1 min-h-0 overflow-hidden",
            "flex flex-col md:flex-row",
          )}
        >
          {/* 画板区域 */}
          <div
            className={cn(
              // MaskBoard 内 ResizeObserver fit 容器，外层不需要 overflow-auto
              "flex-1 min-w-0 min-h-0 overflow-hidden p-3 sm:p-4 bg-[var(--bg-1)]",
              "md:border-r md:border-[var(--border-subtle)]",
            )}
            onPointerDown={cancelConfirmClose}
          >
            {!source ? (
              <div className="flex h-full items-center justify-center type-body-sm text-[var(--fg-1)]">
                <Loader2 className="w-4 h-4 animate-spin mr-2" />
                图片加载中
              </div>
            ) : (
              <MaskBoard
                ref={boardRef}
                imageSrc={source.src}
                disabled={submitting}
                initialStrokes={initialStrokes}
                onStrokesChange={handleStrokesChange}
                onStatsChange={handleStatsChange}
              />
            )}
          </div>

          {/* Prompt 侧栏（桌面 320 宽，移动满宽且 sticky 在底部） */}
          <div
            className={cn(
              "shrink-0 flex flex-col gap-3 p-3 sm:p-4",
              "md:w-[320px] md:max-w-[320px]",
              "max-md:max-h-[min(44dvh,20rem)]",
              "bg-[var(--bg-0)]",
              "mobile-dialog-scroll overflow-y-auto",
              "border-t border-[var(--border-subtle)] md:border-t-0",
            )}
          >
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <label
                  htmlFor="inpaint-prompt"
                  className="block text-[12px] font-medium text-[var(--fg-1)]"
                >
                  把涂抹区域改成什么？
                </label>
                {derivedAspect && (
                  <span
                    className={cn(
                      "shrink-0 inline-flex items-center gap-1 px-2 h-5 rounded-full",
                      "text-[10px] tabular-nums",
                      "bg-[var(--bg-2)] text-[var(--fg-1)] border border-[var(--border-subtle)]",
                    )}
                    title="按原图比例生成（避免构图变形）"
                  >
                    <span className="text-[var(--fg-2)]">比例</span>
                    {derivedAspect}
                  </span>
                )}
              </div>
              <Textarea
                id="inpaint-prompt"
                ref={promptRef}
                value={prompt}
                onChange={(e) => {
                  setPrompt(e.target.value.slice(0, MAX_PROMPT_CHARS));
                  cancelConfirmClose();
                }}
                placeholder="描述涂抹区域要变成什么"
                rows={3}
                className={cn(
                  "resize-none min-h-[84px] md:min-h-[120px]",
                  promptOverHardLimit && "border-[var(--danger)]",
                )}
                disabled={submitting}
              />
              <div className="mt-1 flex items-center justify-between text-[11px] text-[var(--fg-1)]/80">
                <span className="truncate">⌘/Ctrl + Enter 提交</span>
                <span className={promptCounterClass}>
                  {prompt.length}/{SOFT_PROMPT_LIMIT}
                </span>
              </div>
            </div>

            {/* 引导提示：保持简短，移动端隐藏省空间 */}
            <div className="hidden md:block rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/40 p-2.5 text-[11.5px] leading-relaxed text-[var(--fg-1)]/90">
              <strong className="font-medium text-[var(--fg-0)]">提示</strong>
              ：仅描述涂抹区域，越具体越准。
              <Tooltip
                content="不要描述整张图；只写涂抹区域要变成什么。"
                side="top"
              >
                <span className="ml-1 text-[var(--info)] underline decoration-dotted cursor-help">
                  详解
                </span>
              </Tooltip>
            </div>

            {warning && (
              <motion.div
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.18 }}
                className={cn(
                  "rounded-[var(--radius-control)] p-2 text-[11.5px]",
                  "bg-warning-soft text-warning",
                )}
                role="status"
                aria-live="polite"
              >
                {warning}
              </motion.div>
            )}

            {/* Cheatsheet（桌面才显示，移动端省空间） */}
            <div className="hidden md:block text-[10.5px] text-[var(--fg-1)]/70 leading-relaxed">
              <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
                <span>
                  <Kbd>B</Kbd> 画笔 / <Kbd>E</Kbd> 橡皮
                </span>
                <span>
                  <Kbd>[</Kbd> <Kbd>]</Kbd> 调画笔
                </span>
                <span>
                  <Kbd>Z</Kbd> 撤销
                </span>
                <span>
                  <Kbd>Esc</Kbd> 关闭
                </span>
              </div>
            </div>

            <div className="mobile-dialog-footer mt-auto flex items-center justify-end gap-2 pt-2">
              <Button
                variant={inConfirmClose ? "danger" : "ghost"}
                size="md"
                onClick={handleClose}
                disabled={submitting}
              >
                {inConfirmClose ? "确认放弃" : "取消"}
              </Button>
              <Button
                variant="primary"
                size="md"
                onClick={() => void handleSubmit()}
                disabled={!canSubmit}
                loading={submitting}
                className="min-w-[112px]"
              >
                {!hasStroke ? (
                  "未涂抹"
                ) : !promptValid ? (
                  "指令为空"
                ) : promptOverHardLimit ? (
                  "字数超限"
                ) : (
                  <>
                    <Sparkles className="w-3.5 h-3.5" />
                    生成
                  </>
                )}
              </Button>
            </div>

            {/* 实时统计（移动端可见，桌面已在 board 工具栏显示） */}
            <div className="md:hidden -mt-1 text-[11px] text-[var(--fg-1)]/70 text-right">
              {hasStroke
                ? `已涂抹 ${Math.round(coverage * 100)}%`
                : "未涂抹"}
            </div>
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className={cn(
        "inline-flex items-center justify-center min-w-4 h-4 px-1 mx-0.5 rounded",
        "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
        "text-[9.5px] font-mono text-[var(--fg-1)]",
      )}
    >
      {children}
    </kbd>
  );
}
