"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertCircle,
  ChevronDown,
  Copy,
  Download,
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

import { Button, IconButton } from "@/components/ui/primitives";
import { videoBinaryUrl, videoDownloadUrl } from "@/lib/apiClient";
import { prewarmImage, prewarmVideoMetadata } from "@/lib/imagePreload";
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
  progressForItem,
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
  focusVideoWorkbenchElement,
  isTopmostVideoDialog,
  restoreVideoWorkbenchFocus,
  trapVideoDialogFocus,
} from "./video-workbench-ui";

function videoSrc(video: VideoGenerationWithVideo["video"]): string {
  return video.url?.trim() || videoBinaryUrl(video.id);
}

function posterSrc(
  video: VideoGenerationWithVideo["video"],
): string | undefined {
  return video.poster_url?.trim() || undefined;
}

export function prewarmVideoItem(
  item: VideoGenerationWithVideo | null | undefined,
): void {
  if (!item) return;
  prewarmImage(posterSrc(item.video));
  prewarmVideoMetadata(videoSrc(item.video));
}

function videoDownloadName(item: VideoGenerationOut): string {
  const ext =
    hasVideo(item) && item.video.mime === "video/quicktime" ? "mov" : "mp4";
  return `lumen-video-${item.id.slice(0, 8)}.${ext}`;
}

function ActiveVideoTaskSection({
  items,
  retryDisabled,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
}: {
  items: VideoGenerationOut[];
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
        <p className="type-caption text-[var(--fg-2)]">正在进行</p>
        <span className="text-xs tabular-nums text-[var(--fg-2)]">
          {items.length} 条
        </span>
      </div>
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
            showPreview={false}
          />
        ))}
      </div>
    </section>
  );
}

function VideoTaskHistorySection({
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
        <span className="text-xs tabular-nums text-[var(--fg-2)]">
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
        {items.length === 0 && (
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
            className="mobile-dialog-panel ml-auto flex h-full w-full max-w-[460px] flex-col overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)]"
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
                      className="text-sm font-semibold text-[var(--fg-0)]"
                    >
                      视频任务
                    </h2>
                    <p className="mt-0.5 text-xs text-[var(--fg-2)]">
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
                  aria-label="关闭视频任务"
                  tooltip="关闭"
                  onClick={onClose}
                >
                  <X className="h-4 w-4" />
                </IconButton>
              </div>
            </header>

            <div className="mobile-dialog-scroll min-h-0 flex-1 space-y-5 overflow-y-auto p-3 sm:p-4">
              <ActiveVideoTaskSection
                items={activeItems}
                retryDisabled={retryDisabled}
                onCancel={onCancel}
                onRetry={onRetry}
                onCopy={onCopy}
                onUseDraft={onUseDraft}
              />
              <VideoTaskHistorySection
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
      <p className="text-sm font-medium text-[var(--fg-0)]">{title}</p>
      <p className="mt-1 max-w-sm text-xs leading-5 text-[var(--fg-2)]">{description}</p>
    </div>
  );
}

function useActiveVideoTemporaryDownload(item: VideoGenerationOut) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const temporaryUrl = item.temporary_download?.url ?? "";
  const temporaryExpiresAt = item.temporary_download?.expires_at ?? "";
  useEffect(() => {
    const expiresAtMs = Date.parse(temporaryExpiresAt);
    if (!temporaryUrl || !Number.isFinite(expiresAtMs)) return;
    const delayMs = Math.max(0, expiresAtMs - Date.now() - 30_000 + 50);
    const timer = window.setTimeout(
      () => setNowMs(Date.now()),
      Math.min(delayMs, 2_147_483_647),
    );
    return () => window.clearTimeout(timer);
  }, [temporaryExpiresAt, temporaryUrl]);
  return activeVideoTemporaryDownload(item, nowMs);
}

function VideoDownloadLink({
  item,
  fullWidth = false,
}: {
  item: VideoGenerationOut;
  fullWidth?: boolean;
}) {
  const temporaryDownload = useActiveVideoTemporaryDownload(item);
  const stableHref = hasVideo(item) ? videoDownloadUrl(item.video.id) : "";
  const href = temporaryDownload?.url || stableHref;
  if (!href) return null;
  const isTemporary = temporaryDownload != null;
  const expiresTitle =
    isTemporary
      ? `火山临时链接，约 ${Math.max(1, Math.floor(temporaryDownload.expires_in_s / 60))} 分钟后过期`
      : undefined;
  return (
    <a
      href={href}
      download={isTemporary ? undefined : videoDownloadName(item)}
      target={isTemporary ? "_blank" : undefined}
      rel={isTemporary ? "noopener noreferrer" : undefined}
      title={expiresTitle}
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-transparent px-3 text-xs font-medium leading-tight text-[var(--fg-0)] transition-[background-color,border-color,color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
        fullWidth && "w-full",
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0" />
      {isTemporary ? "快速下载" : "下载"}
    </a>
  );
}

function VideoPosterButton({
  item,
  onPreview,
  selected = false,
  compact = false,
}: {
  item: VideoGenerationWithVideo;
  onPreview: () => void;
  selected?: boolean;
  compact?: boolean;
}) {
  const [posterFailure, setPosterFailure] = useState<{
    videoId: string;
    failed: boolean;
  } | null>(null);
  const poster = posterSrc(item.video);
  const videoUrl = videoSrc(item.video);
  const posterFailed =
    posterFailure?.videoId === item.video.id ? posterFailure.failed : false;
  const prewarmPreview = useCallback(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(videoUrl);
  }, [poster, videoUrl]);
  const handlePreview = useCallback(() => {
    prewarmPreview();
    onPreview();
  }, [onPreview, prewarmPreview]);

  useEffect(() => {
    if (selected) prewarmPreview();
  }, [prewarmPreview, selected]);

  return (
    <button
      type="button"
      onClick={handlePreview}
      onFocus={prewarmPreview}
      onPointerDown={prewarmPreview}
      onPointerEnter={prewarmPreview}
      aria-pressed={selected}
      className={cn(
        "group relative w-full overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-0)] text-left transition-colors",
        compact ? "aspect-video" : "mt-3 aspect-video",
        selected
          ? "border-[var(--accent-border)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] hover:border-[var(--border)]",
      )}
    >
      {poster && !posterFailed ? (
        <img
          src={poster}
          alt=""
          loading={selected ? "eager" : "lazy"}
          decoding="async"
          fetchPriority={selected ? "high" : "low"}
          onError={() =>
            setPosterFailure({ videoId: item.video.id, failed: true })
          }
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="grid h-full place-items-center text-[var(--fg-2)]">
          <Film className="h-6 w-6" />
        </div>
      )}
      <span className="absolute inset-0 flex items-center justify-center bg-black/0 transition-colors group-hover:bg-black/20">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--fg-0)]/85 px-3 py-1.5 text-xs font-medium text-[var(--bg-0)] shadow-[var(--shadow-2)]">
          <Play className="h-3.5 w-3.5" />
          播放预览
        </span>
      </span>
    </button>
  );
}

type VideoPlayerStatus = "loading" | "metadata" | "ready" | "buffering" | "error";

function videoPlayerStatusLabel(status: VideoPlayerStatus): string {
  switch (status) {
    case "loading":
      return "读取视频";
    case "metadata":
      return "准备播放";
    case "buffering":
      return "缓冲中";
    case "error":
      return "载入失败";
    default:
      return "";
  }
}

function PrimaryVideoPlayer({
  item,
  className,
}: {
  item: VideoGenerationWithVideo;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [statusState, setStatusState] = useState<{
    videoId: string;
    status: VideoPlayerStatus;
  }>(() => ({ videoId: item.video.id, status: "loading" }));
  const poster = posterSrc(item.video);
  const src = videoSrc(item.video);
  const status =
    statusState.videoId === item.video.id ? statusState.status : "loading";
  const setVideoStatus = useCallback(
    (next: VideoPlayerStatus) =>
      setStatusState({ videoId: item.video.id, status: next }),
    [item.video.id],
  );

  useEffect(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(src);
  }, [poster, src]);

  const retryLoad = useCallback(() => {
    setVideoStatus("loading");
    prewarmImage(poster);
    prewarmVideoMetadata(src);
    videoRef.current?.load();
  }, [poster, setVideoStatus, src]);

  const showState =
    status === "loading" || status === "buffering" || status === "error";

  return (
    <div
      className={cn(
        "relative flex min-h-0 overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border-strong)] bg-[var(--bg-2)] shadow-[var(--shadow-2)]",
        className,
      )}
    >
      <video
        key={item.video.id}
        ref={videoRef}
        controls
        playsInline
        preload="metadata"
        poster={poster}
        src={src}
        onLoadStart={() => setVideoStatus("loading")}
        onLoadedMetadata={() => setVideoStatus("metadata")}
        onCanPlay={() => setVideoStatus("ready")}
        onPlaying={() => setVideoStatus("ready")}
        onWaiting={() => setVideoStatus("buffering")}
        onError={() => setVideoStatus("error")}
        className="h-full min-h-0 w-full bg-[var(--bg-2)] object-contain"
      />
      {showState && (
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center bg-[var(--bg-1)]/70 text-[var(--fg-0)]",
            status !== "error" && "pointer-events-none",
          )}
        >
          <div
            role={status === "error" ? "alert" : "status"}
            aria-live={status === "error" ? "assertive" : "polite"}
            className="inline-flex items-center gap-2 rounded-full border border-[var(--border-strong)] bg-[var(--bg-0)]/90 px-3 py-1.5 text-xs font-medium text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-md"
          >
            {status === "error" ? (
              <button
                type="button"
                onClick={retryLoad}
                className="inline-flex cursor-pointer items-center gap-1.5 text-[var(--fg-0)]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重试
              </button>
            ) : (
              <>
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                {videoPlayerStatusLabel(status)}
              </>
            )}
          </div>
        </div>
      )}
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
}: {
  item: VideoGenerationWithVideo;
  onClose: () => void;
  onUseDraft: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onDelete: () => void;
}) {
  const elapsedLabel = taskElapsedLabel(item);
  const dialogRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const dialog = dialogRef.current;
    const focusFrame = window.requestAnimationFrame(() => {
      focusVideoWorkbenchElement(dialog, { preventScroll: true });
    });
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
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown);
      restoreVideoWorkbenchFocus(previousFocus, dialog);
    };
  }, []);

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`video-preview-${item.id}`}
        tabIndex={-1}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-6xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(900px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap gap-2">
              <StatusPill item={item} />
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
                {actionLabel(item.action)} · {item.resolution} · {formatDurationLabel(item.duration_s)}
              </span>
              {elapsedLabel && (
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
                  {elapsedLabel}
                </span>
              )}
            </div>
            <h2
              id={`video-preview-${item.id}`}
              className="truncate text-base font-semibold text-[var(--fg-0)]"
            >
              视频播放
            </h2>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 w-9 px-0"
            onClick={onClose}
            aria-label="关闭视频播放"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-hidden p-3 sm:p-5">
          <div className="flex h-full min-h-0 flex-col gap-3 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(280px,340px)]">
            <div className="min-h-0 flex-1 lg:h-full">
              <PrimaryVideoPlayer item={item} className="h-full" />
            </div>
            <aside className="max-h-[34%] shrink-0 overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/64 p-3 shadow-[var(--shadow-1)] lg:h-full lg:max-h-none">
              <p className="type-caption text-[var(--fg-2)]">提示词</p>
              <p className="mt-2 text-sm leading-6 text-[var(--fg-0)]">
                {item.prompt}
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5 text-xs text-[var(--fg-2)]">
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.width}x{item.video.height}
                </span>
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {formatDurationLabel(item.duration_s)}
                </span>
                {elapsedLabel && (
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                    {elapsedLabel}
                  </span>
                )}
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.has_audio ? "含音频" : "无音频"}
                </span>
              </div>
            </aside>
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-nowrap items-center gap-2 overflow-x-auto border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 sm:flex-wrap sm:justify-between sm:overflow-visible sm:px-5">
          <VideoDownloadLink item={item} />
          <div className="flex shrink-0 flex-nowrap items-center gap-2 sm:flex-wrap">
            <Button
              variant="secondary"
              size="sm"
              onClick={onUseDraft}
              leftIcon={<RotateCw className="h-3.5 w-3.5" />}
            >
              套用参数
            </Button>
            {isFailedHistoryVideo(item) && (
              <Button
                variant="outline"
                size="sm"
                onClick={onRetry}
                leftIcon={<Play className="h-3.5 w-3.5" />}
              >
                重新生成
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={onCopy}
              leftIcon={<Copy className="h-3.5 w-3.5" />}
            >
              复制
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onDelete}
              leftIcon={<Trash2 className="h-3.5 w-3.5" />}
            >
              删除
            </Button>
          </div>
        </footer>
      </section>
    </div>
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
              "min-h-8 rounded-[var(--radius-control)] px-2 text-xs transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              active
                ? "bg-[var(--bg-2)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-1)]",
            )}
          >
            <span className="inline-flex min-w-0 items-center justify-center gap-1.5">
              <span>{filter.label}</span>
              <span className="rounded-full border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] tabular-nums">
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
      <summary className="flex cursor-pointer list-none items-start gap-2 px-2.5 py-2 text-xs leading-5 text-[var(--danger-fg)]">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 flex-1">{summary}</span>
        <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-danger-border px-2.5 py-2">
        <p className="type-caption text-[var(--danger-fg)]">技术详情</p>
        <pre className="mt-1.5 max-h-36 overflow-auto whitespace-pre-wrap break-all rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-2 font-mono text-[10px] leading-4 text-[var(--fg-1)]">
          {raw}
        </pre>
      </div>
    </details>
  );
}

function TaskRowActions({
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
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {active && (
        <Button
          variant="outline"
          size="sm"
          onClick={onCancel}
          leftIcon={<XCircle className="h-3.5 w-3.5" />}
        >
          取消
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
      <div className="ml-auto flex items-center gap-1">
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

function TaskRow({
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
  const progress = progressForItem(item);
  const progressScale = Math.max(0, Math.min(1, progress / 100));
  const reduceMotion = useReducedMotion();
  const copy = stageCopy(item);
  const videoItem = hasVideo(item) ? item : null;
  const retryable = isFailedHistoryVideo(item);
  const canDownload =
    videoItem != null || activeVideoTemporaryDownload(item) != null;
  const elapsedLabel = taskElapsedLabel(item);
  const errorSummary = item.error_message
    ? taskErrorSummary(item.error_message)
    : null;
  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border p-3 transition-colors hover:border-[var(--border)]",
        active || selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/60",
      )}
    >
      {(active || selected) && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
            <span className="font-medium text-[var(--fg-1)]">{item.model}</span>
            <span>{actionLabel(item.action)}</span>
            <span>{item.resolution}</span>
            <span>{formatDurationLabel(item.duration_s)}</span>
            {elapsedLabel && <span>{elapsedLabel}</span>}
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-[var(--fg-0)]">{item.prompt}</p>
          <p className="mt-1 text-xs leading-5 text-[var(--fg-2)]">{copy.detail}</p>
        </div>
        <StatusPill item={item} />
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <motion.div
          className={cn(
            "h-full w-full origin-left rounded-full",
            active ? "bg-[var(--accent)]" : item.status === "succeeded" ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
          initial={false}
          animate={{ scaleX: progressScale }}
          transition={{ duration: reduceMotion ? 0 : DURATION.normal, ease: EASE.develop }}
        />
      </div>
      {showPreview && videoItem && onPreview && (
        <VideoPosterButton
          item={videoItem}
          selected={selected}
          onPreview={onPreview}
        />
      )}
      {item.error_message && errorSummary && (
        <TaskErrorDetails raw={item.error_message} summary={errorSummary} />
      )}
      <TaskRowActions
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
  const terminalOk = item.status === "succeeded";
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  const copy = stageCopy(item);
  return (
    <span
      className={[
        "rounded-full border px-2 py-1 text-xs",
        terminalOk
          ? "border-success-border bg-success-soft text-success"
          : terminalBad
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      {copy.label} · {Math.round(progressForItem(item))}%
    </span>
  );
}
