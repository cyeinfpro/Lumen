"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  CheckSquare,
  Image as ImageIcon,
  Maximize2,
  MessageSquare,
  Share2,
  Sparkles,
  Upload,
  X,
} from "lucide-react";

import { imageBinaryUrl, imageVariantUrl } from "@/lib/apiClient";
import { useCreateMultiShareMutation } from "@/lib/queries";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { shareOrCopyLink } from "@/lib/shareLink";
import { cn } from "@/lib/utils";
import type { Generation, Message } from "@/lib/types";
import { useUiStore } from "@/store/useUiStore";
import type { LightboxItem } from "@/components/ui/lightbox/types";

type GalleryFilter = "all" | "upload" | "generated";

interface ConversationImageGalleryProps {
  messages: Message[];
  generations: Record<string, Generation>;
}

interface GalleryImage {
  id: string;
  shareImageId: string;
  source: Exclude<GalleryFilter, "all">;
  src: string;
  previewSrc?: string;
  thumbSrc?: string;
  alt: string;
  width?: number;
  height?: number;
  createdAt: number;
  sourceLabel: string;
  sizeLabel: string | null;
  item: LightboxItem;
}

interface MasonryEntry {
  image: GalleryImage;
  index: number;
  estimatedHeight: number;
}

function assistantGenerationIds(msg: Message): string[] {
  if (msg.role !== "assistant") return [];
  if (msg.generation_ids?.length) return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
}

function isoFromMs(ms: number): string | undefined {
  if (!Number.isFinite(ms)) return undefined;
  return new Date(ms).toISOString();
}

function imageRatio(image: Pick<GalleryImage, "width" | "height">): number | null {
  if (!image.width || !image.height || image.height <= 0) return null;
  return image.width / image.height;
}

function columnCountForWidth(width: number): number {
  if (!Number.isFinite(width) || width <= 0) return 5;
  const minTileWidth = 150;
  const gap = 6;
  return Math.max(2, Math.min(9, Math.floor((width + gap) / (minTileWidth + gap))));
}

function sizeLabel(width?: number, height?: number): string | null {
  if (!width || !height) return null;
  return `${width} x ${height}`;
}

function collectConversationImages(
  messages: Message[],
  generations: Record<string, Generation>,
): GalleryImage[] {
  const images: GalleryImage[] = [];
  const seenGenerated = new Set<string>();

  for (const msg of messages) {
    if (msg.role === "user") {
      msg.attachments.forEach((att, index) => {
        const id = `upload:${msg.id}:${att.id}:${index}`;
        const label = sizeLabel(att.width, att.height);
        images.push({
          id,
          shareImageId: att.id,
          source: "upload",
          src: att.data_url,
          previewSrc: att.data_url,
          thumbSrc: att.data_url,
          alt: msg.text || "用户上传图片",
          width: att.width,
          height: att.height,
          createdAt: msg.created_at,
          sourceLabel: "上传",
          sizeLabel: label,
          item: {
            id,
            url: att.data_url,
            previewUrl: att.data_url,
            thumbUrl: att.data_url,
            prompt: msg.text || "用户上传图片",
            width: att.width,
            height: att.height,
            size_actual: label ?? undefined,
            mime: att.mime,
            type: "uploaded-image",
            created_at: isoFromMs(msg.created_at),
            metadata: {
              source: "upload",
              message_id: msg.id,
              image_id: att.id,
            },
          },
        });
      });
      continue;
    }

    for (const generationId of assistantGenerationIds(msg)) {
      const gen = generations[generationId];
      const image = gen?.image;
      if (!gen || gen.status !== "succeeded" || !image) continue;
      if (seenGenerated.has(image.id)) continue;
      seenGenerated.add(image.id);

      const original = imageBinaryUrl(image.id);
      const display = image.display_url ?? imageVariantUrl(image.id, "display2048");
      const thumb =
        image.thumb_url ??
        image.preview_url ??
        imageVariantUrl(image.id, "thumb256");
      const label = image.size_actual || sizeLabel(image.width, image.height);
      images.push({
        id: image.id,
        shareImageId: image.id,
        source: "generated",
        src: original,
        previewSrc: display,
        thumbSrc: thumb,
        alt: gen.prompt,
        width: image.width,
        height: image.height,
        createdAt: gen.finished_at ?? gen.started_at ?? msg.created_at,
        sourceLabel: "生成",
        sizeLabel: label,
        item: {
          id: image.id,
          url: original,
          previewUrl: display,
          thumbUrl: thumb,
          prompt: gen.prompt,
          width: image.width,
          height: image.height,
          aspect_ratio: gen.aspect_ratio,
          size_actual: label ?? undefined,
          mime: image.mime,
          type: "generated-image",
          created_at: isoFromMs(gen.finished_at ?? gen.started_at ?? msg.created_at),
          metadata: {
            source: "generated",
            message_id: msg.id,
            generation_id: gen.id,
          },
        },
      });
    }
  }

  return images.sort((a, b) => b.createdAt - a.createdAt);
}

function estimateGalleryTileHeight(image: GalleryImage): number {
  const ratio = imageRatio(image);
  const mediaRatio = ratio === null ? 1 : Math.min(1.45, Math.max(0.56, 1 / ratio));
  const captionRows = image.alt.length > 42 ? 2 : 1;
  return mediaRatio * 1000 + 34 + captionRows * 14;
}

function distributeByEstimatedHeight(
  images: GalleryImage[],
  columnCount: number,
): MasonryEntry[][] {
  const count = Math.max(1, Math.floor(columnCount));
  const columns = Array.from({ length: count }, () => [] as MasonryEntry[]);
  const heights = Array.from({ length: count }, () => 0);

  images.forEach((image, index) => {
    const estimatedHeight = estimateGalleryTileHeight(image);
    let target = 0;
    for (let i = 1; i < count; i += 1) {
      if (heights[i] < heights[target]) target = i;
    }
    columns[target].push({ image, index, estimatedHeight });
    heights[target] += estimatedHeight;
  });

  return columns;
}

function tileClassFor(image: GalleryImage): string {
  const ratio = imageRatio(image);
  if (ratio === null) return "aspect-square";
  if (ratio < 0.58) return "h-44";
  if (ratio < 0.88) return "aspect-[4/5]";
  if (ratio > 1.75) return "aspect-[16/9]";
  if (ratio > 1.18) return "aspect-[4/3]";
  return "aspect-square";
}

function isLongImage(image: GalleryImage): boolean {
  const ratio = imageRatio(image);
  return ratio !== null && ratio < 0.58;
}

function openGallery(images: GalleryImage[], initialId: string) {
  if (typeof window === "undefined") return;
  const items = images.map((image) => image.item);
  // BUG-019: 统一使用 Zustand store action 打开灯箱。
  useUiStore.getState().openLightboxFromItems(items, initialId);
}

export function ConversationImageGallery({
  messages,
  generations,
}: ConversationImageGalleryProps) {
  const setStudioView = useUiStore((s) => s.setStudioView);
  const [filter, setFilter] = useState<GalleryFilter>("all");
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const createMultiShareMutation = useCreateMultiShareMutation();
  const masonryRef = useRef<HTMLDivElement | null>(null);
  const [columnCount, setColumnCount] = useState(5);
  const allImages = useMemo(
    () => collectConversationImages(messages, generations),
    [messages, generations],
  );
  const uploadCount = allImages.filter((image) => image.source === "upload").length;
  const generatedCount = allImages.filter(
    (image) => image.source === "generated",
  ).length;
  const visibleImages = useMemo(
    () =>
      filter === "all"
        ? allImages
        : allImages.filter((image) => image.source === filter),
    [allImages, filter],
  );
  const selectedImageIds = useMemo(() => {
    if (selectedIds.size === 0) return [];
    return allImages
      .map((image) => image.shareImageId)
      .filter((imageId, index, arr) => selectedIds.has(imageId) && arr.indexOf(imageId) === index);
  }, [allImages, selectedIds]);
  const selectionActive = selectionMode || selectedImageIds.length > 0;
  const toggleSelectedImage = useCallback((imageId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(imageId)) next.delete(imageId);
      else next.add(imageId);
      return next;
    });
  }, []);
  const clearSelection = useCallback(() => {
    setSelectedIds(new Set());
    setSelectionMode(false);
  }, []);
  const shareSelectedImages = useCallback(async () => {
    if (selectedImageIds.length === 0 || createMultiShareMutation.isPending) return;
    try {
      const share = await createMultiShareMutation.mutateAsync({
        imageIds: selectedImageIds,
      });
      const result = await shareOrCopyLink(share.url, "Lumen 图片分享");
      if (result !== "cancelled") {
        pushMobileToast(result === "shared" ? "已打开分享菜单" : "分享链接已复制", "success");
        clearSelection();
      }
    } catch {
      pushMobileToast("分享链接生成失败", "danger");
    }
  }, [clearSelection, createMultiShareMutation, selectedImageIds]);
  const masonryColumns = useMemo(
    () => distributeByEstimatedHeight(visibleImages, columnCount),
    [visibleImages, columnCount],
  );

  useEffect(() => {
    const el = masonryRef.current;
    if (!el) return;

    const update = () => setColumnCount(columnCountForWidth(el.clientWidth));
    update();

    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  if (allImages.length === 0) {
    return (
      <section
        id="conversation-image-gallery"
        aria-label="本会话图片"
        className="mx-auto flex min-h-[52vh] w-full max-w-[960px] flex-col items-center justify-center px-6 text-center"
      >
        <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]">
          <ImageIcon className="h-5 w-5" aria-hidden />
        </div>
        <h2 className="text-base font-medium text-[var(--fg-0)]">
          当前会话还没有图片
        </h2>
        <button
          type="button"
          onClick={() => setStudioView("chat")}
          className={cn(
            "mt-5 inline-flex h-9 items-center gap-2 rounded-full px-3 text-sm",
            "border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)]",
            "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          )}
        >
          <MessageSquare className="h-4 w-4" aria-hidden />
          回到对话
        </button>
      </section>
    );
  }

  return (
    <section
      id="conversation-image-gallery"
      aria-label="本会话图片"
      className="mx-auto w-full max-w-[1400px] px-4 py-5 xl:px-6"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-medium tracking-tight text-[var(--fg-0)]">
            本会话图片
          </h2>
          <p className="mt-0.5 text-[11px] text-[var(--fg-2)]">
            {allImages.length} 张图片 · {uploadCount} 张上传 · {generatedCount} 张生成
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {selectedImageIds.length > 0 ? (
            <>
              <button
                type="button"
                onClick={shareSelectedImages}
                disabled={createMultiShareMutation.isPending}
                className={cn(
                  "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[11px]",
                  "border border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.15)] text-[var(--amber-300)]",
                  "hover:bg-[rgba(242,169,58,0.22)] transition-colors disabled:opacity-60",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                )}
              >
                <Share2 className="h-3.5 w-3.5" aria-hidden />
                {createMultiShareMutation.isPending
                  ? "分享中"
                  : `分享 ${selectedImageIds.length} 张`}
              </button>
              <button
                type="button"
                onClick={clearSelection}
                aria-label="取消选择"
                className={cn(
                  "inline-flex h-7 w-7 items-center justify-center rounded-lg",
                  "border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)]",
                  "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                )}
              >
                <X className="h-3.5 w-3.5" aria-hidden />
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setSelectionMode((value) => !value)}
              aria-pressed={selectionMode}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[11px]",
                "border transition-colors",
                selectionMode
                  ? "border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.14)] text-[var(--amber-300)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <CheckSquare className="h-3.5 w-3.5" aria-hidden />
              多选
            </button>
          )}
          <div
            role="tablist"
            aria-label="图片来源"
            className="flex gap-1 rounded-lg border border-white/5 bg-white/[0.03] p-0.5"
          >
            <FilterButton
              active={filter === "all"}
              label="全部"
              count={allImages.length}
              onClick={() => setFilter("all")}
            />
            <FilterButton
              active={filter === "upload"}
              label="上传"
              count={uploadCount}
              onClick={() => setFilter("upload")}
            />
            <FilterButton
              active={filter === "generated"}
              label="生成"
              count={generatedCount}
              onClick={() => setFilter("generated")}
            />
          </div>
          <button
            type="button"
            onClick={() => setStudioView("chat")}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[11px]",
              "border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)]",
              "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            )}
          >
            <MessageSquare className="h-3.5 w-3.5" aria-hidden />
            回到对话
          </button>
        </div>
      </div>

      <div
        ref={masonryRef}
        className="grid"
        style={{
          gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))`,
          gap: 6,
        }}
      >
        {masonryColumns.map((column, columnIndex) => (
          <div
            key={`gallery-col-${columnIndex}`}
            className="flex min-w-0 flex-col"
            style={{ gap: 6 }}
          >
            {column.map(({ image, index, estimatedHeight }) => {
              const longImage = isLongImage(image);
              const Icon = image.source === "upload" ? Upload : Sparkles;
              const selected = selectedIds.has(image.shareImageId);
              return (
                <button
                  key={image.id}
                  type="button"
                  onClick={() => {
                    if (selectionActive) {
                      toggleSelectedImage(image.shareImageId);
                      return;
                    }
                    openGallery(visibleImages, image.id);
                  }}
                  aria-label={`查看${image.sourceLabel}图片`}
                  aria-pressed={selectionActive ? selected : undefined}
                  className={cn(
                    "group block w-full overflow-hidden rounded-lg",
                    "border border-[var(--border-subtle)] bg-[var(--bg-1)] text-left",
                    "shadow-[var(--shadow-1)] transition-colors duration-200",
                    "hover:border-[var(--amber-400)]/45 hover:bg-[var(--bg-2)]",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                    selected && "border-[var(--amber-400)] ring-2 ring-[var(--amber-400)]/45",
                  )}
                  style={{
                    animationDelay: `${Math.min(index * 22, 300)}ms`,
                    containIntrinsicSize: `1px ${Math.max(150, Math.min(360, estimatedHeight / 4))}px`,
                  }}
                >
                  <span
                    className={cn(
                      "relative block w-full overflow-hidden bg-[var(--bg-2)]",
                      tileClassFor(image),
                    )}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={image.thumbSrc ?? image.previewSrc ?? image.src}
                      alt=""
                      loading="lazy"
                      decoding="async"
                      className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.025]"
                    />
                    <span className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-black/60 to-transparent" />
                    <span className="absolute left-1.5 top-1.5 inline-flex items-center gap-0.5 rounded-full border border-white/15 bg-black/45 px-1.5 py-0.5 text-[10px] text-white/80 backdrop-blur">
                      <Icon className="h-2.5 w-2.5" aria-hidden />
                      {image.sourceLabel}
                    </span>
                    {longImage && (
                      <span className="absolute bottom-2 left-2 rounded-full border border-white/15 bg-black/45 px-2 py-1 text-[11px] text-white/82 backdrop-blur">
                        长图
                      </span>
                    )}
                    {selectionActive ? (
                      <span
                        className={cn(
                          "absolute right-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded-full border backdrop-blur",
                          selected
                            ? "border-[rgba(242,169,58,0.55)] bg-[var(--amber-400)] text-black"
                            : "border-white/20 bg-black/45 text-white/80",
                        )}
                      >
                        {selected && <Check className="h-4 w-4" aria-hidden />}
                      </span>
                    ) : (
                      <span className="absolute bottom-2 right-2 inline-flex h-8 w-8 items-center justify-center rounded-full border border-white/15 bg-black/45 text-white/82 opacity-0 backdrop-blur transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
                        <Maximize2 className="h-4 w-4" aria-hidden />
                      </span>
                    )}
                  </span>
                  <span className="block px-2 py-1.5">
                    <span className="block truncate text-[11px] text-[var(--fg-1)]">
                      {image.alt || image.sourceLabel}
                    </span>
                    {image.sizeLabel && (
                      <span className="mt-px block text-[10px] text-[var(--fg-3)]">
                        {image.sizeLabel}
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </section>
  );
}

function FilterButton({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-[11px] transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        active
          ? "bg-white/10 text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {label}
      <span className="font-mono text-[10px] text-current/65">{count}</span>
    </button>
  );
}
