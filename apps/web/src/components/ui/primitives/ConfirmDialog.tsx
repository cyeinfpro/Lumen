"use client";

// 受控确认对话框。支持 danger tone（红色确认按钮）。Esc 或点击背景关闭。

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "./Button";
import { Dialog } from "./Dialog";

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
  /** Stable resource identity when the title is not a string. */
  resetKey?: string | number;
}

function useConfirmationFeedback(open: boolean, title: React.ReactNode, resetKey?: string | number) {
  const contextKey = resetKey ?? (typeof title === "string" ? title : undefined);
  const [feedback, setFeedback] = useState({
    open, contextKey, failed: false, session: Symbol(),
  });
  if (feedback.open !== open || !Object.is(feedback.contextKey, contextKey)) {
    setFeedback({ open, contextKey, failed: false, session: Symbol() });
  }
  return {
    failed: feedback.open === open && Object.is(feedback.contextKey, contextKey) && feedback.failed,
    clearFailure: () => setFeedback((current) => ({ ...current, failed: false })),
    recordFailure: () => setFeedback((current) => current.session === feedback.session
      ? { ...current, failed: true }
      : current),
  };
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = "确认",
  cancelText = "取消",
  onConfirm,
  onCancel,
  tone = "default",
  confirming = false,
  resetKey,
}: ConfirmDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const errorId = useId();
  const [pending, setPending] = useState(false);
  const { failed, clearFailure, recordFailure } = useConfirmationFeedback(open, title, resetKey);
  const busy = confirming || pending;
  // Keep the synchronous lock and short cooldown independent of React rendering.
  // Callers may omit confirming or only publish pending on the next render.
  const runningRef = useRef(false);
  const lastConfirmAtRef = useRef(0);
  useEffect(() => {
    if (!open) lastConfirmAtRef.current = 0;
  }, [open]);
  // 只在 onConfirm 真正执行中禁掉取消，别把用户锁死在弹窗里。
  const handleCancel = useCallback(() => {
    if (confirming || runningRef.current) return;
    onOpenChange(false);
    onCancel?.();
  }, [confirming, onCancel, onOpenChange]);

  const handleConfirm = async () => {
    if (confirming || runningRef.current) return;
    const now = Date.now();
    if (now - lastConfirmAtRef.current < CONFIRM_REFIRE_GUARD_MS) return;
    lastConfirmAtRef.current = now;
    runningRef.current = true;
    setPending(true);
    clearFailure();
    try {
      await onConfirm();
    } catch {
      // Do not expose raw API exceptions or imply an uncertain mutation was undone.
      recordFailure();
    } finally {
      runningRef.current = false;
      setPending(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleCancel}
      aria-busy={busy}
      closeOnEscape={!busy}
      closeOnBackdrop={!busy}
      aria-labelledby={titleId}
      aria-describedby={[
        description ? descriptionId : null,
        failed ? errorId : null,
      ].filter(Boolean).join(" ") || undefined}
      className="max-w-sm"
    >
      <Dialog.Header>
        <h2
          id={titleId}
          className={cn(
            "type-card-title text-balance",
            tone === "danger" && "text-[var(--danger-fg)]",
          )}
        >
          {title}
        </h2>
      </Dialog.Header>
      {description || failed ? (
        <Dialog.Body>
          {description ? (
            <div id={descriptionId} className="type-body-sm break-words text-pretty text-[var(--fg-1)]">
              {description}
            </div>
          ) : null}
          {failed ? (
            <p id={errorId} role="alert" className="mt-2 type-body-sm break-words text-[var(--danger-fg)]">
              操作结果未确认，核对对象状态后重试。
            </p>
          ) : null}
        </Dialog.Body>
      ) : null}
      <Dialog.Footer className="aria-disabled:pointer-events-none">
        <Button
          variant="ghost"
          size="sm"
          onClick={handleCancel}
          disabled={busy}
        >
          {cancelText}
        </Button>
        <Button
          variant={tone === "danger" ? "danger" : "primary"}
          size="sm"
          onClick={handleConfirm}
          loading={busy}
        >
          {confirmText}
        </Button>
      </Dialog.Footer>
    </Dialog>
  );
}
