"use client";

import {
  GripVertical,
  MousePointerClick,
  Search,
  SearchX,
  X,
} from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import {
  CANVAS_NODE_CATALOG,
  CANVAS_NODE_SPECS,
  type CanvasNodeCatalogId,
  type CanvasNodeCategory,
} from "@/lib/canvas/registry";
import { cn } from "@/lib/utils";

type PaletteCategory = CanvasNodeCategory | "all";

const PALETTE_CATEGORIES: Array<{
  id: PaletteCategory;
  label: string;
}> = [
  { id: "all", label: "全部" },
  { id: "input", label: "素材" },
  { id: "text", label: "文本" },
  { id: "image", label: "图片" },
  { id: "video", label: "视频" },
  { id: "organize", label: "组织" },
  { id: "deliver", label: "交付" },
];

const CATEGORY_ICON_CLASS: Record<CanvasNodeCategory, string> = {
  input: "bg-[var(--info-soft)] text-[var(--info-fg)]",
  text: "bg-[var(--bg-3)] text-[var(--fg-1)]",
  image: "bg-[var(--accent-soft)] text-[var(--accent)]",
  video: "bg-[var(--success-soft)] text-[var(--success-fg)]",
  organize: "bg-[var(--bg-2)] text-[var(--fg-1)]",
  deliver: "bg-[var(--success-soft)] text-[var(--success-fg)]",
};

export function CanvasNodePalette({
  onAdd,
  compact = false,
}: {
  onAdd: (catalogId: CanvasNodeCatalogId) => void;
  compact?: boolean;
}) {
  const searchId = useId();
  const categoryTabsId = useId();
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<PaletteCategory>("all");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleItems = useMemo(
    () =>
      CANVAS_NODE_CATALOG.filter((item) => {
        if (category !== "all" && item.category !== category) return false;
        if (!normalizedQuery) return true;
        const spec = CANVAS_NODE_SPECS[item.type];
        return [
          item.label,
          item.description,
          item.id,
          item.type,
          ...item.keywords,
          ...spec.keywords,
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(normalizedQuery);
      }),
    [category, normalizedQuery],
  );
  const clearSearch = () => {
    setQuery("");
    searchInputRef.current?.focus();
  };

  return (
    <div className="grid gap-3">
      <div className="grid gap-2">
        <label htmlFor={searchId} className="sr-only">
          搜索节点
        </label>
        <div className="relative">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]"
          />
          <input
            ref={searchInputRef}
            id={searchId}
            type="search"
            inputMode="search"
            value={query}
            placeholder="搜索节点"
            autoComplete="off"
            onChange={(event) => setQuery(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape" && query) {
                event.preventDefault();
                event.stopPropagation();
                setQuery("");
              }
            }}
            className={cn(
              "w-full appearance-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-11 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)]",
              "transition-[border-color,background-color,box-shadow] duration-[var(--dur-fast)] ease-[var(--ease-develop)]",
              "focus:border-[var(--border-strong)] focus:shadow-[var(--ring)]",
              "[&::-webkit-search-cancel-button]:appearance-none",
              compact ? "min-h-11 text-base" : "min-h-11 type-body-sm",
            )}
          />
          {query ? (
            <button
              type="button"
              aria-label="清空节点搜索"
              title="清空搜索"
              onClick={clearSearch}
              data-lumen-interactive="true"
              className={cn(
                "absolute right-0 top-1/2 inline-flex -translate-y-1/2 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)]",
                "transition-[background-color,color] duration-[var(--dur-fast)] ease-[var(--ease-develop)]",
                "hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                "h-11 w-11",
              )}
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>
        <div
          id={categoryTabsId}
          role="tablist"
          aria-label="节点类别"
          className="flex gap-1 overflow-x-auto pb-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {PALETTE_CATEGORIES.map((item) => {
            const selected = category === item.id;
            return (
              <button
                key={item.id}
                id={`${categoryTabsId}-${item.id}`}
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => setCategory(item.id)}
                data-lumen-interactive="true"
                className={cn(
                  "min-h-11 shrink-0 rounded-[var(--radius-control)] px-2.5 type-caption font-medium",
                  "transition-[background-color,color,box-shadow] duration-[var(--dur-fast)] ease-[var(--ease-develop)]",
                  "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                  selected
                    ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                    : "text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                )}
              >
                {item.label}
              </button>
            );
          })}
        </div>
        <div className="flex items-center justify-between gap-2 px-0.5 type-caption text-[var(--fg-2)]">
          <span className="inline-flex min-w-0 items-center gap-1.5">
            {compact ? (
              <MousePointerClick aria-hidden="true" className="h-3.5 w-3.5" />
            ) : (
              <GripVertical aria-hidden="true" className="h-3.5 w-3.5" />
            )}
            {compact ? "点击添加" : "拖拽或点击添加"}
          </span>
          <span className="shrink-0 tabular-nums">{visibleItems.length} 项</span>
        </div>
        <span className="sr-only" role="status" aria-live="polite">
          {visibleItems.length > 0
            ? `找到 ${visibleItems.length} 个节点`
            : "没有匹配节点"}
        </span>
      </div>

      {visibleItems.length > 0 ? (
        <div
          role="tabpanel"
          aria-labelledby={`${categoryTabsId}-${category}`}
          className={
            compact
              ? "grid grid-cols-1 gap-2 min-[520px]:grid-cols-2"
              : "grid gap-1"
          }
        >
          {visibleItems.map((item) => {
            const spec = CANVAS_NODE_SPECS[item.type];
            const Icon = spec.icon;
            return (
              <button
                key={item.id}
                type="button"
                draggable={!compact}
                aria-label={`添加${item.label}节点`}
                onDragStart={(event) => {
                  event.dataTransfer.setData(
                    "application/lumen-canvas-node",
                    item.id,
                  );
                  event.dataTransfer.effectAllowed = "copy";
                }}
                onClick={() => onAdd(item.id)}
                data-lumen-interactive="true"
                className={cn(
                  "group flex w-full items-center gap-3 rounded-[var(--radius-control)] border border-transparent px-2.5 text-left",
                  "transition-[background-color,border-color,color] duration-[var(--dur-fast)] ease-[var(--ease-develop)]",
                  "hover:border-[var(--border)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                  compact ? "min-h-14" : "min-h-11",
                )}
              >
                <span
                  className={cn(
                    "grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-control)]",
                    CATEGORY_ICON_CLASS[item.category],
                  )}
                >
                  <Icon className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
                    {item.label}
                  </span>
                  <span className="block truncate type-caption text-[var(--fg-2)]">
                    {item.description}
                  </span>
                </span>
                {!compact ? (
                  <GripVertical
                    aria-hidden="true"
                    className="h-4 w-4 shrink-0 text-[var(--fg-3)] opacity-50 transition-opacity duration-[var(--dur-fast)] ease-[var(--ease-develop)] group-hover:opacity-100"
                  />
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="grid min-h-36 place-items-center rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 px-4 py-6 text-center">
          <div className="grid justify-items-center gap-2">
            <span className="grid h-10 w-10 place-items-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--fg-2)]">
              <SearchX className="h-5 w-5" />
            </span>
            <div>
              <p className="type-body-sm font-medium text-[var(--fg-0)]">
                没有匹配节点
              </p>
              <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                换个关键词，或清空当前搜索
              </p>
            </div>
            <button
              type="button"
              onClick={clearSearch}
              data-lumen-interactive="true"
              className="mt-1 inline-flex min-h-11 items-center gap-1.5 rounded-[var(--radius-control)] px-3 type-body-sm font-medium text-[var(--accent)] transition-[background-color,color] duration-[var(--dur-fast)] ease-[var(--ease-develop)] hover:bg-[var(--accent-soft)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
            >
              <X className="h-4 w-4" />
              清空搜索
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
