"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Copy,
  Download,
  Film,
  Play,
  RefreshCw,
  RotateCw,
  Trash2,
  XCircle,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { prewarmImage, prewarmVideoMetadata } from "@/features/assets";
import { videoBinaryUrl, videoDownloadUrl } from "@/lib/apiClient";
import type { VideoGenerationOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { activeVideoTemporaryDownload } from "@/lib/videoEventSnapshot";

import {
  actionLabel,
  formatDurationLabel,
  hasVideo,
  isFailedHistoryVideo,
  progressForItem,
  stageCopy,
  taskElapsedLabel,
} from "./video-task-model";
import type { VideoGenerationWithVideo } from "./video-task-model";
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

export function prewarmVideoPreviewItem(
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

export function VideoDownloadLink({
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
  const expiresTitle = isTemporary
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
        "inline-flex h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-transparent px-3 type-caption font-medium leading-tight text-[var(--fg-0)] transition-[background-color,border-color,color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] sm:h-9",
        fullWidth && "w-full",
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0" />
      {isTemporary ? "快速下载" : "下载"}
    </a>
  );
}

export function VideoPosterButton({
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
      <span className="absolute inset-0 flex items-center justify-center bg-transparent transition-colors group-hover:bg-[var(--surface-scrim)]">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--fg-0)]/85 px-3 py-1.5 type-caption font-medium text-[var(--bg-0)] shadow-[var(--shadow-2)]">
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
            className="inline-flex items-center gap-2 rounded-full border border-[var(--border-strong)] bg-[var(--bg-0)]/90 px-3 py-1.5 type-caption font-medium text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-md"
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

function StatusPill({ item }: { item: VideoGenerationOut }) {
  const terminalOk = item.status === "succeeded";
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  const copy = stageCopy(item);
  return (
    <span
      className={[
        "rounded-full border px-2 py-1 type-caption",
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

export type VideoPreviewDialogProps = {
  item: VideoGenerationWithVideo;
  onClose: () => void;
  onUseDraft: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onDelete: () => void;
};

export function VideoPreviewDialogContent({
  item,
  onClose,
  onUseDraft,
  onRetry,
  onCopy,
  onDelete,
}: VideoPreviewDialogProps) {
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
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--surface-scrim)] backdrop-blur-md sm:items-center sm:p-5"
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
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-6xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(900px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b landscape:max-sm:rounded-[var(--radius-panel)] landscape:max-sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap gap-2">
              <StatusPill item={item} />
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 type-caption text-[var(--fg-2)]">
                {actionLabel(item.action)} · {item.resolution} ·{" "}
                {formatDurationLabel(item.duration_s)}
              </span>
              {elapsedLabel && (
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 type-caption text-[var(--fg-2)]">
                  {elapsedLabel}
                </span>
              )}
            </div>
            <h2
              id={`video-preview-${item.id}`}
              className="truncate type-body font-semibold text-[var(--fg-0)]"
            >
              视频播放
            </h2>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-11 shrink-0 px-0"
            onClick={onClose}
            aria-label="关闭视频播放"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
          <div className="flex min-h-full flex-col gap-3 lg:grid lg:h-full lg:grid-cols-[minmax(0,1fr)_minmax(280px,340px)]">
            <div className="min-h-[180px] flex-1 landscape:max-sm:min-h-[140px] lg:h-full lg:min-h-0">
              <PrimaryVideoPlayer
                item={item}
                className="h-full min-h-[180px] landscape:max-sm:min-h-[140px] lg:min-h-0"
              />
            </div>
            <aside className="shrink-0 overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/64 p-3 shadow-[var(--shadow-1)] lg:h-full lg:max-h-none">
              <p className="type-caption text-[var(--fg-2)]">提示词</p>
              <p className="mt-2 type-body-sm leading-6 text-[var(--fg-0)]">
                {item.prompt}
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5 type-caption text-[var(--fg-2)]">
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
        <footer className="mobile-dialog-footer flex shrink-0 flex-nowrap items-center gap-2 overflow-x-auto border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 [scrollbar-width:none] sm:flex-wrap sm:justify-between sm:overflow-visible sm:px-5">
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
