"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BookmarkPlus,
  Check,
  CheckSquare,
  Download,
  FolderPlus,
  Image as ImageIcon,
  Loader2,
  Maximize2,
  MessageSquare,
  Share2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import {
  apiFetchNoContent,
  imageBinaryUrl,
  imageVariantUrl,
  type ApparelModelLibraryItemCreateIn,
  type ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";
import {
  useCreateApparelModelLibraryItemMutation,
  useCreateMultiShareMutation,
} from "@/lib/queries";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import {
  getLightboxDownloadFilename,
  triggerImageDownload,
} from "@/components/ui/lightbox/utils";
import { imageResultToLightboxItem } from "@/lib/imageResultLightbox";
import { shareOrCopyLink } from "@/lib/shareLink";
import { cn } from "@/lib/utils";
import type { Generation, Message } from "@/lib/types";
import { useUiStore } from "@/store/useUiStore";
import type { LightboxItem } from "@/components/ui/lightbox/types";

type GalleryFilter = "all" | "upload" | "generated";
type GalleryBulkAction = "delete" | "favorite" | "export" | null;
type GalleryFavoriteGender = "female" | "male";

const FAVORITE_AGE_OPTIONS: Array<[ModelLibraryItemAgeSegment, string]> = [
  ["user_favorites", "用户收藏"],
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "熟龄"],
  ["middle_aged", "中年"],
  ["senior", "老年"],
];

const FAVORITE_GENDER_OPTIONS: Array<[GalleryFavoriteGender, string]> = [
  ["female", "女"],
  ["male", "男"],
];

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
      const createdAt = gen.finished_at ?? gen.started_at ?? msg.created_at;
      const item = imageResultToLightboxItem(gen, image, {
        previewUrl: display,
        thumbUrl: thumb,
        type: "generated-image",
        source: "generated",
        sourceId: msg.id,
        createdAt,
      });
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
        createdAt,
        sourceLabel: "生成",
        sizeLabel: label,
        item,
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

function deleteGalleryImage(imageId: string) {
  return apiFetchNoContent(`/images/${encodeURIComponent(imageId)}`, {
    method: "DELETE",
  });
}

function favoriteSourceFor(
  image: GalleryImage,
): ApparelModelLibraryItemCreateIn["source"] {
  return image.source === "upload" ? "user_upload" : "generated";
}

function favoriteTitleFor(
  image: GalleryImage,
  index: number,
  total: number,
): string {
  const base = image.alt.trim().replace(/\s+/g, " ").slice(0, 36);
  if (base) return total > 1 ? `${base} #${index + 1}` : base;
  return `${image.sourceLabel}图片 ${image.shareImageId.slice(0, 8)}`;
}

export function ConversationImageGallery({
  messages,
  generations,
}: ConversationImageGalleryProps) {
  const setStudioView = useUiStore((s) => s.setStudioView);
  const [filter, setFilter] = useState<GalleryFilter>("all");
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [hiddenImageIds, setHiddenImageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [favoriteDialogOpen, setFavoriteDialogOpen] = useState(false);
  const [favoriteAgeSegment, setFavoriteAgeSegment] =
    useState<ModelLibraryItemAgeSegment>("user_favorites");
  const [favoriteGender, setFavoriteGender] =
    useState<GalleryFavoriteGender>("female");
  const [bulkAction, setBulkAction] = useState<GalleryBulkAction>(null);
  const createMultiShareMutation = useCreateMultiShareMutation();
  const createFavoriteMutation = useCreateApparelModelLibraryItemMutation();
  const masonryRef = useRef<HTMLDivElement | null>(null);
  const [columnCount, setColumnCount] = useState(5);
  const allImages = useMemo(
    () => collectConversationImages(messages, generations),
    [messages, generations],
  );
  const galleryImages = useMemo(
    () => allImages.filter((image) => !hiddenImageIds.has(image.shareImageId)),
    [allImages, hiddenImageIds],
  );
  const uploadCount = galleryImages.filter((image) => image.source === "upload").length;
  const generatedCount = galleryImages.filter(
    (image) => image.source === "generated",
  ).length;
  const visibleImages = useMemo(
    () =>
      filter === "all"
        ? galleryImages
        : galleryImages.filter((image) => image.source === filter),
    [galleryImages, filter],
  );
  const selectedImages = useMemo(() => {
    if (selectedIds.size === 0) return [];
    const seen = new Set<string>();
    const items: GalleryImage[] = [];
    for (const image of galleryImages) {
      if (!selectedIds.has(image.shareImageId) || seen.has(image.shareImageId)) {
        continue;
      }
      seen.add(image.shareImageId);
      items.push(image);
    }
    return items;
  }, [galleryImages, selectedIds]);
  const selectedImageIds = useMemo(() => {
    return selectedImages.map((image) => image.shareImageId);
  }, [selectedImages]);
  const selectionActive = selectionMode || selectedImageIds.length > 0;
  const bulkBusy = bulkAction !== null || createMultiShareMutation.isPending;
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
      if (result === "failed") {
        pushMobileToast("分享链接复制失败", "danger");
      } else if (result !== "cancelled") {
        pushMobileToast(result === "shared" ? "已打开分享菜单" : "分享链接已复制", "success");
        clearSelection();
      }
    } catch {
      pushMobileToast("分享链接生成失败", "danger");
    }
  }, [clearSelection, createMultiShareMutation, selectedImageIds]);
  const exportSelectedImages = useCallback(async () => {
    if (selectedImages.length === 0 || bulkAction !== null) return;
    setBulkAction("export");
    try {
      let exported = 0;
      for (const image of selectedImages) {
        await triggerImageDownload(
          image.src,
          getLightboxDownloadFilename(image.item),
        );
        exported += 1;
      }
      pushMobileToast(`已开始导出 ${exported} 张图片`, "success");
      clearSelection();
    } catch {
      pushMobileToast("部分图片导出失败", "danger");
    } finally {
      setBulkAction(null);
    }
  }, [bulkAction, clearSelection, selectedImages]);
  const deleteSelectedImages = useCallback(async () => {
    if (selectedImageIds.length === 0 || bulkAction !== null) return;
    setBulkAction("delete");
    const results = await Promise.allSettled(
      selectedImageIds.map((imageId) => deleteGalleryImage(imageId)),
    );
    const deletedIds = selectedImageIds.filter(
      (_imageId, index) => results[index]?.status === "fulfilled",
    );
    const failedIds = selectedImageIds.filter(
      (_imageId, index) => results[index]?.status === "rejected",
    );
    if (deletedIds.length > 0) {
      setHiddenImageIds((prev) => {
        const next = new Set(prev);
        for (const imageId of deletedIds) next.add(imageId);
        return next;
      });
    }
    if (failedIds.length === 0) {
      pushMobileToast(`已删除 ${deletedIds.length} 张图片`, "success");
      setDeleteDialogOpen(false);
      clearSelection();
    } else if (deletedIds.length > 0) {
      pushMobileToast(
        `已删除 ${deletedIds.length} 张，${failedIds.length} 张失败`,
        "warning",
      );
      setSelectedIds(new Set(failedIds));
    } else {
      pushMobileToast("删除失败", "danger");
    }
    setBulkAction(null);
  }, [bulkAction, clearSelection, selectedImageIds]);
  const favoriteSelectedImages = useCallback(async () => {
    if (selectedImages.length === 0 || bulkAction !== null) return;
    setBulkAction("favorite");
    const total = selectedImages.length;
    const results = await Promise.allSettled(
      selectedImages.map((image, index) =>
        createFavoriteMutation.mutateAsync({
          source: favoriteSourceFor(image),
          image_id: image.shareImageId,
          title: favoriteTitleFor(image, index, total),
          age_segment: favoriteAgeSegment,
          gender: favoriteGender,
          appearance_direction: null,
          style_tags: [],
          auto_tag: true,
        }),
      ),
    );
    const failedIds = selectedImages
      .filter((_image, index) => results[index]?.status === "rejected")
      .map((image) => image.shareImageId);
    const savedCount = total - failedIds.length;
    if (failedIds.length === 0) {
      pushMobileToast(`已收藏 ${savedCount} 张到模特库`, "success");
      setFavoriteDialogOpen(false);
      clearSelection();
    } else if (savedCount > 0) {
      pushMobileToast(
        `已收藏 ${savedCount} 张，${failedIds.length} 张失败`,
        "warning",
      );
      setSelectedIds(new Set(failedIds));
    } else {
      pushMobileToast("收藏失败", "danger");
    }
    setBulkAction(null);
  }, [
    bulkAction,
    clearSelection,
    createFavoriteMutation,
    favoriteAgeSegment,
    favoriteGender,
    selectedImages,
  ]);
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

  if (galleryImages.length === 0) {
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
            {galleryImages.length} 张图片 · {uploadCount} 张上传 · {generatedCount} 张生成
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {selectedImageIds.length > 0 ? (
            <>
              <GalleryActionButton
                tone="accent"
                onClick={shareSelectedImages}
                disabled={bulkBusy}
                loading={createMultiShareMutation.isPending}
                icon={<Share2 className="h-3.5 w-3.5" aria-hidden />}
              >
                {createMultiShareMutation.isPending
                  ? "分享中"
                  : `分享 ${selectedImageIds.length} 张`}
              </GalleryActionButton>
              <GalleryActionButton
                onClick={() => setFavoriteDialogOpen(true)}
                disabled={bulkBusy}
                loading={bulkAction === "favorite"}
                icon={<BookmarkPlus className="h-3.5 w-3.5" aria-hidden />}
              >
                收藏
              </GalleryActionButton>
              <GalleryActionButton
                onClick={() => void exportSelectedImages()}
                disabled={bulkBusy}
                loading={bulkAction === "export"}
                icon={<Download className="h-3.5 w-3.5" aria-hidden />}
              >
                导出
              </GalleryActionButton>
              <GalleryActionButton
                disabled
                title="当前缺少明确的加入项目 API"
                icon={<FolderPlus className="h-3.5 w-3.5" aria-hidden />}
              >
                加入项目
              </GalleryActionButton>
              <GalleryActionButton
                tone="danger"
                onClick={() => setDeleteDialogOpen(true)}
                disabled={bulkBusy}
                loading={bulkAction === "delete"}
                icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
              >
                删除
              </GalleryActionButton>
              <button
                type="button"
                onClick={clearSelection}
                aria-label="取消选择"
                disabled={bulkBusy}
                className={cn(
                  "inline-flex h-7 w-7 items-center justify-center rounded-lg",
                  "border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)]",
                  "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] transition-colors disabled:opacity-55",
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
            className="flex gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-1)] p-0.5"
          >
            <FilterButton
              active={filter === "all"}
              label="全部"
              count={galleryImages.length}
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

      <ConfirmDialog
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        title="删除选中的图片"
        description={
          <span>
            将从图片库删除 {selectedImageIds.length} 张图片。已发送的会话文字仍会保留，
            但这些图片的原图链接会失效。
          </span>
        }
        tone="danger"
        confirmText="删除"
        confirming={bulkAction === "delete"}
        onConfirm={deleteSelectedImages}
      />

      <ConfirmDialog
        open={favoriteDialogOpen}
        onOpenChange={setFavoriteDialogOpen}
        title="收藏到模特库"
        description={
          <FavoriteOptionsForm
            count={selectedImageIds.length}
            ageSegment={favoriteAgeSegment}
            gender={favoriteGender}
            onAgeSegmentChange={setFavoriteAgeSegment}
            onGenderChange={setFavoriteGender}
          />
        }
        confirmText="收藏"
        confirming={bulkAction === "favorite"}
        onConfirm={favoriteSelectedImages}
      />
    </section>
  );
}

function GalleryActionButton({
  children,
  icon,
  tone = "default",
  loading,
  disabled,
  title,
  onClick,
}: {
  children: React.ReactNode;
  icon: React.ReactNode;
  tone?: "default" | "accent" | "danger";
  loading?: boolean;
  disabled?: boolean;
  title?: string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[11px]",
        "border transition-colors disabled:cursor-not-allowed disabled:opacity-55",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        tone === "accent"
          ? "border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.15)] text-[var(--amber-300)] hover:bg-[rgba(242,169,58,0.22)]"
          : tone === "danger"
            ? "border-danger-border bg-danger-soft text-danger hover:brightness-110"
            : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {loading ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
      ) : (
        icon
      )}
      {children}
    </button>
  );
}

function FavoriteOptionsForm({
  count,
  ageSegment,
  gender,
  onAgeSegmentChange,
  onGenderChange,
}: {
  count: number;
  ageSegment: ModelLibraryItemAgeSegment;
  gender: GalleryFavoriteGender;
  onAgeSegmentChange: (value: ModelLibraryItemAgeSegment) => void;
  onGenderChange: (value: GalleryFavoriteGender) => void;
}) {
  return (
    <div className="mt-3 space-y-3">
      <p className="text-[12px] leading-5 text-[var(--fg-1)]">
        将 {count} 张图片加入用户收藏，并自动识别气质标签。
      </p>
      <label className="block">
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          年龄段
        </span>
        <select
          value={ageSegment}
          onChange={(event) =>
            onAgeSegmentChange(event.target.value as ModelLibraryItemAgeSegment)
          }
          className={cn(
            "h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-0)] px-2.5",
            "text-[13px] text-[var(--fg-0)] focus:border-[var(--border-amber)] focus:outline-none",
          )}
        >
          {FAVORITE_AGE_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <div>
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          性别
        </span>
        <div className="flex gap-2">
          {FAVORITE_GENDER_OPTIONS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={gender === value}
              onClick={() => onGenderChange(value)}
              className={cn(
                "inline-flex h-8 items-center rounded-lg border px-3 text-[12px] transition-colors",
                gender === value
                  ? "border-[var(--border-amber)] bg-[var(--amber-soft)] text-[var(--amber-300)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
    </div>
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
          ? "bg-[var(--bg-2)] text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {label}
      <span className="font-mono text-[10px] text-current/65">{count}</span>
    </button>
  );
}
