"use client";

import { useRef, useState } from "react";
import { Clapperboard, Maximize2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { videoBinaryUrl } from "@/lib/apiClient";
import type { VideoAction } from "@/lib/types";
import { cn } from "@/lib/utils";
import { actionLabel, directorViewportFallback, formatDurationLabel } from "./video-task-model";
import type { VideoGenerationWithVideo } from "./video-task-model";

// @ui-governance-allow media -- Actual video playback uses a fixed dark stage.
const DIRECTOR_STAGE_TEXT = "text-white";
// @ui-governance-allow media -- Inverse help text is limited to the video error scrim.
const DIRECTOR_STAGE_TEXT_MUTED = "text-white/65";

function directorVideoSrc(item: VideoGenerationWithVideo): string {
  return item.video.url?.trim() || videoBinaryUrl(item.video.id);
}

function directorVideoPoster(item: VideoGenerationWithVideo): string | undefined {
  return item.video.poster_url?.trim() || undefined;
}

export function VideoDirectorViewport({
  item,
  loading,
  error,
  onRetry,
  action,
  prompt,
  sourceReady,
  onPreview,
}: {
  item: VideoGenerationWithVideo | null;
  loading: boolean;
  error?: string | null;
  onRetry: () => void;
  action: VideoAction;
  prompt: string;
  sourceReady: boolean;
  onPreview: (item: VideoGenerationWithVideo) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [failedVideoId, setFailedVideoId] = useState<string | null>(null);
  const failed = item != null && failedVideoId === item.video.id;
  const fallback = directorViewportFallback(action, sourceReady, prompt, loading);

  // An empty stage is guidance, not media. Do not reserve a cinema-sized black box.
  if (!item) {
    return (
      <section data-video-preview="empty" aria-labelledby="director-viewport-title" className="min-w-0 rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-4 py-4">
        <div className="flex items-center gap-2">
          <Clapperboard className="h-4 w-4 shrink-0 text-[var(--fg-muted-aa)]" aria-hidden />
          <h2 id="director-viewport-title" className="type-body-sm font-medium text-[var(--fg-0)]">成片预览</h2>
        </div>
        <div role={error ? "alert" : "status"} aria-live="polite" className="mt-2 space-y-1">
          <p className="type-body-sm text-[var(--fg-1)]">{error ? "任务记录加载失败" : fallback.title}</p>
          <p className="max-w-lg text-pretty type-caption leading-5 text-[var(--fg-muted-aa)]">{error ?? fallback.description}</p>
        </div>
        {error && <Button variant="outline" size="sm" className="mt-3" onClick={onRetry} leftIcon={<RefreshCw className="h-3.5 w-3.5" aria-hidden />}>重试</Button>}
      </section>
    );
  }

  const summary = `${actionLabel(item.action)} · ${item.resolution} · ${item.aspect_ratio} · ${formatDurationLabel(item.duration_s)}`;
  const retryVideo = () => {
    setFailedVideoId(null);
    window.requestAnimationFrame(() => videoRef.current?.load());
  };

  return (
    <section data-video-preview="ready" className="min-w-0" aria-labelledby="director-viewport-title">
      <header className="mb-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 id="director-viewport-title" className="type-body-sm font-medium text-[var(--fg-0)]">成片预览</h2>
          <p className="mt-0.5 break-words type-caption text-[var(--fg-muted-aa)]">最近成片 · {summary}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          leftIcon={<Maximize2 className="h-3.5 w-3.5" aria-hidden />}
          onClick={() => {
            videoRef.current?.pause();
            onPreview(item);
          }}
        >
          放大预览
        </Button>
      </header>
      <div className="relative aspect-video max-h-[min(50dvh,28rem)] w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--surface-media)]">
        <video
          key={item.video.id}
          ref={videoRef}
          src={directorVideoSrc(item)}
          poster={directorVideoPoster(item)}
          controls
          playsInline
          preload="metadata"
          aria-label="导演视口最近成片"
          onLoadedMetadata={() => setFailedVideoId(null)}
          onError={() => setFailedVideoId(item.video.id)}
          className={cn("absolute inset-0 h-full w-full object-contain", failed && "invisible")}
        >
          当前浏览器不支持视频预览。
        </video>
        {failed && (
          <div role="alert" className={cn("absolute inset-0 flex flex-col items-center justify-center gap-3 px-5 text-center", DIRECTOR_STAGE_TEXT)}>
            <div>
              <p className={cn("type-body-sm font-semibold", DIRECTOR_STAGE_TEXT)}>成片预览加载失败</p>
              <p className={cn("mt-1 break-words type-caption", DIRECTOR_STAGE_TEXT_MUTED)}>视频记录仍保留，可重试或打开完整预览。</p>
            </div>
            <Button variant="glass" size="sm" onClick={retryVideo} leftIcon={<RefreshCw className="h-3.5 w-3.5" aria-hidden />}>重试</Button>
          </div>
        )}
      </div>
    </section>
  );
}
