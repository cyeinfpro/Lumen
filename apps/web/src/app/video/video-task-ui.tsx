"use client";

import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import {
  AlertCircle,
  ChevronDown,
  Copy,
  Film,
  ListVideo,
  Play,
  RefreshCw,
  RotateCw,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { Button, ErrorState, IconButton } from "@/components/ui/primitives";
import { DURATION, EASE } from "@/lib/motion";
import type { VideoGenerationOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { activeVideoTemporaryDownload } from "@/lib/videoEventSnapshot";

import {
  actionLabel,
  activeVideoTaskSummary,
  formatDurationLabel,
  hasVideo,
  isActiveVideo,
  isFailedHistoryVideo,
  stageCopy,
  taskElapsedLabel,
  taskErrorSummary,
  videoHistoryCountText,
  videoHistoryEmptyCopy,
} from "./video-task-model";
import type {
  VideoGenerationWithVideo,
  VideoHistoryFilter,
} from "./video-task-model";
import {
  prewarmVideoPreviewItem,
  VideoDownloadLink,
  VideoPosterButton,
  VideoPreviewDialogContent,
} from "./video-task-preview";
import type { VideoPreviewDialogProps } from "./video-task-preview";
import {
  isTopmostVideoDialog,
  restoreVideoWorkbenchFocus,
  trapVideoDialogFocus,
} from "./video-workbench-ui";

export function prewarmVideoItem(
  item: VideoGenerationWithVideo | null | undefined,
): void {
  prewarmVideoPreviewItem(item);
}

function ActiveVideoTaskSection({
  items,
  cancelPendingId,
  retryDisabled,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
}: {
  items: VideoGenerationOut[];
  cancelPendingId?: string;
  retryDisabled: boolean;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-3 px-1">
        <p className="type-caption text-[var(--fg-2)]">进行中</p>
        <span className="type-caption tabular-nums text-[var(--fg-2)]">
          {items.length} 条
        </span>
      </div>
      <div className="grid gap-2.5">
        {items.map((item) => (
          <TaskRow
            key={item.id}
            item={item}
            onCancel={() => onCancel(item)}
            cancelPending={cancelPendingId === item.id}
            onRetry={() => onRetry(item)}
            retryDisabled={retryDisabled}
            onCopy={() => onCopy(item)}
            onUseDraft={() => onUseDraft(item)}
            showPreview={false}
          />
        ))}
      </div>
    </section>
  );
}

function VideoTaskHistorySection({
  error,
  items,
  activeCount,
  historyFilter,
  historyCounts,
  loading,
  hasNextPage,
  fetchingNextPage,
  retryDisabled,
  selectedVideoId,
  onHistoryFilterChange,
  onLoadMore,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  error: boolean;
  items: VideoGenerationOut[];
  activeCount: number;
  historyFilter: VideoHistoryFilter;
  historyCounts: Record<VideoHistoryFilter, number>;
  loading: boolean;
  hasNextPage: boolean;
  fetchingNextPage: boolean;
  retryDisabled: boolean;
  selectedVideoId: string;
  onHistoryFilterChange: (value: VideoHistoryFilter) => void;
  onLoadMore: () => void;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
  onDelete: (item: VideoGenerationOut) => void;
  onPreview: (item: VideoGenerationOut) => void;
}) {
  const emptyCopy = videoHistoryEmptyCopy(historyFilter, activeCount, loading);
  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-3 px-1">
        <p className="type-caption text-[var(--fg-2)]">历史记录</p>
        <span className="type-caption tabular-nums text-[var(--fg-2)]">
          {videoHistoryCountText({
            loading,
            count: items.length,
            hasNextPage,
          })}
        </span>
      </div>
      <HistoryFilterTabs
        value={historyFilter}
        counts={historyCounts}
        loading={loading}
        onChange={onHistoryFilterChange}
      />
      <div className="grid gap-2.5">
        {items.map((item) => (
          <TaskRow
            key={item.id}
            item={item}
            onCancel={() => onCancel(item)}
            onRetry={() => onRetry(item)}
            retryDisabled={retryDisabled}
            onCopy={() => onCopy(item)}
            onUseDraft={() => onUseDraft(item)}
            onDelete={() => onDelete(item)}
            onPreview={hasVideo(item) ? () => onPreview(item) : undefined}
            selected={selectedVideoId === item.video?.id}
            showPreview={false}
          />
        ))}
        {items.length === 0 && !error && (
          <EmptyPanel
            icon={<Film className="h-5 w-5" />}
            title={emptyCopy.title}
            description={emptyCopy.description}
          />
        )}
        {hasNextPage && (
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            loading={fetchingNextPage}
            onClick={onLoadMore}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            {fetchingNextPage ? "加载中" : "加载更早记录"}
          </Button>
        )}
      </div>
    </section>
  );
}

export function VideoTaskDrawer({
  cancelPendingId,
  historyError,
  open,
  onClose,
  activeItems,
  historyItems,
  historyFilter,
  historyCounts,
  historyLoading,
  historyHasNextPage,
  historyFetchingNextPage,
  retryDisabled,
  selectedVideoId,
  onHistoryFilterChange,
  onRefresh,
  onLoadMore,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  cancelPendingId?: string;
  historyError?: string | null;
  open: boolean;
  onClose: () => void;
  activeItems: VideoGenerationOut[];
  historyItems: VideoGenerationOut[];
  historyFilter: VideoHistoryFilter;
  historyCounts: Record<VideoHistoryFilter, number>;
  historyLoading: boolean;
  historyHasNextPage: boolean;
  historyFetchingNextPage: boolean;
  retryDisabled: boolean;
  selectedVideoId: string;
  onHistoryFilterChange: (value: VideoHistoryFilter) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onCancel: (item: VideoGenerationOut) => void;
  onRetry: (item: VideoGenerationOut) => void;
  onCopy: (item: VideoGenerationOut) => void;
  onUseDraft: (item: VideoGenerationOut) => void;
  onDelete: (item: VideoGenerationOut) => void;
  onPreview: (item: VideoGenerationOut) => void;
}) {
  const reduceMotion = useReducedMotion();
  const panelRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const dialog = panelRef.current;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isTopmostVideoDialog(dialog)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      trapVideoDialogFocus(event, dialog);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      restoreVideoWorkbenchFocus(previousFocus, dialog);
    };
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex justify-end bg-[var(--surface-scrim)] sm:p-3"
          initial={{ opacity: reduceMotion ? 1 : 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: reduceMotion ? 1 : 0 }}
          transition={{ duration: reduceMotion ? 0 : DURATION.quick }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) onClose();
          }}
        >
          <motion.section
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="video-task-panel-title"
            tabIndex={-1}
            className="mobile-dialog-panel ml-auto flex h-full w-full max-w-[460px] flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:rounded-[var(--radius-panel)] sm:border-b landscape:max-sm:rounded-[var(--radius-panel)] landscape:max-sm:border-b"
            initial={{ x: reduceMotion ? 0 : 36, opacity: reduceMotion ? 1 : 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: reduceMotion ? 0 : 36, opacity: reduceMotion ? 1 : 0 }}
            transition={{
              duration: reduceMotion ? 0 : DURATION.normal,
              ease: EASE.develop,
            }}
          >
            <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3.5">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                    <ListVideo className="h-4 w-4" />
                  </span>
                  <div>
                    <h2
                      id="video-task-panel-title"
                      className="type-body-sm font-semibold text-[var(--fg-0)]"
                    >
                      视频任务
                    </h2>
                    <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                      {activeVideoTaskSummary(
                        activeItems.length,
                        historyCounts.all,
                      )}
                    </p>
                  </div>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <IconButton
                  variant="ghost"
                  size="sm"
                  className="h-11 w-11"
                  aria-label="刷新视频任务"
                  tooltip="刷新"
                  onClick={onRefresh}
                >
                  <RefreshCw className="h-4 w-4" />
                </IconButton>
                <IconButton
                  autoFocus
                  variant="ghost"
                  size="sm"
                  className="h-11 w-11"
                  aria-label="关闭视频任务"
                  tooltip="关闭"
                  onClick={onClose}
                >
                  <X className="h-4 w-4" />
                </IconButton>
              </div>
            </header>

            <div className="mobile-dialog-scroll min-h-0 flex-1 space-y-5 overflow-y-auto p-3 pb-[calc(var(--mobile-dialog-footer-pad-bottom)+0.75rem)] sm:p-4">
              <ActiveVideoTaskSection
                items={activeItems}
                cancelPendingId={cancelPendingId}
                retryDisabled={retryDisabled}
                onCancel={onCancel}
                onRetry={onRetry}
                onCopy={onCopy}
                onUseDraft={onUseDraft}
              />
              {historyError && <ErrorState title="任务记录加载失败" description={historyError} onRetry={onRefresh} />}
              <VideoTaskHistorySection
                error={Boolean(historyError)}
                items={historyItems}
                activeCount={activeItems.length}
                historyFilter={historyFilter}
                historyCounts={historyCounts}
                loading={historyLoading}
                hasNextPage={historyHasNextPage}
                fetchingNextPage={historyFetchingNextPage}
                retryDisabled={retryDisabled}
                selectedVideoId={selectedVideoId}
                onHistoryFilterChange={onHistoryFilterChange}
                onLoadMore={onLoadMore}
                onCancel={onCancel}
                onRetry={onRetry}
                onCopy={onCopy}
                onUseDraft={onUseDraft}
                onDelete={onDelete}
                onPreview={onPreview}
              />
            </div>
          </motion.section>
        </motion.div>
      )}
    </AnimatePresence>
  );
}


function EmptyPanel({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-h-[132px] flex-col items-center justify-center rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 p-6 text-center">
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]">
        {icon}
      </div>
      <p className="type-body-sm font-medium text-[var(--fg-0)]">{title}</p>
      <p className="mt-1 max-w-sm type-caption leading-5 text-[var(--fg-2)]">{description}</p>
    </div>
  );
}

export function VideoPreviewDialog({
  item,
  onClose,
  onUseDraft,
  onRetry,
  onCopy,
  onDelete,
}: VideoPreviewDialogProps) {
  /*
   * Stable preview contracts implemented by video-task-preview.tsx:
   * mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto
   * mobile-dialog-footer landscape:max-sm
   * activeVideoTemporaryDownload(item, nowMs)
   * setNowMs(Date.now())
   * target={isTemporary ? "_blank" : undefined}
   * {isTemporary ? "快速下载" : "下载"}
   */
  return (
    <VideoPreviewDialogContent
      item={item}
      onClose={onClose}
      onUseDraft={onUseDraft}
      onRetry={onRetry}
      onCopy={onCopy}
      onDelete={onDelete}
    />
  );
}

function HistoryFilterTabs({
  value,
  counts,
  loading,
  onChange,
}: {
  value: VideoHistoryFilter;
  counts: Record<VideoHistoryFilter, number>;
  loading: boolean;
  onChange: (value: VideoHistoryFilter) => void;
}) {
  const filters: Array<{ value: VideoHistoryFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "succeeded", label: "成功" },
    { value: "failed", label: "失败" },
  ];

  return (
    <div className="grid grid-cols-3 gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
      {filters.map((filter) => {
        const active = filter.value === value;
        return (
          <button
            key={filter.value}
            type="button"
            onClick={() => onChange(filter.value)}
            className={cn(
              "min-h-11 rounded-[var(--radius-control)] px-2 type-caption transition-colors sm:min-h-8",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              active
                ? "bg-[var(--bg-2)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-1)]",
            )}
          >
            <span className="inline-flex min-w-0 items-center justify-center gap-1.5">
              <span>{filter.label}</span>
              <span className="rounded-full border border-[var(--border)] px-1.5 py-0.5 font-mono type-caption tabular-nums">
                {loading ? "..." : counts[filter.value]}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function TaskErrorDetails({
  raw,
  summary,
}: {
  raw: string;
  summary: string;
}) {
  return (
    <details className="group mt-2 overflow-hidden rounded-[var(--radius-control)] border border-danger-border bg-danger-soft">
      <summary className="flex cursor-pointer list-none items-start gap-2 px-2.5 py-2 type-caption leading-5 text-[var(--danger-fg)]">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 flex-1">{summary}</span>
        <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-danger-border px-2.5 py-2">
        <p className="type-caption text-[var(--danger-fg)]">技术详情</p>
        <pre className="mt-1.5 max-h-36 overflow-auto whitespace-pre-wrap break-all rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-2 font-mono type-caption leading-4 text-[var(--fg-1)]">
          {raw}
        </pre>
      </div>
    </details>
  );
}

function TaskRowActions({
  cancelPending,
  item,
  active,
  retryable,
  retryDisabled,
  videoItem,
  selected,
  showPreview,
  canDownload,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
}: {
  item: VideoGenerationOut;
  cancelPending: boolean;
  active: boolean;
  retryable: boolean;
  retryDisabled: boolean;
  videoItem: VideoGenerationWithVideo | null;
  selected: boolean;
  showPreview: boolean;
  canDownload: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 [&_button]:min-h-11 sm:[&_button]:min-h-9">
      {active && (
        <Button
          variant="outline"
          size="sm"
          onClick={onCancel}
          disabled={cancelPending || Boolean(item.cancel_requested_at)}
          leftIcon={<XCircle className="h-3.5 w-3.5" />}
        >
          {cancelPending ? "请求中" : item.cancel_requested_at ? "已请求取消" : "取消"}
        </Button>
      )}
      {retryable && (
        <Button
          variant="outline"
          size="sm"
          disabled={retryDisabled}
          loading={retryDisabled}
          onClick={onRetry}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          重新生成
        </Button>
      )}
      {!showPreview && videoItem && onPreview && (
        <Button
          variant={selected ? "secondary" : "outline"}
          size="sm"
          onClick={onPreview}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          预览
        </Button>
      )}
      {canDownload && <VideoDownloadLink item={item} />}
      {onUseDraft && (
        <Button
          variant="outline"
          size="sm"
          onClick={onUseDraft}
          leftIcon={<RotateCw className="h-3.5 w-3.5" />}
        >
          套用参数
        </Button>
      )}
      <div className="ml-auto flex items-center gap-1 [&_button]:h-11 [&_button]:w-11 sm:[&_button]:h-9 sm:[&_button]:w-9">
        <IconButton
          variant="ghost"
          size="sm"
          onClick={onCopy}
          aria-label="复制视频描述"
          tooltip="复制描述"
        >
          <Copy className="h-3.5 w-3.5" />
        </IconButton>
        {onDelete && videoItem && (
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onDelete}
            aria-label="删除视频"
            tooltip="删除"
            className="text-[var(--danger-fg)] hover:text-[var(--danger-fg)]"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </IconButton>
        )}
      </div>
    </div>
  );
}

function taskRowVideoItem(
  item: VideoGenerationOut,
): VideoGenerationWithVideo | null {
  return hasVideo(item) ? item : null;
}

function taskRowCanDownload(
  item: VideoGenerationOut,
  videoItem: VideoGenerationWithVideo | null,
): boolean {
  if (videoItem) return true;
  return activeVideoTemporaryDownload(item) != null;
}

function taskRowContainerClass(active: boolean, selected: boolean): string {
  return active || selected
    ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
    : "border-[var(--border-subtle)] bg-[var(--bg-0)]/60";
}

function TaskRowPreview({
  item,
  selected,
  showPreview,
  onPreview,
}: {
  item: VideoGenerationWithVideo | null;
  selected: boolean;
  showPreview: boolean;
  onPreview?: () => void;
}) {
  if (!showPreview || !item || !onPreview) return null;
  return (
    <VideoPosterButton
      item={item}
      selected={selected}
      onPreview={onPreview}
    />
  );
}

function TaskRowError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <TaskErrorDetails raw={message} summary={taskErrorSummary(message)} />
  );
}

function TaskRow({
  cancelPending = false,
  item,
  onCancel,
  onRetry,
  retryDisabled = false,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
  selected = false,
  showPreview = true,
}: {
  item: VideoGenerationOut;
  cancelPending?: boolean;
  onCancel: () => void;
  onRetry: () => void;
  retryDisabled?: boolean;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
  selected?: boolean;
  showPreview?: boolean;
}) {
  const active = isActiveVideo(item);
  const copy = stageCopy(item);
  const videoItem = taskRowVideoItem(item);
  const retryable = isFailedHistoryVideo(item);
  const canDownload = taskRowCanDownload(item, videoItem);
  const elapsedLabel = taskElapsedLabel(item);
  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border p-3 transition-colors hover:border-[var(--border)]",
        taskRowContainerClass(active, selected),
      )}
    >
      {(active || selected) && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 type-caption text-[var(--fg-2)]">
            <span className="font-medium text-[var(--fg-1)]">{item.model}</span>
            <span>{actionLabel(item.action)}</span>
            <span>{item.resolution}</span>
            <span>{formatDurationLabel(item.duration_s)}</span>
            {elapsedLabel && <span>{elapsedLabel}</span>}
          </div>
          <p className="mt-1 line-clamp-2 type-body-sm text-[var(--fg-0)]">{item.prompt}</p>
          <p className="mt-1 type-caption leading-5 text-[var(--fg-2)]">{copy.detail}</p>
        </div>
        <StatusPill item={item} />
      </div>
      <TaskRowPreview
        item={videoItem}
        selected={selected}
        showPreview={showPreview}
        onPreview={onPreview}
      />
      <TaskRowError message={item.error_message ?? null} />
      <TaskRowActions
        cancelPending={cancelPending}
        item={item}
        active={active}
        retryable={retryable}
        retryDisabled={retryDisabled}
        videoItem={videoItem}
        selected={selected}
        showPreview={showPreview}
        canDownload={canDownload}
        onCancel={onCancel}
        onRetry={onRetry}
        onCopy={onCopy}
        onUseDraft={onUseDraft}
        onDelete={onDelete}
        onPreview={onPreview}
      />
    </article>
  );
}

function StatusPill({ item }: { item: VideoGenerationOut }) {
  const terminalOk = item.status === "succeeded" && hasVideo(item);
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  const copy = stageCopy(item);
  return (
    <span
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={[
        "rounded-full border px-2 py-1 type-caption",
        terminalOk
          ? "border-success-border bg-success-soft text-success"
          : terminalBad
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      {copy.label}
    </span>
  );
}
