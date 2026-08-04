import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/primitives/Dialog";

export function MemoryCapabilityModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      aria-labelledby="memory-capability-title"
      className="w-full max-w-sm"
    >
      <Dialog.Header className="grid-cols-[auto_minmax(0,1fr)] items-center">
        <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />
        <h3
          id="memory-capability-title"
          className="type-card-title text-balance"
        >
          需要 embedding provider
        </h3>
      </Dialog.Header>
      <Dialog.Body>
        <p className="type-body-sm text-pretty text-[var(--fg-1)]">
          启用前需在管理员后台为某个 provider 勾选 “embedding”
          用途；记忆的写入、检索、抽取均依赖向量。
        </p>
      </Dialog.Body>
      <Dialog.Footer>
        <Button variant="outline" size="sm" onClick={onClose}>
          关闭
        </Button>
        <Link
          href="/admin"
          onClick={onClose}
          className="type-control inline-flex min-h-9 items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] px-3 text-[var(--accent-on)] shadow-[var(--shadow-1)] hover:bg-[var(--accent-hover)] max-sm:min-h-11"
        >
          去管理员后台
        </Link>
      </Dialog.Footer>
    </Dialog>
  );
}
