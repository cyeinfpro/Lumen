import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

export function MemoryCapabilityModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return <CapabilityModal onClose={onClose} />;
}

function CapabilityModal({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-sm mobile-dialog-shell sm:items-center"
      onClick={onClose}
    >
      <div
        className="mobile-dialog-panel flex w-full max-w-md flex-col overflow-hidden rounded-t-[var(--radius-dialog)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] p-5 shadow-[var(--shadow-3)] sm:rounded-[var(--radius-dialog)] sm:border-b"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-capability-title"
      >
        <div className="mobile-dialog-scroll min-h-0 overflow-y-auto pr-0.5">
          <div className="mb-2 flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />
            <h3 id="memory-capability-title" className="type-card-title">
              需要 embedding provider
            </h3>
          </div>
          <p className="type-body-sm leading-6 text-[var(--fg-1)]">
            启用前需在管理员后台为某个 provider 勾选 “embedding”
            用途；记忆的写入、检索、抽取均依赖向量。
          </p>
        </div>
        <div className="mobile-dialog-footer -mx-5 mt-5 flex shrink-0 flex-col gap-2 border-t border-[var(--border)] px-5 pt-3 sm:mx-0 sm:flex-row sm:justify-end sm:border-t-0 sm:px-0 sm:pt-0">
          <Button variant="outline" size="md" onClick={onClose}>
            {copy.action.confirm}
          </Button>
          <Link
            href="/admin"
            onClick={onClose}
            className="inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] bg-accent px-4 text-sm font-medium text-black"
          >
            去管理员后台
          </Link>
        </div>
      </div>
    </div>
  );
}
