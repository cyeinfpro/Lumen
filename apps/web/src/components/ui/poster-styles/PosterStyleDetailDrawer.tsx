"use client";

// 海报风格详情抽屉。
// 右侧滑出 drawer，结构：
// - 顶部 header（title + category + 关闭）
// - 大封面（点击放大 → Lightbox）
// - samples 缩略图行（点击切换主图）
// - 字段网格（mood / source / preset version / 自动打标时间）
// - palette 行
// - prompt_template 文本块（复制按钮）
// - 操作行（编辑 / auto-tag / 删除）

import { motion } from "framer-motion";
import {
  Check,
  Copy,
  Edit3,
  Maximize2,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import Image from "next/image";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import {
  POSTER_STYLE_CATEGORY_LABEL,
  type PosterStyleItem,
} from "@/lib/apiClient";
import {
  useDeletePosterStyleMutation,
  usePosterStyleQuery,
  useTriggerPosterStyleAutoTagMutation,
} from "@/lib/queries";
import { useUiStore } from "@/store/useUiStore";
import { cn } from "@/lib/utils";
import { formatShortDate } from "../projects/utils";
import { PosterStyleEditDialog } from "./PosterStyleEditDialog";

export interface PosterStyleDetailDrawerProps {
  itemId: string;
  onClose: () => void;
}

export function PosterStyleDetailDrawer({
  itemId,
  onClose,
}: PosterStyleDetailDrawerProps) {
  const detail = usePosterStyleQuery(itemId);
  const item = detail.data;
  const [activeSampleIndex, setActiveSampleIndex] = useState(0);
  const [editOpen, setEditOpen] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [copied, setCopied] = useState(false);

  const autoTag = useTriggerPosterStyleAutoTagMutation(itemId, {
    onSuccess: (data) =>
      toast.success("已重新识别", {
        description:
          data.style_tags.length > 0
            ? data.style_tags.join("、")
            : "未识别到明显风格特征",
      }),
    onError: (err) =>
      toast.error("识别失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const deleteItem = useDeletePosterStyleMutation({
    onSuccess: () => {
      toast.success("已从当前视图移除");
      onClose();
    },
    onError: (err) =>
      toast.error("移除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  // ESC 关闭 + body lock
  useBodyScrollLock(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // samples lightbox 适配
  const lightboxItems = useMemo<LightboxItem[]>(() => {
    if (!item) return [];
    const samples = item.samples.length > 0
      ? item.samples
      : [
          {
            index: 0,
            image_id: item.cover_image_id,
            image_url: item.cover_image_url,
            display_url: item.display_url,
            thumb_url: item.thumb_url,
          },
        ];
    return samples.map((sample) => ({
      id: `${item.id}#${sample.index}`,
      url: sample.image_url,
      thumbUrl: sample.thumb_url ?? undefined,
      previewUrl: sample.display_url ?? sample.image_url,
      prompt: item.title,
      filename: item.download_filename ?? undefined,
    }));
  }, [item]);

  const samples = item?.samples ?? [];
  const activeSample =
    samples.length > 0 ? samples[Math.min(activeSampleIndex, samples.length - 1)] : null;
  const previewUrl =
    activeSample?.display_url ||
    activeSample?.image_url ||
    item?.display_url ||
    item?.cover_image_url ||
    "";

  const requestDelete = () => {
    if (confirmingDelete) {
      deleteItem.mutate(itemId);
      setConfirmingDelete(false);
      return;
    }
    setConfirmingDelete(true);
    window.setTimeout(() => setConfirmingDelete(false), 3000);
  };

  const handleCopyPrompt = async () => {
    if (!item?.prompt_template) {
      toast.warning("当前风格没有 prompt 模板");
      return;
    }
    try {
      await navigator.clipboard.writeText(item.prompt_template);
      setCopied(true);
      toast.success("已复制 prompt 模板");
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("复制失败");
    }
  };

  const openLightbox = () => {
    if (lightboxItems.length === 0 || !activeSample) return;
    const initialId = `${itemId}#${activeSample.index}`;
    useUiStore.getState().openLightboxFromItems(lightboxItems, initialId, null);
  };

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex justify-end bg-black/60 backdrop-blur-md"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="风格详情"
        initial={{ x: "100%", opacity: 0.4 }}
        animate={{ x: 0, opacity: 1 }}
        exit={{ x: "100%", opacity: 0 }}
        transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-panel flex h-full w-full max-w-full flex-col overflow-hidden border-l border-[var(--border)] bg-[var(--bg-0)] md:max-w-xl"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div className="min-w-0">
            <p className="type-page-kicker">风格详情</p>
            <h3 className="type-page-title-sm mt-2 truncate">
              {item?.title || (detail.isPending ? "加载中" : "未找到")}
            </h3>
            {item ? (
              <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                {POSTER_STYLE_CATEGORY_LABEL[item.category]}
                {item.mood ? ` · ${item.mood}` : ""}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-11 w-11 shrink-0 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-9 md:w-9"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="mobile-dialog-scroll grid min-h-0 flex-1 gap-5 overflow-y-auto overscroll-contain px-5 py-5">
          {detail.isPending ? (
            <div className="flex h-40 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              <Spinner size={20} />
              加载中
            </div>
          ) : !item ? (
            <p className="border-y border-[var(--border)] py-12 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              该风格已不可用
            </p>
          ) : (
            <>
              {/* 大封面 */}
              <div className="grid gap-3">
                <button
                  type="button"
                  onClick={openLightbox}
                  className="relative aspect-square w-full cursor-zoom-in overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
                >
                  {previewUrl ? (
                    <Image
                      src={previewUrl}
                      alt={item.title}
                      fill
                      unoptimized
                      sizes="(max-width: 768px) 100vw, 540px"
                      className="object-cover"
                    />
                  ) : null}
                  <span className="pointer-events-none absolute bottom-2 right-2 inline-flex h-7 w-7 items-center justify-center rounded-full bg-black/60 text-white backdrop-blur">
                    <Maximize2 className="h-3.5 w-3.5" />
                  </span>
                </button>
                {samples.length > 1 ? (
                  <div className="grid grid-cols-5 gap-2">
                    {samples.map((sample, idx) => {
                      const active = idx === activeSampleIndex;
                      const thumb = sample.thumb_url || sample.image_url;
                      return (
                        <button
                          key={`${sample.index}-${idx}`}
                          type="button"
                          onClick={() => setActiveSampleIndex(idx)}
                          aria-label={`查看样图 ${idx + 1}`}
                          className={cn(
                            "relative aspect-square min-h-11 min-w-11 overflow-hidden rounded-[var(--radius-card)] border bg-[var(--bg-2)] transition-colors",
                            active
                              ? "border-[var(--border-amber)]"
                              : "border-[var(--border)] hover:border-[var(--border-strong)]",
                          )}
                        >
                          <Image
                            src={thumb}
                            alt={`样图 ${idx + 1}`}
                            fill
                            unoptimized
                            sizes="96px"
                            className="object-cover"
                          />
                        </button>
                      );
                    })}
                  </div>
                ) : null}
              </div>

              {/* 字段网格 */}
              <section className="grid grid-cols-1 gap-x-5 gap-y-3 border-t border-[var(--border)] pt-4 min-[380px]:grid-cols-2">
                <MetaCell label="来源">
                  {sourceLabel(item.source)}
                </MetaCell>
                <MetaCell label="类目">
                  {POSTER_STYLE_CATEGORY_LABEL[item.category]}
                </MetaCell>
                {item.preset_id ? (
                  <MetaCell label="预设 ID">
                    <span className="block truncate font-mono text-[11px] normal-case">
                      {item.preset_id}
                      {item.version ? ` · v${item.version}` : ""}
                    </span>
                  </MetaCell>
                ) : null}
                {item.auto_tagged_at ? (
                  <MetaCell label="打标时间">
                    {formatShortDate(item.auto_tagged_at)}
                  </MetaCell>
                ) : null}
                <MetaCell label="创建">
                  {formatShortDate(item.created_at)}
                </MetaCell>
                {item.recommended_aspects.length > 0 ? (
                  <MetaCell label="推荐尺寸">
                    {item.recommended_aspects.join(" · ")}
                  </MetaCell>
                ) : null}
              </section>

              {/* style_tags */}
              {item.style_tags.length > 0 ? (
                <section className="grid gap-2 border-t border-[var(--border)] pt-4">
                  <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    风格标签
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {item.style_tags.map((tag) => (
                      <span
                        key={tag}
                        className="inline-flex max-w-full items-center break-words border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-1)] min-[390px]:tracking-[0.14em]"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </section>
              ) : null}

              {/* palette */}
              {item.palette.length > 0 ? (
                <section className="grid gap-2 border-t border-[var(--border)] pt-4">
                  <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    色板
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    {item.palette.map((hex, idx) => (
                      <div
                        key={`${hex}-${idx}`}
                        className="flex items-center gap-2"
                      >
                        <span
                          aria-hidden
                          title={hex}
                          className="h-5 w-5 rounded-[var(--radius-card)] border border-[var(--border)]"
                          style={{ backgroundColor: hex }}
                        />
                        <span className="font-mono text-[11px] uppercase text-[var(--fg-2)]">
                          {hex}
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}

              {/* prompt_template */}
              {item.prompt_template ? (
                <section className="grid gap-2 border-t border-[var(--border)] pt-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                      Prompt 模板
                    </p>
                    <button
                      type="button"
                      onClick={handleCopyPrompt}
                      className="inline-flex min-h-11 items-center gap-1.5 px-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:text-[var(--amber-300)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:h-7 md:min-h-0"
                    >
                      {copied ? (
                        <Check className="h-3 w-3 text-[var(--success)]" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                      {copied ? "已复制" : "复制"}
                    </button>
                  </div>
                  <p className="whitespace-pre-wrap break-words rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-[13px] leading-relaxed text-[var(--fg-1)]">
                    {item.prompt_template}
                  </p>
                </section>
              ) : null}
            </>
          )}
        </div>

        {item ? (
          <PosterStyleDetailActions
            item={item}
            autoTagPending={autoTag.isPending}
            confirmingDelete={confirmingDelete}
            deleting={deleteItem.isPending}
            onAutoTag={() => autoTag.mutate()}
            onDelete={requestDelete}
            onEdit={() => setEditOpen(true)}
          />
        ) : null}
      </motion.div>

      {editOpen && item ? (
        <PosterStyleEditDialog
          item={item}
          onClose={() => setEditOpen(false)}
        />
      ) : null}
    </div>
  );
}

function PosterStyleDetailActions({
  item,
  autoTagPending,
  confirmingDelete,
  deleting,
  onAutoTag,
  onDelete,
  onEdit,
}: {
  item: PosterStyleItem;
  autoTagPending: boolean;
  confirmingDelete: boolean;
  deleting: boolean;
  onAutoTag: () => void;
  onDelete: () => void;
  onEdit: () => void;
}) {
  const isUserItem = item.id.startsWith("user:");
  const deleteLabel = confirmingDelete
    ? "确认"
    : item.source === "preset"
      ? "隐藏预设"
      : "删除";

  return (
    <footer className="mobile-dialog-footer grid shrink-0 grid-cols-1 gap-2 border-t border-[var(--border)] px-5 py-4 min-[380px]:grid-cols-2 md:flex md:items-center md:justify-end">
      <button
        type="button"
        onClick={onAutoTag}
        disabled={autoTagPending}
        className="inline-flex min-h-11 min-w-0 items-center justify-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[11px] uppercase tracking-[0.12em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--amber-300)] disabled:cursor-not-allowed disabled:opacity-50 min-[390px]:tracking-[0.16em] md:h-9 md:min-h-0"
      >
        {autoTagPending ? <Spinner size={12} /> : <Sparkles className="h-3.5 w-3.5" />}
        重新识别
      </button>
      {isUserItem ? (
        <Button
          variant="outline"
          onClick={onEdit}
          leftIcon={<Edit3 className="h-3.5 w-3.5" />}
          className="w-full md:w-auto"
        >
          编辑
        </Button>
      ) : null}
      <button
        type="button"
        onClick={onDelete}
        disabled={deleting}
        className={cn(
          "inline-flex min-h-11 min-w-0 items-center justify-center gap-1.5 border px-3 font-mono text-[11px] uppercase tracking-[0.12em] transition-colors disabled:cursor-not-allowed disabled:opacity-50 min-[390px]:tracking-[0.16em] md:h-9 md:min-h-0",
          isUserItem ? "min-[380px]:col-span-2 md:col-span-1" : "",
          confirmingDelete
            ? "border-[var(--danger)] text-[var(--danger)]"
            : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)] hover:text-[var(--danger)]",
        )}
      >
        {deleting ? <Spinner size={12} /> : <Trash2 className="h-3.5 w-3.5" />}
        {deleteLabel}
      </button>
    </footer>
  );
}

function MetaCell({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid min-w-0 gap-1">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        {label}
      </p>
      <p className="min-w-0 break-words font-mono text-[12px] text-[var(--fg-0)]">{children}</p>
    </div>
  );
}

function sourceLabel(source: string): string {
  switch (source) {
    case "preset":
      return "预设";
    case "favorite":
      return "收藏";
    case "user_upload":
      return "上传";
    case "generated":
      return "生成";
    default:
      return source;
  }
}
