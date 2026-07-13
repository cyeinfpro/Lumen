"use client";

import {
  ArrowLeft,
  Copy,
  Image as ImageIcon,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  Trash2,
  Video,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  useCanvasesQuery,
  useDeleteCanvasMutation,
  useDuplicateCanvasMutation,
  usePatchCanvasMutation,
} from "@/lib/queries/canvases";
import type { CanvasListItem } from "@/lib/canvas/types";
import { Button, IconButton, Input, toast } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "@/components/ui/projects/components/ProjectTopBar";

export function CanvasProjectIndex() {
  const query = useCanvasesQuery({ limit: 60 });
  const router = useRouter();
  const duplicate = useDuplicateCanvasMutation();
  const remove = useDeleteCanvasMutation();
  const [active, setActive] = useState<CanvasListItem | null>(null);
  const [renaming, setRenaming] = useState<CanvasListItem | null>(null);

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <ProjectMobileTopBar
        title="无限画布"
        subtitle="自由工作流"
        backHref="/projects"
        right={
          <Link
            href="/projects/canvas/new"
            aria-label="新建画布"
            className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-[var(--accent)] text-[var(--accent-on)]"
          >
            <Plus className="h-5 w-5" />
          </Link>
        }
      />
      <ProjectTopBar
        right={
          <Link
            href="/projects/canvas/new"
            className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-3 type-body-sm font-medium text-[var(--accent-on)]"
          >
            <Plus className="h-4 w-4" />
            新建画布
          </Link>
        }
      />
      <main className="mb-[var(--mobile-tabbar-height)] min-h-0 flex-1 overflow-y-auto px-3 pb-8 pt-3 min-[390px]:px-4 md:mb-0 md:px-6 md:py-6">
        <div className="mx-auto w-full max-w-[var(--content-workbench)]">
          <header className="border-b border-[var(--border)] pb-5">
            <Link
              href="/projects"
              className="hidden min-h-10 items-center gap-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:inline-flex"
            >
              <ArrowLeft className="h-4 w-4" />
              返回项目
            </Link>
            <div className="mt-2 flex items-end justify-between gap-4">
              <div>
                <p className="type-page-kicker">Canvas Projects</p>
                <h1 className="type-page-title mt-1">无限画布</h1>
                <p className="type-body-sm mt-2 text-[var(--fg-1)]">
                  组织提示词、素材、图片与视频生成，不占用固定工作流记录。
                </p>
              </div>
              <span className="hidden type-metric text-[var(--fg-2)] md:block">
                {query.data?.items.length ?? 0}
              </span>
            </div>
          </header>

          {query.isLoading ? (
            <div className="grid min-h-[320px] place-items-center">
              <Loader2 className="h-6 w-6 animate-spin text-[var(--accent)]" />
            </div>
          ) : query.isError ? (
            <div className="grid min-h-[320px] place-items-center text-center">
              <div>
                <p className="type-card-title">画布加载失败</p>
                <Button className="mt-4" onClick={() => query.refetch()}>
                  重试
                </Button>
              </div>
            </div>
          ) : query.data?.items.length ? (
            <div className="grid gap-3 pt-4 sm:grid-cols-2 xl:grid-cols-3">
              {query.data.items.map((item) => (
                <CanvasProjectCard
                  key={item.id}
                  item={item}
                  onMore={() => setActive(item)}
                  onRename={() => setRenaming(item)}
                />
              ))}
            </div>
          ) : (
            <div className="grid min-h-[420px] place-items-center text-center">
              <div className="max-w-sm">
                <span className="mx-auto grid h-14 w-14 place-items-center rounded-[var(--radius-card)] bg-[var(--accent-soft)] text-[var(--accent)]">
                  <Workflow className="h-6 w-6" />
                </span>
                <h2 className="type-section-title mt-4">创建第一张画布</h2>
                <p className="type-body-sm mt-2 text-[var(--fg-2)]">
                  默认会创建并连接提示词与图片生成节点。
                </p>
                <Link
                  href="/projects/canvas/new"
                  className="mt-5 inline-flex h-11 items-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 type-body-sm font-medium text-[var(--accent-on)]"
                >
                  <Plus className="h-4 w-4" />
                  新建画布
                </Link>
              </div>
            </div>
          )}
        </div>
      </main>
      <ProjectMobileTabBar />

      <BottomSheet
        open={Boolean(active)}
        onClose={() => setActive(null)}
        ariaLabel="画布操作"
      >
        {active ? (
          <div className="mobile-dialog-scroll p-4">
            <p className="type-page-kicker">画布操作</p>
            <h2 className="type-card-title mt-1 mb-4 truncate">{active.title}</h2>
            <div className="grid gap-2">
              <ActionButton
                icon={Pencil}
                label="重命名"
                onClick={() => {
                  setRenaming(active);
                  setActive(null);
                }}
              />
              <ActionButton
                icon={Copy}
                label="复制画布"
                loading={duplicate.isPending}
                onClick={async () => {
                  try {
                    const copy = await duplicate.mutateAsync(active.id);
                    setActive(null);
                    router.push(`/projects/canvas/${copy.id}`);
                  } catch (error) {
                    toast.error(error instanceof Error ? error.message : "复制失败");
                  }
                }}
              />
              <ActionButton
                icon={Trash2}
                label="删除画布"
                danger
                loading={remove.isPending}
                onClick={async () => {
                  if (!window.confirm(`删除“${active.title}”？生成资产不会被删除。`)) return;
                  try {
                    await remove.mutateAsync(active.id);
                    setActive(null);
                    toast.success("画布已删除");
                  } catch (error) {
                    toast.error(error instanceof Error ? error.message : "删除失败");
                  }
                }}
              />
            </div>
          </div>
        ) : null}
      </BottomSheet>

      {renaming ? (
        <RenameDialog
          item={renaming}
          onClose={() => setRenaming(null)}
          onSaved={() => {
            setRenaming(null);
            void query.refetch();
          }}
        />
      ) : null}
    </div>
  );
}

function CanvasProjectCard({
  item,
  onMore,
  onRename,
}: {
  item: CanvasListItem;
  onMore: () => void;
  onRename: () => void;
}) {
  return (
    <article className="surface-card surface-card-hover group overflow-hidden rounded-[var(--radius-card)]">
      <Link
        href={`/projects/canvas/${item.id}`}
        className="block aspect-[16/9] overflow-hidden bg-[var(--surface-media)]"
      >
        {item.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- API-backed canvas thumbnail.
          <img
            src={item.thumbnail_url}
            alt=""
            className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] group-hover:scale-[1.015]"
          />
        ) : (
          <div className="grid h-full place-items-center text-[var(--fg-3)]">
            <Workflow className="h-8 w-8" />
          </div>
        )}
      </Link>
      <div className="p-3">
        <div className="flex items-start gap-2">
          <Link href={`/projects/canvas/${item.id}`} className="min-w-0 flex-1">
            <h2 className="truncate type-card-title">{item.title}</h2>
            <p className="type-caption mt-1 text-[var(--fg-2)]">
              {item.node_count} 节点 · {item.edge_count} 连接
            </p>
          </Link>
          <IconButton
            aria-label="画布操作"
            tooltip="画布操作"
            onClick={onMore}
          >
            <MoreHorizontal className="h-4 w-4" />
          </IconButton>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-[var(--border-subtle)] pt-3 type-caption text-[var(--fg-2)]">
          <span className="inline-flex items-center gap-1">
            <ImageIcon className="h-3.5 w-3.5" />
            {item.image_output_count}
          </span>
          <span className="inline-flex items-center gap-1">
            <Video className="h-3.5 w-3.5" />
            {item.video_output_count}
          </span>
          {item.running_count > 0 ? (
            <span className="text-[var(--accent)]">{item.running_count} 运行中</span>
          ) : null}
          {item.has_conflict ? (
            <span className="text-[var(--danger-fg)]">版本冲突</span>
          ) : item.has_failure ? (
            <span className="text-[var(--danger-fg)]">执行失败</span>
          ) : null}
          <button
            type="button"
            onClick={onRename}
            className="ml-auto text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            重命名
          </button>
        </div>
      </div>
    </article>
  );
}

function RenameDialog({
  item,
  onClose,
  onSaved,
}: {
  item: CanvasListItem;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(item.title);
  const patch = usePatchCanvasMutation(item.id);
  return (
    <div className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] grid place-items-center bg-[var(--surface-scrim)] p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="重命名画布"
        className="mobile-dialog-panel surface-dialog w-full max-w-sm rounded-[var(--radius-dialog)] bg-[var(--bg-1)]"
      >
        <header className="border-b border-[var(--border)] p-4">
          <h2 className="type-card-title">重命名画布</h2>
        </header>
        <div className="p-4">
          <Input
            autoFocus
            label="名称"
            value={title}
            maxLength={255}
            onChange={(event) => setTitle(event.currentTarget.value)}
          />
        </div>
        <footer className="grid grid-cols-2 gap-2 border-t border-[var(--border)] p-3">
          <Button variant="secondary" onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={patch.isPending}
            disabled={!title.trim()}
            onClick={async () => {
              try {
                await patch.mutateAsync({ title: title.trim() });
                onSaved();
              } catch (error) {
                toast.error(error instanceof Error ? error.message : "保存失败");
              }
            }}
          >
            保存
          </Button>
        </footer>
      </div>
    </div>
  );
}

function ActionButton({
  icon: Icon,
  label,
  onClick,
  danger,
  loading,
}: {
  icon: typeof Copy;
  label: string;
  onClick: () => void;
  danger?: boolean;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={loading}
      onClick={onClick}
      className={`flex min-h-12 w-full items-center gap-3 rounded-[var(--radius-control)] border px-3 type-body-sm transition-colors ${
        danger
          ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-0)] hover:bg-[var(--bg-3)]"
      }`}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Icon className="h-4 w-4" />}
      {label}
    </button>
  );
}
