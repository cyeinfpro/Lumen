"use client";

import { useRef, useState } from "react";
import { Clapperboard, Maximize2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { videoBinaryUrl } from "@/lib/apiClient";
import type { VideoAction } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  actionLabel,
  directorViewportFallback,
  formatDurationLabel,
} from "./video-task-model";
import type { VideoGenerationWithVideo } from "./video-task-model";

// @ui-governance-allow media -- The fixed dark video stage needs inverse text.
const DIRECTOR_STAGE_TEXT = "text-white";
// @ui-governance-allow media -- The fixed dark video stage needs inverse text.
const DIRECTOR_STAGE_TEXT_MUTED = "text-white/65";
// @ui-governance-allow media -- The fixed dark video stage needs inverse text.
const DIRECTOR_STAGE_TEXT_FAINT = "text-white/55";

function directorVideoSrc(item: VideoGenerationWithVideo): string {
  return item.video.url?.trim() || videoBinaryUrl(item.video.id);
}

function directorVideoPoster(
  item: VideoGenerationWithVideo,
): string | undefined {
  return item.video.poster_url?.trim() || undefined;
}

export function VideoDirectorViewport({
  item,
  loading,
  action,
  prompt,
  sourceReady,
  onPreview,
}: {
  item: VideoGenerationWithVideo | null;
  loading: boolean;
  action: VideoAction;
  prompt: string;
  sourceReady: boolean;
  onPreview: (item: VideoGenerationWithVideo) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [failedVideoId, setFailedVideoId] = useState<string | null>(null);
  const failed = item != null && failedVideoId === item.video.id;
  const fallback = directorViewportFallback(
    action,
    sourceReady,
    prompt,
    loading,
  );
  const summary = item
    ? `${actionLabel(item.action)} · ${item.resolution} · ${item.aspect_ratio} · ${formatDurationLabel(item.duration_s)}`
    : "固定 16:9 监看画布";

  const retryVideo = () => {
    if (!item) return;
    setFailedVideoId(null);
    window.requestAnimationFrame(() => videoRef.current?.load());
  };

  return (
    <section className="min-w-0" aria-labelledby="director-viewport-title">
      <header className="mb-2 flex min-w-0 flex-col gap-2 px-1 min-[420px]:flex-row min-[420px]:items-center min-[420px]:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Clapperboard
              className="h-4 w-4 shrink-0 text-[var(--accent)]"
              aria-hidden="true"
            />
            <h2
              id="director-viewport-title"
              className="type-body-sm font-semibold text-[var(--fg-0)]"
            >
              导演视口
            </h2>
          </div>
          <p className="mt-0.5 break-words type-caption text-[var(--fg-2)]">
            {item ? `最近成片 · ${summary}` : summary}
          </p>
        </div>
        {item && (
          <Button
            variant="outline"
            size="sm"
            className="shrink-0 self-start min-[420px]:self-center"
            leftIcon={<Maximize2 className="h-3.5 w-3.5" />}
            onClick={() => {
              videoRef.current?.pause();
              onPreview(item);
            }}
          >
            放大预览
          </Button>
        )}
      </header>

      <div className="relative aspect-video w-full overflow-hidden bg-[var(--surface-media)] shadow-[var(--shadow-1)]">
        {item && (
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
            className={cn(
              "absolute inset-0 h-full w-full object-contain",
              failed && "invisible",
            )}
          >
            当前浏览器不支持视频预览。
          </video>
        )}

        {!item && (
          <div
            role="status"
            aria-live="polite"
            className={cn(
              "absolute inset-0 flex flex-col items-center justify-center gap-2 px-5 text-center",
              DIRECTOR_STAGE_TEXT,
            )}
          >
            <Clapperboard
              className={cn(
                "h-8 w-8",
                fallback.kind === "source"
                  ? "text-[var(--accent)]"
                  : DIRECTOR_STAGE_TEXT_FAINT,
              )}
              aria-hidden="true"
            />
            <p
              className={cn(
                "type-body-sm font-semibold",
                DIRECTOR_STAGE_TEXT,
              )}
            >
              {fallback.title}
            </p>
            <p
              className={cn(
                "max-w-md break-words type-caption leading-5",
                DIRECTOR_STAGE_TEXT_MUTED,
              )}
            >
              {fallback.description}
            </p>
          </div>
        )}

        {item && failed && (
          <div
            role="alert"
            className={cn(
              "absolute inset-0 flex flex-col items-center justify-center gap-3 px-5 text-center",
              DIRECTOR_STAGE_TEXT,
            )}
          >
            <div>
              <p
                className={cn(
                  "type-body-sm font-semibold",
                  DIRECTOR_STAGE_TEXT,
                )}
              >
                成片预览加载失败
              </p>
              <p
                className={cn(
                  "mt-1 break-words type-caption",
                  DIRECTOR_STAGE_TEXT_MUTED,
                )}
              >
                视频记录仍保留，可重试或打开完整预览。
              </p>
            </div>
            <Button
              variant="glass"
              size="sm"
              onClick={retryVideo}
              leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
            >
              重试
            </Button>
          </div>
        )}
      </div>
    </section>
  );
}
