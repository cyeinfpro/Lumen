import Link from "next/link";
import { AlertTriangle, ArrowRight, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";

import { Button, Input, Skeleton } from "@/components/ui/primitives";

export function ProjectActionsSheet({
  title,
  itemTitle,
  error,
  renaming,
  onTitleChange,
  onStartRename,
  onCancelRename,
  onSaveRename,
  onStartDelete,
  onClose,
  patchPending,
}: {
  title: string;
  itemTitle: string;
  error: string | null;
  renaming: boolean;
  onTitleChange: (value: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onSaveRename: () => void;
  onStartDelete: () => void;
  onClose: () => void;
  patchPending: boolean;
}) {
  return (
    <div className="grid gap-4 px-5 pb-5 pt-3">
      <div className="border-b border-[var(--border)] pb-3">
        <p className="type-caption text-[var(--fg-2)]">
          项目
        </p>
        <p className="mt-1 truncate type-body font-semibold text-[var(--fg-0)]">
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
          <Input
            label="项目名称"
            error={error ?? undefined}
            disabled={patchPending}
            value={title}
            onChange={(event) => onTitleChange(event.target.value)}
            maxLength={120}
            autoFocus
          />
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="ghost" onClick={onCancelRename} className="min-h-11">
              取消
            </Button>
            <Button type="submit" disabled={patchPending} className="min-h-11">
              保存
            </Button>
          </div>
        </form>
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
    <div role="status" aria-label="项目加载中" className="divide-y divide-[var(--border-subtle)]">
      {Array.from({ length: 8 }).map((_, index) => (
        <div key={index} className="flex items-center gap-3 py-3">
          <Skeleton className="h-16 w-12 shrink-0 rounded-[var(--radius-control)]" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ErrorPanel({ onRetry, forbidden = false }: { onRetry: () => void; forbidden?: boolean }) {
  return (
    <div role="alert" className="border-y border-danger-border bg-danger-soft px-5 py-6 md:px-6 md:py-7">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-danger-border text-[var(--danger)]">
          <AlertTriangle className="h-4 w-4" />
        </span>
        <div className="flex-1">
          <h3 className="type-card-title">
            {forbidden ? "项目访问被拒绝" : "项目加载失败"}
          </h3>
          <p className="type-body-sm mt-0.5">
            {forbidden ? "当前账号没有访问权限。" : "网络错误或服务繁忙，稍后重试。"}
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
            暂无服饰项目
          </h2>
          <p className="type-body-sm mt-3 max-w-2xl">
            新项目的素材、生成任务和交付结果将在这里汇总。
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
