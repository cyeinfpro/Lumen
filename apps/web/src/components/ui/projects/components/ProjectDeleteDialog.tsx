"use client";

import { useState } from "react";
import { ConfirmDialog } from "@/components/ui/primitives";

export const PROJECT_DELETE_IMPACT = "项目、关联对话和生成图片将被移除，进行中的任务将取消。已保存到模特库的图片保留。";

export function ProjectDeleteDialog({ open, title, pending, onOpenChange, onConfirm }: {
  open: boolean;
  title: string;
  pending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => Promise<unknown>;
}) {
  const [error, setError] = useState<string | null>(null);
  return <ConfirmDialog
    open={open}
    onOpenChange={(next) => { if (!pending) { setError(null); onOpenChange(next); } }}
    title={`删除“${title}”？`}
    description={<><p>{PROJECT_DELETE_IMPACT}</p>{error ? <p role="alert" className="mt-2 text-[var(--danger-fg)]">{error}</p> : null}</>}
    confirmText="删除"
    tone="danger"
    confirming={pending}
    onConfirm={async () => {
      setError(null);
      try {
        await onConfirm();
        onOpenChange(false);
      } catch {
        setError("删除结果未确认，核对项目状态后重试。");
      }
    }}
  />;
}
