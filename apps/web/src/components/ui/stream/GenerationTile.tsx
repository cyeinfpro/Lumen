"use client";

import {
  Check,
  Copy,
  Crosshair,
  Download,
  Image as ImageIcon,
  ImageDown,
  RotateCcw,
  Zap,
} from "lucide-react";
import {
  memo,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter } from "next/navigation";
import { formatDistanceToNowStrict } from "date-fns";
import { zhCN } from "date-fns/locale";
import { ActionSheet } from "@/components/ui/primitives/mobile";
import type { GenerationSummary } from "@/lib/queries/stream";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { useChatStore } from "@/store/useChatStore";
import type { GeneratedImage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/lib/imagePreload";

export interface GenerationTileProps {
  item: GenerationSummary;
  onOpen: (itemId: string, rect: DOMRect) => void;
  selectionMode?: boolean;
  selected?: boolean;
  onToggleSelect?: (imageId: string) => void;
}

const LONG_PRESS_MS = 420;
const TAP_FEEDBACK_MS = 180;

function formatAge(iso: string): string {
  try {
    return formatDistanceToNowStrict(new Date(iso), {
      addSuffix: false,
      locale: zhCN,
    });
  } catch {
    return "";
  }
}

function mimeFromOutputFormat(format: string | null | undefined): string | undefined {
  if (format === "jpeg") return "image/jpeg";
  if (format === "png") return "image/png";
  if (format === "webp") return "image/webp";
  return undefined;
}

function extensionFromMime(mime: string | null | undefined): string {
  if (!mime) return "png";
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (normalized === "image/jpeg") return "jpg";
  if (normalized === "image/png") return "png";
  if (normalized === "image/webp") return "webp";
  return "png";
}

function imageMimeFor(item: GenerationSummary): string | undefined {
  return item.image.mime ?? mimeFromOutputFormat(item.output_format);
}

function imageDownloadName(item: GenerationSummary): string {
  return `${item.id}.${extensionFromMime(imageMimeFor(item))}`;
}

function buildGeneratedImage(item: GenerationSummary): GeneratedImage {
  return {
    id: item.image.id,
    data_url: item.image.url,
    mime: imageMimeFor(item),
    display_url: item.image.display_url ?? item.image.url,
    preview_url: item.image.display_url ?? item.image.thumb_url,
    thumb_url: item.image.thumb_url,
    width: item.image.width,
    height: item.image.height,
    parent_image_id: null,
    from_generation_id: item.id,
    size_requested: item.size_actual,
    size_actual: item.size_actual,
  };
}

function ensureImageInChatStore(item: GenerationSummary) {
  const imageId = item.image.id;
  if (useChatStore.getState().imagesById[imageId]) return;
  const image = buildGeneratedImage(item);
  useChatStore.setState((state) => ({
    imagesById: {
      ...state.imagesById,
      [imageId]: image,
    },
  }));
}

function imageSourcesFor(item: GenerationSummary): string[] {
  const seen = new Set<string>();
  const sources = [
    item.image.thumb_url,
    item.image.display_url,
    item.image.url,
  ].filter((src): src is string => Boolean(src && src.trim()));
  return sources.filter((src) => {
    if (seen.has(src)) return false;
    seen.add(src);
    return true;
  });
}

function localVariantSrcSet(imageId: string): string {
  return [
    `${imageVariantUrl(imageId, "thumb256")} 256w`,
    `${imageVariantUrl(imageId, "preview1024")} 1024w`,
    `${imageVariantUrl(imageId, "display2048")} 2048w`,
  ].join(", ");
}

function GenerationTileComponent({
  item,
  onOpen,
  selectionMode = false,
  selected = false,
  onToggleSelect,
}: GenerationTileProps) {
  const [tapped, setTapped] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [sourceIndex, setSourceIndex] = useState(0);
  const [imageLoaded, setImageLoaded] = useState(false);
  const pressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tapTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressed = useRef(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const router = useRouter();
  const promoteImageToReference = useChatStore((s) => s.promoteImageToReference);
  const imageId = item.image.id;

  const age = useMemo(() => formatAge(item.created_at), [item.created_at]);
  const imageSources = useMemo(() => imageSourcesFor(item), [item]);
  const imageSrc = imageSources[sourceIndex] ?? null;
  const imageFailed = imageSources.length === 0 || sourceIndex >= imageSources.length;
  const imageSrcSet = useMemo(() => localVariantSrcSet(imageId), [imageId]);
  const lightboxPreview = useMemo(
    () => item.image.display_url ?? imageVariantUrl(imageId, "display2048"),
    [imageId, item.image.display_url],
  );

  useEffect(() => {
    return () => {
      if (pressTimer.current) clearTimeout(pressTimer.current);
      if (tapTimer.current) clearTimeout(tapTimer.current);
    };
  }, []);

  const onPointerDown = useCallback(
    () => {
      if (selectionMode) return;
      prewarmImage(lightboxPreview);
      longPressed.current = false;
      if (pressTimer.current) clearTimeout(pressTimer.current);
      pressTimer.current = setTimeout(() => {
        longPressed.current = true;
        setSheetOpen(true);
        try {
          navigator.vibrate?.(10);
        } catch {
          /* no-op */
        }
      }, LONG_PRESS_MS);
    },
    [lightboxPreview, selectionMode],
  );

  const onPreviewIntent = useCallback(() => {
    if (selectionMode) return;
    prewarmImage(lightboxPreview);
  }, [lightboxPreview, selectionMode]);

  const clearPress = useCallback(() => {
    if (pressTimer.current) {
      clearTimeout(pressTimer.current);
      pressTimer.current = null;
    }
  }, []);

  const onClick = useCallback(() => {
    if (longPressed.current) {
      longPressed.current = false;
      return;
    }
    if (selectionMode) {
      onToggleSelect?.(imageId);
      return;
    }
    setTapped(true);
    if (tapTimer.current) clearTimeout(tapTimer.current);
    tapTimer.current = setTimeout(() => setTapped(false), TAP_FEEDBACK_MS);
    const el = rootRef.current;
    if (!el) return;
    const img = el.querySelector("img");
    const rect = (img ?? el).getBoundingClientRect();
    onOpen(item.id, rect);
  }, [imageId, item.id, onOpen, onToggleSelect, selectionMode]);

  const onKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    onClick();
  }, [onClick]);

  const onMakeRef = useCallback(() => {
    const imageId = item.image.id;
    const before = useChatStore.getState().composer.attachments.length;
    try {
      ensureImageInChatStore(item);
      promoteImageToReference(imageId);
      const after = useChatStore.getState().composer.attachments.length;
      if (after <= before) {
        throw new Error("image_not_available");
      }
      router.push("/");
      pushMobileToast("已添加为参考图", "success");
    } catch {
      pushMobileToast("添加参考图失败", "danger");
    }
  }, [item, promoteImageToReference, router]);

  const onSave = useCallback(() => {
    const url = item.image.url;
    if (!url) {
      pushMobileToast("原图地址不可用", "danger");
      return;
    }
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener noreferrer";
    a.target = "_blank";
    a.download = imageDownloadName(item);
    document.body.appendChild(a);
    a.click();
    a.remove();
  }, [item]);

  const onCopyPrompt = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(item.prompt);
      pushMobileToast("已复制 prompt", "success");
    } catch {
      pushMobileToast("复制失败", "danger");
    }
  }, [item.prompt]);

  const onLocate = useCallback(() => {
    router.push(`/?scrollTo=${encodeURIComponent(item.message_id)}`);
  }, [router, item.message_id]);

  const w = Math.max(1, item.image.width || 1);
  const h = Math.max(1, item.image.height || 1);
  const promptChars = Array.from(item.prompt);
  const promptShort = promptChars.slice(0, 68).join("");
  const promptTruncated = promptChars.length > 68;
  const altText = promptChars.slice(0, 80).join("") || "生成作品";

  return (
    <>
    <article
        className={cn(
          "group relative block w-full overflow-hidden rounded-lg",
          "border border-[var(--border-subtle)] bg-[var(--bg-1)]/94 text-left shadow-[var(--shadow-1)]",
          "transition-[border-color,box-shadow,transform,background-color] duration-200 ease-[var(--ease-develop)]",
          "hover:-translate-y-0.5 hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)] hover:shadow-[var(--shadow-2)]",
          "active:scale-[0.995]",
          tapped && "shadow-amber",
          selected && "border-[var(--amber-400)] shadow-amber",
        )}
      >
        <div
          ref={rootRef}
          role="button"
          tabIndex={0}
          onPointerDown={onPointerDown}
          onPointerEnter={onPreviewIntent}
          onPointerUp={clearPress}
          onPointerLeave={clearPress}
          onPointerCancel={clearPress}
          onClick={onClick}
          onFocus={onPreviewIntent}
          onKeyDown={onKeyDown}
          className="block w-full cursor-pointer text-left focus-visible:outline-none"
          aria-label={promptShort || "查看作品"}
          aria-pressed={selectionMode ? selected : undefined}
        >
          <div
            className="relative w-full overflow-hidden bg-[var(--bg-2)]"
            style={{ aspectRatio: `${w} / ${h}` }}
          >
            {!imageLoaded && !imageFailed && (
              <div className="absolute inset-0 animate-shimmer bg-[var(--bg-2)]" />
            )}
            {imageSrc && !imageFailed ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={imageSrc}
                srcSet={imageSrcSet}
                sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 25vw"
                alt={altText}
                loading="lazy"
                decoding="async"
                fetchPriority="low"
                draggable={false}
                onLoad={() => setImageLoaded(true)}
                onError={() => {
                  setImageLoaded(false);
                  setSourceIndex((index) => index + 1);
                }}
                className={cn(
                  "absolute inset-0 h-full w-full object-cover transition-[transform,opacity,filter] duration-300 ease-[var(--ease-develop)]",
                  "group-hover:scale-[1.025] group-hover:brightness-[1.04]",
                  imageLoaded ? "opacity-100" : "opacity-0",
                )}
              />
            ) : (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-[var(--bg-2)] text-[var(--fg-2)]">
                <ImageIcon className="h-5 w-5" />
                <span className="text-[11px]">图片载入失败</span>
                <button
                  type="button"
                  onPointerDown={(e) => e.stopPropagation()}
                  onKeyDown={(e) => e.stopPropagation()}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSourceIndex(0);
                    setImageLoaded(false);
                  }}
                  className="mt-1 inline-flex h-7 cursor-pointer items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
                >
                  <RotateCcw className="h-3 w-3" />
                  重试
                </button>
              </div>
            )}
            <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/45 via-transparent to-black/20 opacity-0 transition-opacity duration-200 group-hover:opacity-100" />
            {age && (
              <span className="pointer-events-none absolute left-2 top-2 rounded-md bg-black/45 px-1.5 py-1 text-[10px] tabular-nums text-white/82 opacity-0 backdrop-blur-md transition-opacity duration-200 group-hover:opacity-100">
                {age}
              </span>
            )}
            {selectionMode && (
              <span
                className={cn(
                  "absolute right-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded-full border backdrop-blur-md transition-colors",
                  selected
                    ? "border-[rgba(242,169,58,0.55)] bg-[var(--amber-400)] text-black"
                    : "border-white/20 bg-black/45 text-white/80",
                )}
                aria-hidden
              >
                {selected && <Check className="h-4 w-4" />}
              </span>
            )}
          </div>

          <div className="space-y-2 px-2.5 pb-2.5 pt-2.5">
            <div
              className="text-[13px] leading-[1.5] text-[var(--fg-0)]"
              style={{
                display: "-webkit-box",
                WebkitLineClamp: 2,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {promptShort}
              {promptTruncated && "…"}
            </div>
            <div className="flex min-w-0 items-center gap-1.5 overflow-hidden">
              {age && (
                <span className="shrink-0 text-[11px] tabular-nums text-[var(--fg-2)]">
                  {age}
                </span>
              )}
              {item.aspect_ratio && (
                <span className="shrink-0 rounded-[4px] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-1.5 py-px text-[10px] font-mono tabular-nums text-[var(--fg-2)]">
                  {item.aspect_ratio}
                </span>
              )}
              {item.fast && (
                <span className="inline-flex shrink-0 items-center gap-0.5 rounded-[4px] border border-[rgba(242,169,58,0.18)] bg-[rgba(242,169,58,0.12)] px-1.5 py-px text-[10px] font-medium text-[var(--amber-300)]">
                  <Zap className="h-2.5 w-2.5" />
                  Fast
                </span>
              )}
              {item.has_ref && (
                <span className="shrink-0 rounded-[4px] border border-[rgba(139,92,246,0.18)] bg-[rgba(139,92,246,0.12)] px-1.5 py-px text-[10px] font-medium text-[#a78bfa]">
                  参考图
                </span>
              )}
            </div>
          </div>
        </div>

        <div className={cn(
          "pointer-events-none absolute right-2 top-2 hidden items-center gap-1 opacity-0 transition-opacity duration-200 group-hover:opacity-100 md:flex",
          selectionMode && "hidden",
        )}>
          <TileAction label="做参考图" onClick={onMakeRef}>
            <ImageIcon className="h-3.5 w-3.5" />
          </TileAction>
          <TileAction label="复制 prompt" onClick={onCopyPrompt}>
            <Copy className="h-3.5 w-3.5" />
          </TileAction>
          <TileAction label="保存原图" onClick={onSave}>
            <Download className="h-3.5 w-3.5" />
          </TileAction>
          {imageFailed && (
            <TileAction label="重试载入" onClick={() => { setSourceIndex(0); setImageLoaded(false); }}>
              <RotateCcw className="h-3.5 w-3.5" />
            </TileAction>
          )}
        </div>
      </article>

      <ActionSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        actions={[
          {
            key: "ref",
            label: "做参考图",
            icon: <ImageIcon className="w-4 h-4" />,
            onSelect: onMakeRef,
          },
          {
            key: "save",
            label: "保存到相册",
            icon: <ImageDown className="w-4 h-4" />,
            onSelect: onSave,
          },
          {
            key: "copy",
            label: "复制 prompt",
            icon: <Copy className="w-4 h-4" />,
            onSelect: onCopyPrompt,
          },
          {
            key: "locate",
            label: "在对话中定位",
            icon: <Crosshair className="w-4 h-4" />,
            onSelect: onLocate,
          },
        ]}
      />
    </>
  );
}

export const GenerationTile = memo(GenerationTileComponent);

function TileAction({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="pointer-events-auto inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-md border border-white/10 bg-black/50 text-white shadow-[0_6px_18px_rgba(0,0,0,0.24)] backdrop-blur-md transition-[background-color,transform] hover:bg-black/70 active:scale-95 focus-visible:outline-none"
    >
      {children}
    </button>
  );
}
