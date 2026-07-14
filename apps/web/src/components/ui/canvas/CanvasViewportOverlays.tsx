import { Cable, Plus, X } from "lucide-react";

import { BottomSheet } from "@/components/ui/primitives/mobile";
import type { CompatibleTarget } from "./CanvasViewportModel";
import styles from "./canvas.module.css";

export function CanvasEmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className={styles.emptyState}>
      <div className={styles.emptyStateContent}>
        <span className={styles.emptyStateIcon} aria-hidden>
          <Plus />
        </span>
        <p className={styles.emptyStateTitle}>开始构建画布</p>
        <p className={styles.emptyStateCopy}>从一个节点开始。</p>
        <button
          type="button"
          className={styles.emptyStateAction}
          onClick={onCreate}
        >
          <Plus aria-hidden />
          创建节点
        </button>
      </div>
    </div>
  );
}

export function MobileConnectTargets({
  open,
  targets,
  onOpen,
  onClose,
  onCancel,
  onSelect,
}: {
  open: boolean;
  targets: CompatibleTarget[];
  onOpen: () => void;
  onClose: () => void;
  onCancel: () => void;
  onSelect: (target: CompatibleTarget) => void;
}) {
  return (
    <>
      <div className="absolute inset-x-3 top-3 z-[var(--z-tabbar)] flex items-center justify-center gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/96 px-3 type-body-sm text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-xl"
        >
          <Cable className="h-4 w-4 text-[var(--accent)]" />
          兼容目标 {targets.length}
        </button>
        <button
          type="button"
          aria-label="取消连接"
          title="取消连接"
          onClick={onCancel}
          className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/96 text-[var(--fg-1)] shadow-[var(--shadow-2)] backdrop-blur-xl"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <BottomSheet
        open={open}
        onClose={onClose}
        ariaLabel="兼容连接目标"
        snapPoints={["62%"]}
        className="mobile-dialog-sheet"
      >
        <div className="mobile-dialog-scroll h-full overflow-y-auto p-4">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="type-page-kicker">连接目标</p>
              <h2 className="type-card-title mt-1">选择兼容端口</h2>
            </div>
            <button
              type="button"
              aria-label="关闭连接目标"
              title="关闭"
              onClick={onClose}
              className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors active:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="mt-4 grid gap-2">
            {targets.length > 0 ? (
              targets.map((target) => (
                <button
                  key={target.key}
                  type="button"
                  onClick={() => onSelect(target)}
                  className="flex min-h-12 w-full items-center justify-between gap-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-left transition-colors active:bg-[var(--bg-3)]"
                >
                  <span className="min-w-0">
                    <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
                      {target.nodeTitle}
                    </span>
                    <span className="block truncate type-caption text-[var(--fg-2)]">
                      {target.nodeType}
                    </span>
                  </span>
                  <span className="shrink-0 type-caption text-[var(--accent)]">
                    {target.handleLabel}
                  </span>
                </button>
              ))
            ) : (
              <p className="py-8 text-center type-body-sm text-[var(--fg-2)]">
                当前没有可连接的目标端口。
              </p>
            )}
          </div>
        </div>
      </BottomSheet>
    </>
  );
}
