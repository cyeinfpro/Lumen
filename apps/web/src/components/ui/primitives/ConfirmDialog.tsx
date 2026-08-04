"use client";

// 受控确认对话框。支持 danger tone（红色确认按钮）。Esc 或点击背景关闭。

import { useCallback, useEffect, useId, useRef } from "react";
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
}: ConfirmDialogProps) {
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
    try {
      await onConfirm();
    } finally {
      runningRef.current = false;
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleCancel}
      aria-busy={confirming}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      className="max-w-sm"
    >
      <Dialog.Header>
        <h2
          id={titleId}
          className={cn(
            "type-card-title text-balance",
            tone === "danger" && "text-[var(--danger)]",
          )}
        >
          {title}
        </h2>
      </Dialog.Header>
      {description ? (
        <Dialog.Body>
          <div
            id={descriptionId}
            className="type-body-sm text-pretty text-[var(--fg-1)]"
          >
            {description}
          </div>
        </Dialog.Body>
      ) : null}
      <Dialog.Footer className="aria-disabled:pointer-events-none">
        <Button
          variant="ghost"
          size="sm"
          onClick={handleCancel}
          disabled={confirming}
          aria-disabled={confirming || undefined}
        >
          {cancelText}
        </Button>
        <Button
          variant={tone === "danger" ? "danger" : "primary"}
          size="sm"
          onClick={handleConfirm}
          loading={confirming}
          aria-disabled={confirming || undefined}
        >
          {confirmText}
        </Button>
      </Dialog.Footer>
    </Dialog>
  );
}
