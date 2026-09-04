"use client";

import {
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  memo,
  useRef,
  useState,
} from "react";
import { Check, Copy, ImagePlus, MoreHorizontal } from "lucide-react";

import {
  Button,
  IconButton,
  MediaControlButton,
} from "@/components/ui/primitives";
import { ViewportImage } from "@/components/ui/ViewportImage";
import { cn } from "@/lib/utils";
import { aspectRatioToCss } from "@/lib/sizing";
import { imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/features/assets";
import { imageResultToLightboxItem } from "@/lib/imageResultLightbox";
import type { Generation, GeneratedImage, UserMessage } from "@/lib/types";
import type { LightboxItem } from "@/components/ui/lightbox/types";

export interface ImageMenuInfo {
  imageId: string;
  prompt: string;
  genId: string;
  x: number;
  y: number;
}

export type ConversationTurnSide = "user" | "assistant";

export function ConversationTurn({
  id,
  side,
  children,
  className,
}: {
  id?: string;
  side: ConversationTurnSide;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      id={id}
      className={cn(
        "group/turn relative w-full",
        side === "user" && "mx-auto max-w-[var(--content-composer)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export const ConversationUserTurn = memo(function ConversationUserTurn({
  msg,
  copied,
  onCopy,
}: {
  msg: UserMessage;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <ConversationTurn id={`msg-${msg.id}`} side="user">
      <div className="flex items-start gap-3 border-l border-[var(--border-strong)] pl-4">
        <div className="min-w-0 flex-1">
          {msg.attachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {msg.attachments.map((attachment) => (
                <div
                  key={attachment.id}
                  className="relative h-11 w-11 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)]"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={attachment.data_url}
                    alt=""
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                </div>
              ))}
            </div>
          )}

          {msg.text ? (
            <div className="flex items-start gap-2">
              <p className="type-body min-w-0 flex-1 whitespace-pre-wrap break-words text-left text-[var(--fg-0)] [overflow-wrap:anywhere] font-zh-body">
                {msg.text}
              </p>
              <IconButton
                size="sm"
                aria-label={copied ? "已复制" : "复制"}
                tooltip={copied ? "已复制" : "复制"}
                onClick={onCopy}
                className={cn(
                  "mt-0.5",
                  copied
                    ? "bg-success-soft text-success opacity-100"
                    : "text-[var(--fg-3)] opacity-0 group-hover/turn:opacity-60 hover:!opacity-100 max-sm:opacity-100",
                )}
              >
                {copied ? (
                  <Check className="h-3.5 w-3.5" aria-hidden />
                ) : (
                  <Copy className="h-3.5 w-3.5" aria-hidden />
                )}
              </IconButton>
            </div>
          ) : null}
        </div>
      </div>
    </ConversationTurn>
  );
});

function aspectRatioNumber(
  image: Pick<GeneratedImage, "width" | "height">,
  fallback: string,
): number | null {
  if (image.width && image.height && image.height > 0) {
    return image.width / image.height;
  }
  const match = fallback.match(/^(\d+)\s*:\s*(\d+)$/);
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? width / height : null;
}

function imageSource(image: GeneratedImage): string {
  return image.preview_url ?? image.thumb_url ?? image.display_url ?? image.data_url;
}

function lightboxThumbUrl(image: GeneratedImage): string | undefined {
  return image.thumb_url ?? image.preview_url;
}

function isFreeGeneration(gen: Generation, image: GeneratedImage): boolean {
  return (
    gen.billing_free === true ||
    gen.billing_label === "free" ||
    gen.is_dual_race_bonus === true ||
    image.billing_free === true ||
    image.billing_label === "free" ||
    image.is_dual_race_bonus === true
  );
}

function formatElapsed(gen: Generation): string | null {
  if (!gen.finished_at || !gen.started_at) return null;
  const ms = Math.max(0, gen.finished_at - gen.started_at);
  return `${(Math.round(ms / 100) / 10).toFixed(1)}s`;
}

function desktopFrameStyle(ratio: number | null): CSSProperties {
  if (ratio === null) {
    return { width: "min(100%, 620px)" };
  }

  const maxWidth =
    ratio < 0.75
      ? 480
      : ratio <= 1.2
        ? 580
        : ratio <= 1.8
          ? 780
          : 860;
  const viewportHeightWidth = `${(ratio * 58).toFixed(2)}dvh`;
  return {
    width: `min(100%, ${maxWidth}px, ${viewportHeightWidth})`,
  };
}

function mobileFrameClass(ratio: number | null): string {
  if (ratio !== null && ratio < 0.58) return "max-w-[min(44%,176px)]";
  if (ratio !== null && ratio < 0.9) return "max-w-[min(60%,260px)]";
  if (ratio !== null && ratio > 1.7) return "max-w-[min(82%,340px)]";
  return "max-w-[min(76%,320px)]";
}

export interface ConversationFinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  platform: "desktop" | "mobile";
  inGrid?: boolean;
  onPreview: (button: HTMLButtonElement | null) => void;
  onCopy: () => void;
  onEditImage?: () => void;
  onOpenMenu?: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  onContextMenu?: (event: ReactMouseEvent<HTMLButtonElement>) => void;
}

function finalImageFrameClass(
  platform: ConversationFinalImageProps["platform"],
  inGrid: boolean,
  ratio: number | null,
): string {
  if (inGrid) return platform === "desktop" ? "justify-self-stretch" : "";
  if (platform === "desktop") return "mx-auto";
  return mobileFrameClass(ratio);
}

function finalImageFrameStyle(
  platform: ConversationFinalImageProps["platform"],
  inGrid: boolean,
  ratio: number | null,
): CSSProperties | undefined {
  if (platform !== "desktop" || inGrid) return undefined;
  return desktopFrameStyle(ratio);
}

function finalImageMediaHeightClass(
  platform: ConversationFinalImageProps["platform"],
  inGrid: boolean,
  isLongImage: boolean,
): string | undefined {
  if (platform !== "mobile" || !isLongImage) return undefined;
  return inGrid
    ? "h-[min(24vh,168px)] min-h-[112px]"
    : "h-[min(30vh,220px)] min-h-[140px]";
}

function finalImageAspectRatio(
  platform: ConversationFinalImageProps["platform"],
  isLongImage: boolean,
  ratioCss: string,
): string | undefined {
  if (platform === "mobile" && isLongImage) return undefined;
  return ratioCss;
}

function finalImageRootMargin(
  platform: ConversationFinalImageProps["platform"],
  inGrid: boolean,
): string {
  if (platform === "desktop") {
    return inGrid ? "480px 0px" : "720px 0px";
  }
  return inGrid ? "320px 0px" : "520px 0px";
}

function finalImageSizeLabel(image: GeneratedImage): string {
  const source = image.size_actual || `${image.width} × ${image.height}`;
  return source.replace(/(\d)\s*x\s*(\d)/gi, "$1 × $2");
}

interface FinalImageMediaProps
  extends Pick<
    ConversationFinalImageProps,
    | "gen"
    | "image"
    | "platform"
    | "inGrid"
    | "onPreview"
    | "onOpenMenu"
    | "onContextMenu"
  > {
  ratioCss: string;
  isLongImage: boolean;
  free: boolean;
}

function FinalImageMedia({
  gen,
  image,
  platform,
  inGrid = false,
  onPreview,
  onOpenMenu,
  onContextMenu,
  ratioCss,
  isLongImage,
  free,
}: FinalImageMediaProps) {
  const previewButtonRef = useRef<HTMLButtonElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const handlePreview = () => onPreview(previewButtonRef.current);
  const handlePreviewIntent = () => {
    const previewUrl =
      image.display_url ?? imageVariantUrl(image.id, "display2048");
    prewarmImage(previewUrl);
  };

  return (
    <div
      className={cn(
        "relative w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-1)]",
        "border border-[var(--border-subtle)] shadow-[var(--shadow-1)]",
        "transition-[border-color,opacity] duration-150 group-hover:border-[var(--border-strong)]",
        finalImageMediaHeightClass(platform, inGrid, isLongImage),
      )}
      style={{
        aspectRatio: finalImageAspectRatio(platform, isLongImage, ratioCss),
        contain: "layout paint",
      }}
    >
      <Button
        ref={previewButtonRef}
        variant="ghost"
        size="md"
        onClick={handlePreview}
        onPointerEnter={handlePreviewIntent}
        onPointerDown={handlePreviewIntent}
        onFocus={handlePreviewIntent}
        onContextMenu={onContextMenu}
        aria-label="查看大图"
        className="absolute inset-0 block h-full w-full min-h-0 min-w-0 rounded-none border-0 bg-transparent p-0 text-left hover:bg-transparent focus-visible:shadow-[var(--ring)]"
      >
        {!loaded && (
          <span
            aria-hidden
            className="absolute inset-0 animate-pulse bg-[var(--bg-2)]"
          />
        )}
        <ViewportImage
          src={imageSource(image)}
          alt={gen.prompt}
          rootMargin={finalImageRootMargin(platform, inGrid)}
          persistAfterVisible
          fetchPriority="low"
          onLoad={() => setLoaded(true)}
          className={cn(
            "h-full w-full object-contain transition-opacity duration-300",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
      </Button>

      {free ? (
        <span className="pointer-events-none absolute left-2 top-2 type-caption rounded-full border border-[var(--border-strong)] bg-[var(--media-control-bg)] px-2 py-0.5 text-[var(--media-control-fg)] backdrop-blur">
          免费
        </span>
      ) : null}

      {onOpenMenu ? (
        <MediaControlButton
          size="sm"
          aria-label="更多操作"
          title="更多操作"
          onClick={onOpenMenu}
          onContextMenu={onContextMenu}
          className="absolute right-1.5 top-1.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </MediaControlButton>
      ) : null}
    </div>
  );
}

function FinalImageFooter({
  gen,
  tail,
  onCopy,
  onEditImage,
}: Pick<ConversationFinalImageProps, "gen" | "onCopy" | "onEditImage"> & {
  tail: string;
}) {
  return (
    <div className="flex items-center gap-1.5 px-0.5">
      <Button
        variant="ghost"
        size="sm"
        onClick={onCopy}
        className="min-w-0 flex-1 justify-start px-1 text-left type-caption tabular-nums text-[var(--fg-2)] hover:bg-transparent hover:text-[var(--fg-0)]"
        title={gen.prompt}
        aria-label="复制 prompt"
      >
        <span className="truncate">{tail}</span>
        <Copy className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
      </Button>
      {onEditImage ? (
        <Button
          variant="secondary"
          size="sm"
          leftIcon={<ImagePlus className="h-3.5 w-3.5" aria-hidden />}
          onClick={onEditImage}
          className="shrink-0 px-2 text-[var(--fg-2)]"
          aria-label="用作参考图"
        >
          参考图
        </Button>
      ) : null}
    </div>
  );
}

export const FinalImage = memo(function FinalImage({
  gen,
  image,
  platform,
  inGrid = false,
  onPreview,
  onCopy,
  onEditImage,
  onOpenMenu,
  onContextMenu,
}: ConversationFinalImageProps) {
  const ratioCss = aspectRatioToCss(gen.aspect_ratio);
  const ratio = aspectRatioNumber(image, gen.aspect_ratio);
  const isLongImage = ratio !== null && ratio < 0.58;
  const free = isFreeGeneration(gen, image);
  const elapsed = formatElapsed(gen);
  const tail = [
    gen.aspect_ratio,
    finalImageSizeLabel(image),
    elapsed ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      className={cn(
        "group flex w-full flex-col gap-1.5",
        finalImageFrameClass(platform, inGrid, ratio),
      )}
      style={finalImageFrameStyle(platform, inGrid, ratio)}
    >
      <FinalImageMedia
        gen={gen}
        image={image}
        platform={platform}
        inGrid={inGrid}
        onPreview={onPreview}
        onOpenMenu={onOpenMenu}
        onContextMenu={onContextMenu}
        ratioCss={ratioCss}
        isLongImage={isLongImage}
        free={free}
      />
      <FinalImageFooter
        gen={gen}
        tail={tail}
        onCopy={onCopy}
        onEditImage={onEditImage}
      />
    </div>
  );
});

export function lightboxItemForConversationImage(
  gen: Generation,
  image: GeneratedImage,
): LightboxItem {
  const previewUrl =
    image.display_url ?? imageVariantUrl(image.id, "display2048");
  return imageResultToLightboxItem(gen, image, {
    previewUrl,
    thumbUrl: lightboxThumbUrl(image),
    createdAt: gen.finished_at ?? gen.started_at,
  });
}
