"use client";

import type { ReleaseInfo } from "@/lib/apiClient";
import { ConfirmDialog } from "@/components/ui/primitives";
import type { PendingUpdateConfirm } from "./AdminUpdatePanel.helpers";

export function UpdateConfirmDialog({
  pending,
  confirming,
  onClose,
  onConfirm,
}: {
  pending: PendingUpdateConfirm | null;
  confirming: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const description = pending ? (
    <div className="space-y-2">
      <p>
        将更新到
        <span className="font-mono text-[var(--fg-0)]">
          {" "}
          {pending.targetTag}
        </span>
        ，期间服务会重启并短暂不可用。
      </p>
      <p className="text-[var(--fg-2)]">请确认目标版本无误后再继续。</p>
    </div>
  ) : null;
  return (
    <ConfirmDialog
      open={pending != null}
      onOpenChange={(open) => {
        if (!open && !confirming) onClose();
      }}
      title="确认运行更新？"
      description={description}
      confirmText="确认更新"
      cancelText="取消"
      tone="danger"
      confirming={confirming}
      onConfirm={onConfirm}
    />
  );
}

export function RollbackConfirmDialog({
  pending,
  confirming,
  onClose,
  onConfirm,
}: {
  pending: ReleaseInfo | null;
  confirming: boolean;
  onClose: () => void;
  onConfirm: (releaseId: string) => void;
}) {
  const description = pending
    ? `回滚到 release ${pending.id}？将切回旧代码并重启 Lumen 服务（约 30 秒不可用）。数据库不会回滚，仅切代码。`
    : "";
  return (
    <ConfirmDialog
      open={pending != null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="回滚到此版本？"
      description={description}
      confirmText="确认回滚"
      cancelText="取消"
      tone="danger"
      confirming={confirming}
      onConfirm={() => {
        if (pending) onConfirm(pending.id);
      }}
    />
  );
}
