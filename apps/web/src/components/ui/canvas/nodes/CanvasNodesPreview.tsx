import { Maximize2, PlayCircle } from "lucide-react";
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

export function OutputPreview({
  output,
  alt,
  crop = null,
  large = false,
}: {
  output: CanvasOutput;
  alt: string;
  crop?: NormalizedCanvasCrop | null;
  large?: boolean;
}) {
  const media = useOutputPreviewMedia(output);
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
        <OutputTypeBadge type={output.type} />
        <CanvasOutputDownloadButton
          output={output}
          title={alt}
          className="absolute bottom-2 left-2 z-10"
        />
      </div>
      {media.videoSrc ? (
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
  onError: () => void;
}

function useOutputPreviewMedia(output: CanvasOutput): OutputPreviewMediaState {
  const imageSources =
    output.type === "image" ? imagePreviewSources(output) : [];
  const imageSourceKey = imageSources.join("\n");
  const [imageSourceState, setImageSourceState] = useState({
    key: imageSourceKey,
    index: 0,
  });
  const imageSourceIndex =
    imageSourceState.key === imageSourceKey ? imageSourceState.index : 0;
  const videoSrc = output.type === "video" ? videoPlaybackSource(output) : null;
  const poster = output.type === "video" ? videoPosterSource(output) : null;
  const [failedVideoSrc, setFailedVideoSrc] = useState<string | null>(null);
  const imageSrc = imageSources[imageSourceIndex] ?? null;
  const visibleSrc =
    output.type === "video"
      ? videoSrc === failedVideoSrc
        ? null
        : videoSrc
      : imageSrc;
  const onError = () => {
    if (!visibleSrc) return;
    if (output.type === "video") {
      setFailedVideoSrc(visibleSrc);
      return;
    }
    setImageSourceState({
      key: imageSourceKey,
      index: imageSourceIndex + 1,
    });
  };
  return { visibleSrc, videoSrc, poster, onError };
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
  return (
    <button
      type="button"
      aria-label={video ? `播放${alt}` : `放大查看${alt}`}
      title={video ? "播放视频" : "查看大图"}
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
        poster={media.poster}
        alt={alt}
        width={width}
        height={height}
        cropStyle={cropStyle}
        onNaturalSize={onNaturalSize}
        onError={media.onError}
      />
      <OutputPreviewAffordance type={output.type} />
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
  type,
  src,
  poster,
  alt,
  width,
  height,
  cropStyle,
  onNaturalSize,
  onError,
}: {
  type: CanvasOutput["type"];
  src: string | null;
  poster?: string | null;
  alt: string;
  width?: number;
  height?: number;
  cropStyle?: CSSProperties;
  onNaturalSize: (
    size: { src: string; width: number; height: number },
  ) => void;
  onError: () => void;
}) {
  if (!src) {
    return (
      <div className="grid h-full min-h-16 place-items-center type-caption text-[var(--fg-3)]">
        无预览
      </div>
    );
  }
  if (type === "video") {
    return (
      <video
        src={src}
        poster={poster || undefined}
        muted
        playsInline
        preload={poster ? "metadata" : "auto"}
        aria-label={alt}
        className="pointer-events-none h-full w-full object-contain"
        onLoadedMetadata={(event) => {
          if (poster) return;
          const video = event.currentTarget;
          if (video.duration > 0 && video.currentTime === 0) {
            video.currentTime = Math.min(0.05, video.duration / 10);
          }
        }}
        onLoadedData={(event) => {
          const video = event.currentTarget;
          if (video.videoWidth <= 0 || video.videoHeight <= 0) return;
          onNaturalSize({
            src,
            width: video.videoWidth,
            height: video.videoHeight,
          });
        }}
        onError={onError}
      />
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- API-backed signed media and canvas thumbnails.
    <img
      src={src}
      alt={alt}
      width={width}
      height={height}
      loading="lazy"
      decoding="async"
      className={cn(!cropStyle && "h-full w-full object-contain")}
      style={cropStyle}
      onLoad={(event) => {
        if (width && height) return;
        const image = event.currentTarget;
        if (image.naturalWidth <= 0 || image.naturalHeight <= 0) return;
        onNaturalSize({
          src,
          width: image.naturalWidth,
          height: image.naturalHeight,
        });
      }}
      onError={onError}
      draggable={false}
    />
  );
}

function OutputTypeBadge({ type }: { type: CanvasOutput["type"] }) {
  if (type !== "video") return null;
  return (
    <span className="pointer-events-none absolute bottom-1 right-1 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-1.5 py-0.5 type-mono-meta text-[var(--media-control-fg)]">
      视频
    </span>
  );
}

function imagePreviewSources(output: CanvasOutput): string[] {
  return uniqueMediaSources([
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
