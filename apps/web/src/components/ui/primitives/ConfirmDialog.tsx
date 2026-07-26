"use client";

// 受控确认对话框。支持 danger tone（红色确认按钮）。Esc 或点击背景关闭。
// focus 管理：打开时把焦点移到 dialog，关闭时交还原始 active 元素。
//
// 不依赖 Radix；用 framer-motion 处理入出场。自带 body scroll lock。

import { motion, AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useId, useRef } from "react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { cn } from "@/lib/utils";
import { Button } from "./Button";
import { useModalLayer } from "./mobile/useModalLayer";

/**
 * 触发一次 onConfirm 后的冷却窗口（ms）。只需要盖住"双击 + 等 mutation
 * pending 生效"这段，故意取得短：更长会挡住用户看到错误后的正常重试。
 */
const CONFIRM_REFIRE_GUARD_MS = 1200;

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  onConfirm: () => void | Promise<void>;
  onCancel?: () => void;
  tone?: "default" | "danger";
  /** 确认按钮是否处于加载态（异步操作可由外部控制） */
  confirming?: boolean;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = "确定",
  cancelText = "取消",
  onConfirm,
  onCancel,
  tone = "default",
  confirming = false,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  // I-2：不能只靠外部 confirming 挡重复确认。
  // 1) 调用方可能压根不传（volcano-asset-manager-view 甚至硬编码 confirming
  //    ={false}），确认按钮全程可点；
  // 2) 即便传了 mutation.isPending，从 click 到 pending 生效之间隔着一次
  //    re-render，双击的第二下可以整个穿过这道空窗。
  // 确认动作大多是删除 / 提交 / 生成，重复触发 = 重复执行、重复扣费。
  // 两道闸门都在 ref 上（同步生效，不额外触发渲染，也不引入 effect 里 setState）：
  //   runningRef      —— onConfirm 执行期间硬锁；
  //   lastConfirmAtRef —— 刚触发过的短窗口内再点直接丢弃，挡住双击与渲染空窗。
  // 窗口过后仍可再点：用户在看到失败提示后本来就该能重试，做成永久置灰反而
  // 会变成"点了没反应"的哑按钮。
  const runningRef = useRef(false);
  const lastConfirmAtRef = useRef(0);
  useEffect(() => {
    if (!open) lastConfirmAtRef.current = 0;
  }, [open]);
  useBodyScrollLock(open);
  // 只在 onConfirm 真正执行中禁掉取消，别把用户锁死在弹窗里。
  const handleCancel = useCallback(() => {
    if (confirming || runningRef.current) return;
    onOpenChange(false);
    onCancel?.();
  }, [confirming, onCancel, onOpenChange]);
  const onDialogKeyDown = useModalLayer({
    open,
    rootRef: dialogRef,
    onClose: handleCancel,
  });

  const handleConfirm = async () => {
    if (confirming || runningRef.current) return;
    const now = Date.now();
    if (now - lastConfirmAtRef.current < CONFIRM_REFIRE_GUARD_MS) return;
    lastConfirmAtRef.current = now;
    runningRef.current = true;
    try {
      await onConfirm();
    } finally {
      runningRef.current = false;
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          key="confirm-overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          className={cn(
            "fixed inset-0 bg-black/55 backdrop-blur-sm z-[var(--z-dialog)]",
            // 桌面居中；移动端贴底（拇指可及 + 避开顶部刘海/浏览器 UI）
            "flex items-end justify-center p-0",
            "mobile-dialog-shell sm:items-center sm:justify-center",
          )}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) handleCancel();
          }}
        >
          <motion.div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-busy={confirming || undefined}
            aria-labelledby={titleId}
            aria-describedby={description ? descriptionId : undefined}
            tabIndex={-1}
            onKeyDown={onDialogKeyDown}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className={cn(
              "w-full max-w-sm",
              "mobile-dialog-panel overflow-hidden",
              "surface-dialog",
              "flex flex-col p-5 focus-visible:outline-none",
              // 移动端：底部 sheet，仅顶部圆角；safe-area 下补底 padding
              "max-sm:max-w-none max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none",
              "max-sm:border-b-0",
              // 桌面：四角圆角
              "sm:rounded-[var(--radius-dialog)]",
            )}
          >
            <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto pr-0.5">
              <h2
                id={titleId}
                className={cn(
                  "type-card-title text-balance",
                  tone === "danger" && "text-[var(--danger)]",
                )}
              >
                {title}
              </h2>
              {description ? (
                <div id={descriptionId} className="type-body-sm mt-1.5 text-pretty text-[var(--fg-1)]">
                  {description}
                </div>
              ) : null}
            </div>
            <div
              className={cn(
                "mobile-dialog-footer mt-5 flex shrink-0 gap-2 max-sm:-mx-5 max-sm:border-t max-sm:border-[var(--border)] max-sm:px-5 max-sm:pt-3",
                // 移动端纵向堆叠避免按钮被挤压；桌面横向右对齐
                "flex-col sm:flex-row sm:items-center sm:justify-end",
                "aria-disabled:pointer-events-none",
              )}
            >
              <Button
                variant="ghost"
                size="sm"
                onClick={handleCancel}
                disabled={confirming}
                aria-disabled={confirming || undefined}
                className="min-h-11 w-full sm:w-auto"
              >
                {cancelText}
              </Button>
              <Button
                variant={tone === "danger" ? "danger" : "primary"}
                size="sm"
                onClick={handleConfirm}
                loading={confirming}
                aria-disabled={confirming || undefined}
                className="min-h-11 w-full sm:w-auto"
              >
                {confirmText}
              </Button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
