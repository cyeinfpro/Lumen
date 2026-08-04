import Link from "next/link";
import { AlertTriangle, ArrowRight, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";

import { Button, Skeleton } from "@/components/ui/primitives";

export function ProjectActionsSheet({
  title,
  itemTitle,
  renaming,
  confirmingDelete,
  onTitleChange,
  onStartRename,
  onCancelRename,
  onSaveRename,
  onStartDelete,
  onCancelDelete,
  onConfirmDelete,
  onClose,
  patchPending,
  removePending,
}: {
  title: string;
  itemTitle: string;
  renaming: boolean;
  confirmingDelete: boolean;
  onTitleChange: (value: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onSaveRename: () => void;
  onStartDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
  onClose: () => void;
  patchPending: boolean;
  removePending: boolean;
}) {
  return (
    <div className="grid gap-4 px-5 pb-5 pt-3">
      <div className="border-b border-[var(--border)] pb-3">
        <p className="type-caption text-[var(--fg-2)]">
          项目
        </p>
        <p className="mt-1 truncate type-body font-semibold tracking-tight text-[var(--fg-0)]">
          {itemTitle || "服饰模特图"}
        </p>
      </div>
      {renaming ? (
        <form
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            onSaveRename();
          }}
        >
          <label className="grid gap-1.5">
            <span className="type-caption text-[var(--fg-2)]">
              名称
            </span>
            <input
              value={title}
              onChange={(event) => onTitleChange(event.target.value)}
              maxLength={120}
              autoFocus
              className="control-shell h-11 px-3 type-body text-[var(--fg-0)] outline-none focus:border-accent-border focus:shadow-[var(--ring)]"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="ghost" onClick={onCancelRename} className="min-h-11">
              取消
            </Button>
            <Button type="submit" disabled={patchPending} className="min-h-11">
              保存
            </Button>
          </div>
        </form>
      ) : confirmingDelete ? (
        <div className="grid gap-3">
          <p className="type-body text-[var(--fg-0)]">确认删除这个项目？</p>
          <p className="type-caption leading-5 text-[var(--fg-2)]">
            项目会从列表移除，关联对话不会被删除。
          </p>
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="ghost" onClick={onCancelDelete} className="min-h-11">
              取消
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={removePending}
              onClick={onConfirmDelete}
              className="min-h-11"
            >
              删除
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-1">
          <button
            type="button"
            onClick={onStartRename}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 px-1 text-left type-body text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-3)] active:bg-[var(--bg-3)]"
          >
            <Pencil className="h-4 w-4 text-[var(--fg-2)]" />
            重命名
          </button>
          <button
            type="button"
            onClick={onStartDelete}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 px-1 text-left type-body text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] active:bg-[var(--danger-soft)]"
          >
            <Trash2 className="h-4 w-4" />
            删除
          </button>
          <button
            type="button"
            onClick={onClose}
            className="mt-2 flex min-h-11 w-full cursor-pointer items-center justify-center border border-[var(--border)] px-3 type-body text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)]"
          >
            取消
          </button>
        </div>
      )}
    </div>
  );
}

export function SkeletonGrid() {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-7 md:gap-x-5 md:gap-y-9 lg:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: 8 }).map((_, index) => (
        <div key={index} className="grid gap-3">
          <Skeleton className="aspect-[3/4] w-full rounded-[var(--radius-card)]" />
          <div className="space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ErrorPanel({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-y border-danger-border bg-danger-soft px-5 py-6 md:px-6 md:py-7">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-danger-border text-[var(--danger)]">
          <AlertTriangle className="h-4 w-4" />
        </span>
        <div className="flex-1">
          <h3 className="type-card-title">
            项目加载失败
          </h3>
          <p className="type-body-sm mt-0.5">
            网络错误或服务繁忙，稍后重试。
          </p>
          <Button
            className="mt-3"
            variant="secondary"
            size="sm"
            onClick={onRetry}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            重试
          </Button>
        </div>
      </div>
    </div>
  );
}

export function EmptyHero() {
  return (
    <section className="page-section">
      <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:gap-8">
        <div>
          <p className="type-caption text-accent">
            服饰工作流
          </p>
          <h2 className="type-page-title mt-2 max-w-2xl ">
            从一张商品图，到一条完整的模特图工作流
          </h2>
          <p className="type-body-sm mt-3 max-w-2xl">
            上传商品图，确认模特候选，再进入展示图生成、质检和交付。每一步都可以继续编辑和回看。
          </p>
        </div>
        <Link
          href="/projects/apparel-model-showcase/new"
          className="group inline-flex shrink-0 items-center gap-2 self-start rounded-full bg-[var(--accent)] px-5 py-2.5 type-body-sm font-medium text-[var(--accent-on)] shadow-[var(--shadow-amber)] transition-transform duration-[var(--dur-base)] hover:scale-[1.02] active:scale-[0.98] md:self-end"
        >
          <Plus className="h-3.5 w-3.5" />
          创建第一个项目
          <ArrowRight className="h-3.5 w-3.5 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-hover:translate-x-0 group-hover:opacity-100" />
        </Link>
      </div>
    </section>
  );
}
