"use client";

// 风格库选择器（创建海报项目时使用）。
// - 模态对话框 / 抽屉
// - tabs 切换 category（all/illustration/3d/minimal/retro/traditional/photo/other）
// - 卡片网格，每个 cover + title + mood + tags
// - 选中后 onSelect(styleId)
//
// 数据来源：usePosterStylesQuery（query.ts 已暴露）；只读，不创建/编辑风格。

import { Check, Search, X } from "lucide-react";
import Image from "next/image";
import { useMemo, useState } from "react";

import { Spinner } from "@/components/ui/primitives/Spinner";
import { usePosterStylesQuery } from "@/lib/queries";
import {
  POSTER_STYLE_CATEGORY_LABEL,
  type PosterStyleCategoryFilter,
  type PosterStyleItem,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";

const CATEGORY_TABS: PosterStyleCategoryFilter[] = [
  "all",
  "illustration",
  "3d",
  "minimal",
  "retro",
  "traditional",
  "photo",
  "other",
];

export interface PosterStyleSelectorProps {
  open: boolean;
  onClose: () => void;
  selectedId?: string | null;
  onSelect: (style: PosterStyleItem) => void;
}

export function PosterStyleSelector({
  open,
  onClose,
  selectedId,
  onSelect,
}: PosterStyleSelectorProps) {
  const [category, setCategory] = useState<PosterStyleCategoryFilter>("all");
  const [search, setSearch] = useState("");

  const query = usePosterStylesQuery(
    {
      category,
      q: search.trim() || undefined,
      limit: 60,
    },
    { enabled: open },
  );

  const items = useMemo(() => query.data?.items ?? [], [query.data?.items]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="选择风格"
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-stretch justify-center bg-black/65 backdrop-blur-sm md:items-center"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="mobile-dialog-panel relative flex h-[var(--mobile-dialog-max-height)] w-full max-w-[1100px] flex-col overflow-hidden bg-[var(--bg-0)] shadow-[var(--shadow-2)] max-md:rounded-t-[var(--radius-sheet)] md:h-[min(86vh,720px)] md:rounded-[var(--radius-card)] md:border md:border-[var(--border)]">
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Poster Style
            </p>
            <h2 className="type-section-title mt-1">选择风格</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-9 w-9 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="shrink-0 border-b border-[var(--border)] px-5 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <label className="relative flex-1 min-w-[180px]">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-3)]" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value.slice(0, 60))}
                placeholder="搜索风格标题 / 标签"
                className="h-9 w-full border-b border-[var(--border)] bg-transparent pl-7 pr-2 text-[13px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
              />
            </label>
            <div className="scrollbar-none flex max-w-full gap-1 overflow-x-auto">
              {CATEGORY_TABS.map((cat) => (
                <button
                  key={cat}
                  type="button"
                  onClick={() => setCategory(cat)}
                  className={cn(
                    "inline-flex min-h-8 shrink-0 cursor-pointer items-center px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                    category === cat
                      ? "border-b border-[var(--amber-400)] text-[var(--amber-300)]"
                      : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  )}
                >
                  {POSTER_STYLE_CATEGORY_LABEL[cat]}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {query.isLoading ? (
            <div className="flex h-64 flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
              <Spinner size={20} />
              <p className="font-mono text-[10px] uppercase tracking-[0.18em]">
                加载中
              </p>
            </div>
          ) : !items.length ? (
            <div className="flex h-64 flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em]">
                暂无风格
              </p>
              <p className="text-[12px] text-[var(--fg-3)]">
                可在「风格库」页面创建或同步预设。
              </p>
            </div>
          ) : (
            <ul className="grid grid-cols-2 gap-x-4 gap-y-6 md:grid-cols-3 xl:grid-cols-4">
              {items.map((style) => (
                <StyleCard
                  key={style.id}
                  style={style}
                  selected={selectedId === style.id}
                  onSelect={() => onSelect(style)}
                />
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function StyleCard({
  style,
  selected,
  onSelect,
}: {
  style: PosterStyleItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const coverUrl = style.display_url || style.cover_image_url || style.thumb_url || "";
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        className={cn(
          "group relative block w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] text-left transition-shadow duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          selected ? "ring-1 ring-inset ring-[var(--border-amber)]" : "",
        )}
      >
        <div className="relative aspect-[4/5] w-full overflow-hidden">
          {coverUrl ? (
            <Image
              src={coverUrl}
              alt={style.title}
              fill
              sizes="(max-width: 768px) 50vw, 240px"
              unoptimized
              className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
            />
          ) : (
            <div className="flex h-full items-center justify-center font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              暂无封面
            </div>
          )}
          {selected ? (
            <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-[var(--amber-400)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--accent-on)] shadow-[var(--shadow-amber)]">
              <Check className="h-3 w-3" />
              已选
            </span>
          ) : null}
        </div>
        <div className="border-b border-[var(--border)] px-1 py-2">
          <p className="line-clamp-1 text-[13px] font-medium tracking-tight text-[var(--fg-0)]">
            {style.title}
          </p>
          {style.mood ? (
            <p className="mt-1 line-clamp-1 text-[12px] text-[var(--fg-2)]">
              {style.mood}
            </p>
          ) : null}
        </div>
      </button>
    </li>
  );
}
