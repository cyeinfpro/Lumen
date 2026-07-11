"use client";

// 受控确认对话框。支持 danger tone（红色确认按钮）。Esc 或点击背景关闭。
// focus 管理：打开时把焦点移到 dialog，关闭时交还原始 active 元素。
//
// 不依赖 Radix；用 framer-motion 处理入出场。自带 body scroll lock。

import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useRef } from "react";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { cn } from "@/lib/utils";
import { Button } from "./Button";

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
  const prevActiveRef = useRef<HTMLElement | null>(null);
  useBodyScrollLock(open);

  useEffect(() => {
    if (!open) return;
    prevActiveRef.current = (document.activeElement as HTMLElement) ?? null;
    // rAF 等一帧保证节点已挂载；preventScroll 避免页面跳动
    const raf = requestAnimationFrame(() => {
      dialogRef.current?.focus({ preventScroll: true });
    });
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onOpenChange(false);
        onCancel?.();
        return;
      }
      // 焦点陷阱：Tab / Shift+Tab 在 dialog 内循环
      if (e.key === "Tab") {
        const dialog = dialogRef.current;
        if (!dialog) return;
        const focusables = dialog.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (focusables.length === 0) {
          e.preventDefault();
          dialog.focus();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !dialog.contains(active)) {
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
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", handleKey);
      prevActiveRef.current?.focus?.();
    };
  }, [open, onOpenChange, onCancel]);

  const handleConfirm = async () => {
    await onConfirm();
  };

  const handleCancel = () => {
    onOpenChange(false);
    onCancel?.();
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
            aria-labelledby="confirm-dialog-title"
            tabIndex={-1}
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
                id="confirm-dialog-title"
                className={cn(
                  "type-card-title text-balance",
                  tone === "danger" && "text-[var(--danger)]",
                )}
              >
                {title}
              </h2>
              {description ? (
                <div className="type-body-sm mt-1.5 text-pretty text-[var(--fg-1)]">
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
                className="w-full sm:w-auto"
              >
                {cancelText}
              </Button>
              <Button
                variant={tone === "danger" ? "danger" : "primary"}
                size="sm"
                onClick={handleConfirm}
                loading={confirming}
                aria-disabled={confirming || undefined}
                className="w-full sm:w-auto"
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
