import { Maximize2, PlayCircle, RefreshCw } from "lucide-react";
import {
  useState,
  type CSSProperties,
  type Dispatch,
  type SetStateAction,
} from "react";

import type { LightboxItem } from "@/components/ui/lightbox/types";
import {
  imageBinaryUrl,
  imageVariantUrl,
  videoBinaryUrl,
} from "@/lib/apiClient";
import type { CanvasOutput } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/store/useUiStore";
import { CanvasOutputDownloadButton } from "../CanvasOutputDownloadButton";
import { CanvasVideoPreviewDialog } from "../CanvasVideoPreviewDialog";

export interface NormalizedCanvasCrop {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface OutputPreviewProps {
  output: CanvasOutput;
  alt: string;
  crop?: NormalizedCanvasCrop | null;
  large?: boolean;
}

export function OutputPreview(props: OutputPreviewProps) {
  // A replacement source resets failures, retry attempts, natural size and playback.
  const { output } = props;
  const identity = JSON.stringify([output.type, output.image_id, output.video_id,
    output.url, output.preview_url, output.poster_url, output.thumbnail_url,
    output.source_sha256, output.preparation_revision, output.preparation_state]);
  return <CanvasOutputPreview key={identity} {...props} />;
}

function CanvasOutputPreview({
  output,
  alt,
  crop = null,
  large = false,
}: OutputPreviewProps) {
  const media = useOutputPreviewMedia(output, large);
  const [videoPreviewOpen, setVideoPreviewOpen] = useState(false);
  const width = outputDimension(output.width);
  const height = outputDimension(output.height);
  const [naturalSize, setNaturalSize] = useState<{
    src: string;
    width: number;
    height: number;
  } | null>(null);
  const natural = matchingNaturalSize(media.visibleSrc, naturalSize);
  const previewWidth = width ?? natural?.width;
  const previewHeight = height ?? natural?.height;
  const cropStyle = outputCropStyle(
    output.type,
    crop,
    previewWidth,
    previewHeight,
  );
  return (
    <>
      <div
        data-canvas-preview-state={media.status}
        data-canvas-asset-preparation={output.preparation_state}
        className={cn(
          "relative w-full overflow-hidden bg-[var(--surface-media)]",
          large ? "min-h-[112px]" : "min-h-16",
        )}
        style={{
          aspectRatio: outputAspectRatio(
            output,
            crop,
            previewWidth,
            previewHeight,
          ),
        }}
      >
        <OutputPreviewButton
          output={output}
          alt={alt}
          media={media}
          width={width}
          height={height}
          cropStyle={cropStyle}
          onNaturalSize={setNaturalSize}
          onOpenVideo={() => setVideoPreviewOpen(true)}
        />
        {media.status === "failed" ? (
          <button
            type="button"
            aria-label="重试预览"
            className="nodrag nopan absolute inset-x-3 top-2 z-[var(--z-header)] mx-auto flex min-h-11 w-fit items-center gap-2 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-3 type-caption text-[var(--media-control-fg)] focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => { event.stopPropagation(); media.retry(); }}
          >
            <RefreshCw className="h-4 w-4" aria-hidden />重试预览
          </button>
        ) : null}
        <OutputTypeBadge type={output.type} />
        <CanvasOutputDownloadButton
          output={output}
          title={alt}
          className="absolute bottom-2 left-2 z-[var(--z-header)]"
        />
      </div>
      {videoPreviewOpen && media.videoSrc ? (
        <CanvasVideoPreviewDialog
          key={media.videoSrc}
          open={videoPreviewOpen}
          output={output}
          src={media.videoSrc}
          poster={media.poster}
          title={alt}
          onClose={() => setVideoPreviewOpen(false)}
        />
      ) : null}
    </>
  );
}

interface OutputPreviewMediaState {
  visibleSrc: string | null;
  videoSrc: string | null;
  poster: string | null;
  status: "loading" | "ready" | "failed" | "unavailable" | "processing" | "preparation_failed";
  attempt: number;
  onLoad: () => void;
  onError: () => void;
  retry: () => void;
}

function useOutputPreviewMedia(output: CanvasOutput, large: boolean): OutputPreviewMediaState {
  const videoSrc = output.type === "video" ? videoPlaybackSource(output) : null;
  const poster = output.type === "video" ? videoPosterSource(output) : null;
  // Browsing thumbnails never creates a video element or requests its binary.
  const preparing = output.preparation_state === "pending" || output.preparation_state === "preparing";
  const preparationFailed = output.preparation_state === "failed";
  const unavailable = output.preparation_state === "unavailable";
  const sources = preparing || preparationFailed || unavailable ? [] : output.type === "video"
    ? uniqueMediaSources([poster])
    : imagePreviewSources(output, large);
  const [index, setIndex] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const visibleSrc = sources[index] ?? null;
  const status = preparing ? "processing" : preparationFailed ? "preparation_failed"
    : sources.length === 0 ? "unavailable"
    : !visibleSrc ? "failed" : loaded ? "ready" : "loading";
  return {
    visibleSrc, videoSrc, poster, status, attempt,
    onLoad: () => setLoaded(true),
    onError: () => { setLoaded(false); setIndex((value) => value + 1); },
    retry: () => { setLoaded(false); setIndex(0); setAttempt((value) => value + 1); },
  };
}

function OutputPreviewButton({
  output,
  alt,
  media,
  width,
  height,
  cropStyle,
  onNaturalSize,
  onOpenVideo,
}: {
  output: CanvasOutput;
  alt: string;
  media: OutputPreviewMediaState;
  width?: number;
  height?: number;
  cropStyle?: CSSProperties;
  onNaturalSize: Dispatch<
    SetStateAction<{ src: string; width: number; height: number } | null>
  >;
  onOpenVideo: () => void;
}) {
  const video = output.type === "video";
  const canOpen = media.status !== "processing" && (video ? Boolean(media.videoSrc) : Boolean(output.url?.trim() || output.image_id));
  return (
    <button
      type="button"
      data-canvas-output-preview
      aria-label={video ? `播放${alt}` : `放大查看${alt}`}
      title={canOpen ? (video ? "播放视频" : "查看大图") : "暂无可用预览"}
      disabled={!canOpen}
      className={cn(
        "nodrag nopan nowheel group block h-full w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]",
        video ? "cursor-pointer" : "cursor-zoom-in",
      )}
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        openCanvasOutputPreview(output, alt, media.videoSrc, onOpenVideo);
      }}
    >
      <OutputPreviewMedia
        type={output.type}
        src={media.visibleSrc}
        status={media.status}
        canPlay={Boolean(media.videoSrc)}
        attempt={media.attempt}
        alt={alt}
        width={width}
        height={height}
        cropStyle={cropStyle}
        onNaturalSize={onNaturalSize}
        onLoad={media.onLoad}
        onError={media.onError}
      />
      {canOpen && media.status !== "failed" ? <OutputPreviewAffordance type={output.type} /> : null}
    </button>
  );
}

function OutputPreviewAffordance({ type }: { type: CanvasOutput["type"] }) {
  if (type === "video") {
    return (
      <span
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-1/2 grid h-11 w-11 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)]"
      >
        <PlayCircle className="h-6 w-6" />
      </span>
    );
  }
  return (
    <span
      aria-hidden
      className="pointer-events-none absolute right-2 top-2 grid h-8 w-8 place-items-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] opacity-0 shadow-[var(--shadow-2)] transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
    >
      <Maximize2 className="h-4 w-4" />
    </span>
  );
}

function openCanvasOutputPreview(
  output: CanvasOutput,
  alt: string,
  videoSrc: string | null,
  onOpenVideo: () => void,
) {
  if (output.type !== "video") {
    openCanvasImagePreview(output, alt);
    return;
  }
  if (videoSrc) onOpenVideo();
}

function matchingNaturalSize(
  src: string | null,
  naturalSize: { src: string; width: number; height: number } | null,
) {
  return src && naturalSize?.src === src ? naturalSize : null;
}

function outputCropStyle(
  type: CanvasOutput["type"],
  crop: NormalizedCanvasCrop | null,
  width?: number,
  height?: number,
): CSSProperties | undefined {
  if (!crop || type !== "image" || !width || !height) return undefined;
  return {
    height: `${100 / crop.height}%`,
    left: `${(-crop.x / crop.width) * 100}%`,
    maxWidth: "none",
    position: "absolute",
    top: `${(-crop.y / crop.height) * 100}%`,
    width: `${100 / crop.width}%`,
  };
}

function OutputPreviewMedia({
  type, src, status, canPlay, attempt, alt, width, height, cropStyle, onNaturalSize, onLoad, onError,
}: {
  type: CanvasOutput["type"];
  src: string | null;
  status: OutputPreviewMediaState["status"];
  canPlay: boolean;
  attempt: number;
  alt: string;
  width?: number;
  height?: number;
  cropStyle?: CSSProperties;
  onNaturalSize: (size: { src: string; width: number; height: number }) => void;
  onLoad: () => void;
  onError: () => void;
}) {
  if (!src) {
    return (
      <div className="grid h-full min-h-[112px] place-items-center px-3 pb-10 pt-14 text-center type-caption text-[var(--media-control-fg)]">
        {status === "processing" ? "正在准备素材" : status === "preparation_failed" ? "素材检查失败，可查看原文件或重新选择"
          : status === "failed" ? "预览暂时载入失败" : type === "video" && canPlay ? "暂无海报，点击播放视频" : "暂无可用预览"}
      </div>
    );
  }
  return (
    <>
      {status === "loading" ? (
        <span className="pointer-events-none absolute inset-x-2 bottom-12 text-center type-caption text-[var(--media-control-fg)]" role="status">
          正在载入预览
        </span>
      ) : null}
      {/* API-backed signed images/posters stay separate from click-to-play video. */}
      {/* eslint-disable-next-line @next/next/no-img-element -- Signed API media. */}
      <img
        key={`${src}:${attempt}`}
        src={src}
        alt={alt}
        width={width}
        height={height}
        loading="lazy"
        decoding="async"
        className={cn(!cropStyle && "h-full w-full object-contain")}
        style={cropStyle}
        onLoad={(event) => {
          onLoad();
          if (width && height) return;
          const image = event.currentTarget;
          if (image.naturalWidth <= 0 || image.naturalHeight <= 0) return;
          onNaturalSize({ src, width: image.naturalWidth, height: image.naturalHeight });
        }}
        onError={onError}
        draggable={false}
      />
    </>
  );
}

function OutputTypeBadge({ type }: { type: CanvasOutput["type"] }) {
  if (type !== "video") return null;
  return (
    <span className="pointer-events-none absolute bottom-1 right-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-0.5 type-caption text-[var(--media-control-fg)]">
      视频
    </span>
  );
}

function imagePreviewSources(output: CanvasOutput, large: boolean): string[] {
  return uniqueMediaSources([
    !large ? output.thumbnail_url : null,
    output.preview_url,
    output.image_id
      ? imageVariantUrl(output.image_id, "display2048")
      : null,
    output.url,
    output.image_id ? imageBinaryUrl(output.image_id) : null,
  ]);
}

function videoPlaybackSource(output: CanvasOutput): string | null {
  return (
    output.url?.trim() ||
    (output.video_id ? videoBinaryUrl(output.video_id) : null) ||
    null
  );
}

function videoPosterSource(output: CanvasOutput): string | null {
  return output.poster_url?.trim() || output.preview_url?.trim() || null;
}

function uniqueMediaSources(
  sources: Array<string | null | undefined>,
): string[] {
  return Array.from(
    new Set(
      sources
        .map((source) => source?.trim() ?? "")
        .filter((source) => source.length > 0),
    ),
  );
}

function openCanvasImagePreview(output: CanvasOutput, alt: string) {
  const item = canvasImageLightboxItem(output, alt);
  if (!item) return;
  useUiStore.getState().openLightboxFromItems([item], item.id);
}

function canvasImageLightboxItem(
  output: CanvasOutput,
  alt: string,
): LightboxItem | null {
  const imageId = mediaText(output.image_id);
  const originalUrl = mediaText(output.url) || imageBinarySource(imageId);
  if (!originalUrl) return null;
  const id =
    imageId ||
    mediaText(output.generation_id) ||
    `canvas-image-${originalUrl}`;
  const item: LightboxItem = {
    id,
    url: originalUrl,
    previewUrl:
      mediaText(output.preview_url) ||
      imageDisplaySource(imageId) ||
      originalUrl,
    thumbUrl: imageDisplaySource(imageId) || originalUrl,
    prompt: mediaText(output.label) || alt,
    width: outputDimension(output.width),
    height: outputDimension(output.height),
    generation_id: output.generation_id ?? null,
    source: "canvas",
    source_type: "canvas_output",
  };
  return item;
}

function mediaText(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

function imageBinarySource(imageId: string | null): string | null {
  return imageId ? imageBinaryUrl(imageId) : null;
}

function imageDisplaySource(imageId: string | null): string | null {
  return imageId ? imageVariantUrl(imageId, "display2048") : null;
}

function outputAspectRatio(
  output: CanvasOutput,
  crop?: NormalizedCanvasCrop | null,
  resolvedWidth?: number,
  resolvedHeight?: number,
): string {
  const width = resolvedWidth ?? outputDimension(output.width);
  const height = resolvedHeight ?? outputDimension(output.height);
  if (width && height) {
    return crop && output.type === "image"
      ? `${width * crop.width} / ${height * crop.height}`
      : `${width} / ${height}`;
  }
  return output.type === "video" ? "16 / 9" : "1 / 1";
}

function outputDimension(value: number | null | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : undefined;
}

export function normalizedCanvasCrop(
  value: unknown,
): NormalizedCanvasCrop | null {
  if (!value || typeof value !== "object") return null;
  const crop = value as Record<string, unknown>;
  const x = Number(crop.x);
  const y = Number(crop.y);
  const width = Number(crop.width);
  const height = Number(crop.height);
  if (
    ![x, y, width, height].every(Number.isFinite) ||
    x < 0 ||
    y < 0 ||
    width <= 0 ||
    height <= 0 ||
    x + width > 1 ||
    y + height > 1
  ) {
    return null;
  }
  return { x, y, width, height };
}
