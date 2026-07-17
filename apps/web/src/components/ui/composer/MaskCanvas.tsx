"use client";

// MaskCanvas —— Composer 入口的局部修改 dialog 包装。
// 画板逻辑统一抽到 components/ui/inpaint/MaskBoard.tsx；这里只做：
//   - dialog 外壳（aria/Esc/scroll lock/submitting 锁）
//   - 取消 / 确认 操作按钮
//   - 覆盖率预警（>95% 提示用户基本全覆盖）
//
// 设计 V1 边界：
//   - 撤销栈仅维护本次会话；关闭弹窗后状态丢失（即"已 mask"显示，但二次进入是空白画布）
//   - 不做客户端羽化、不做 SAM、不做模板选区

import { AnimatePresence, motion } from "framer-motion";
import { Loader2, X } from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/primitives";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";

import type { MaskBoardHandle, MaskExport } from "../inpaint/MaskBoard";

export type { MaskExport } from "../inpaint/MaskBoard";

const MaskBoard = lazy(async () => {
  const mod = await import("../inpaint/MaskBoard");
  return { default: mod.MaskBoard };
});

// 涂满判定阈值：> 95% 提示用户"基本全覆盖了"，但仍允许提交（业务上有人就是要重画几乎全图）。
const FULL_COVERAGE_WARN = 0.95;

export interface MaskCanvasProps {
  open: boolean;
  /** 原图 data URL（与 attachment.data_url 一致） */
  imageSrc: string;
  /** 关闭弹窗（无论确认还是取消都会被调用前置） */
  onClose: () => void;
  /** 用户点击"确认"后回调，外部负责 toBlob → uploadImage → setMask */
  onConfirm: (mask: MaskExport) => void | Promise<void>;
  /** 提交中（外部上传 mask 的过程），用于按钮 loading 与禁用关闭 */
  submitting?: boolean;
}

// 外层只做 mount 门控：open=true 才挂内部 panel。
// 内部 panel 每次 mount 都是全新状态，避免 React 19 react-hooks/set-state-in-effect 的 reset 副作用。
export function MaskCanvas(props: MaskCanvasProps) {
  if (!props.open) return null;
  return <MaskCanvasInner {...props} />;
}

function MaskCanvasInner({
  imageSrc,
  onClose,
  onConfirm,
  submitting,
}: MaskCanvasProps) {
  const boardRef = useRef<MaskBoardHandle | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [hasStroke, setHasStroke] = useState(false);

  // ———— Esc 关闭 + body 滚动锁 ————
  const submittingRef = useRef(submitting);
  useBodyScrollLock(true);
  useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submittingRef.current) {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // hasStroke 由 MaskBoard 的 onStatsChange 回调驱动：撤销/清除/涂抹都会同步触发，
  // 避免原本只在外层 pointer 事件采样导致按钮"内部撤销"后仍 enabled 的脏状态。
  const handleStatsChange = useCallback(
    (stats: { coverage: number; strokeCount: number }) => {
      setHasStroke(stats.strokeCount > 0);
    },
    [],
  );

  const handleConfirm = useCallback(async () => {
    if (submitting) return;
    setWarning(null);
    const m = await boardRef.current?.exportMask();
    if (!m) {
      setWarning("画布尚未就绪或未涂抹任何区域");
      return;
    }
    if (m.coverage > FULL_COVERAGE_WARN) {
      setWarning(
        `已涂抹约 ${(m.coverage * 100).toFixed(0)}%，几乎全图重画 — 可继续，或撤销几笔后再试`,
      );
    }
    await onConfirm(m);
  }, [submitting, onConfirm]);

  return (
    <AnimatePresence>
      <motion.div
        key="mask-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.16 }}
        className={cn(
          "fixed inset-0 z-[var(--z-dialog)]",
          "bg-black/72 backdrop-blur-md mobile-perf-surface",
          "mobile-dialog-shell flex items-end justify-center sm:items-center",
          "px-3 sm:p-6",
        )}
      >
        <motion.div
          role="dialog"
          aria-modal="true"
          aria-label="局部修改 mask 画布"
          initial={{ opacity: 0, scale: 0.96, y: 8 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 8 }}
          transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
          className={cn(
            "mobile-dialog-panel w-full max-w-[860px]",
            "h-[var(--mobile-dialog-max-height)] sm:h-auto sm:max-h-[calc(100dvh-3rem)]",
            "flex flex-col overflow-hidden",
            "rounded-t-[var(--radius-sheet)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] sm:rounded-[var(--radius-dialog)] sm:border-b",
            "shadow-[var(--shadow-2)]",
          )}
        >
          {/* Header */}
          <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-[var(--border-subtle)]">
            <div className="flex flex-col">
              <h2 className="type-card-title">局部修改</h2>
              <p className="type-body-sm text-[var(--fg-1)]">
                涂抹要被重画的区域；红色高亮即 mask
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                if (submitting) return;
                onClose();
              }}
              disabled={submitting}
              aria-label="关闭"
              className={cn(
                "shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-full",
                "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
                "disabled:opacity-40 disabled:cursor-not-allowed",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* 画板（hasStroke 状态由 MaskBoard 的 onStatsChange 回调同步） */}
          <div className="mobile-dialog-scroll flex-1 min-h-0 overflow-auto p-3 sm:p-4 bg-[var(--bg-1)]">
            {!imageSrc ? (
              <div className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
                <Loader2 className="w-4 h-4 animate-spin" />
                正在载入图片…
              </div>
            ) : (
              <Suspense
                fallback={
                  <div className="flex min-h-[280px] items-center justify-center gap-2 text-sm text-[var(--fg-1)]">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    正在载入画布…
                  </div>
                }
              >
                <MaskBoard
                  ref={boardRef}
                  imageSrc={imageSrc}
                  disabled={submitting}
                  onStatsChange={handleStatsChange}
                />
              </Suspense>
            )}
          </div>

          {/* 警告 */}
          <AnimatePresence>
            {warning && (
              <motion.div
                initial={{
                  opacity: 0,
                  transform: "translateY(-4px)",
                }}
                animate={{
                  opacity: 1,
                  transform: "translateY(0)",
                }}
                exit={{
                  opacity: 0,
                  transform: "translateY(-4px)",
                }}
                transition={{
                  duration: DURATION.quick,
                  ease: EASE.develop,
                }}
                className="border-t border-[var(--border-subtle)]"
              >
                <div
                  className={cn(
                    "px-4 py-2 text-xs",
                    "bg-[var(--amber-400)]/10 text-[var(--amber-400)]",
                  )}
                >
                  {warning}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {/* 底部操作 */}
          <div className="mobile-dialog-footer flex items-center justify-end gap-2 px-4 py-3 border-t border-[var(--border-subtle)]">
            <Button
              variant="ghost"
              size="sm"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void handleConfirm()}
              disabled={!hasStroke || submitting}
              loading={submitting}
            >
              确认
            </Button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
