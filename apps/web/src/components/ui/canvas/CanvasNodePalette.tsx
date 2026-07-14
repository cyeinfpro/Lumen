"use client";

import {
  GripVertical,
  MousePointerClick,
  Search,
  SearchX,
  X,
} from "lucide-react";
import { useId, useRef, useState } from "react";

import {
  CANVAS_NODE_SPECS,
  CANVAS_NODE_TYPES,
} from "@/lib/canvas/registry";
import type { CanvasNodeType } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";

type NodeGroupId = "input" | "generate" | "organize" | "deliver";

const NODE_GROUPS: Array<{
  id: NodeGroupId;
  label: string;
  iconClassName: string;
  markerClassName: string;
}> = [
  {
    id: "input",
    label: "输入",
    iconClassName: "bg-[var(--info-soft)] text-[var(--info-fg)]",
    markerClassName: "bg-[var(--info)]",
  },
  {
    id: "generate",
    label: "生成",
    iconClassName: "bg-[var(--accent-soft)] text-[var(--accent)]",
    markerClassName: "bg-[var(--accent)]",
  },
  {
    id: "organize",
    label: "组织",
    iconClassName: "bg-[var(--bg-3)] text-[var(--fg-1)]",
    markerClassName: "bg-[var(--fg-2)]",
  },
  {
    id: "deliver",
    label: "交付",
    iconClassName: "bg-[var(--success-soft)] text-[var(--success-fg)]",
    markerClassName: "bg-[var(--success)]",
  },
];

const NODE_GROUP_BY_TYPE: Record<CanvasNodeType, NodeGroupId> = {
  prompt: "input",
  image_asset: "input",
  video_asset: "input",
  image_generate: "generate",
  video_generate: "generate",
  note: "organize",
  frame: "organize",
  delivery: "deliver",
};

export function CanvasNodePalette({
  onAdd,
  compact = false,
}: {
  onAdd: (type: CanvasNodeType) => void;
  compact?: boolean;
}) {
  const searchId = useId();
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleGroups = NODE_GROUPS.map((group) => ({
    ...group,
    types: CANVAS_NODE_TYPES.filter((type) => {
      if (NODE_GROUP_BY_TYPE[type] !== group.id) return false;
      if (!normalizedQuery) return true;
      const spec = CANVAS_NODE_SPECS[type];
      return [group.label, spec.label, spec.description, type]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalizedQuery);
    }),
  })).filter((group) => group.types.length > 0);
  const resultCount = visibleGroups.reduce(
    (count, group) => count + group.types.length,
    0,
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
              compact ? "h-11 text-base" : "h-10 type-body-sm",
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
                compact ? "h-11 w-11" : "h-9 w-9",
              )}
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>
        <p className="flex items-center gap-1.5 px-0.5 type-caption text-[var(--fg-2)]">
          {compact ? (
            <MousePointerClick aria-hidden="true" className="h-3.5 w-3.5" />
          ) : (
            <GripVertical aria-hidden="true" className="h-3.5 w-3.5" />
          )}
          {compact ? "点击节点添加到画布" : "拖拽节点到画布，或点击添加"}
        </p>
        <span className="sr-only" role="status" aria-live="polite">
          {query
            ? resultCount > 0
              ? `找到 ${resultCount} 个节点`
              : "没有匹配节点"
            : ""}
        </span>
      </div>

      {visibleGroups.length > 0 ? (
        visibleGroups.map((group) => (
          <section
            key={group.id}
            aria-labelledby={`${searchId}-${group.id}`}
            className="grid gap-1.5"
          >
            <div className="flex items-center gap-2 px-1">
              <span
                aria-hidden="true"
                className={cn("h-1.5 w-1.5 rounded-full", group.markerClassName)}
              />
              <h3
                id={`${searchId}-${group.id}`}
                className="type-caption font-medium text-[var(--fg-1)]"
              >
                {group.label}
              </h3>
              <span className="type-caption tabular-nums text-[var(--fg-3)]">
                {group.types.length}
              </span>
            </div>
            <div className={compact ? "grid grid-cols-2 gap-2" : "grid gap-1"}>
              {group.types.map((type) => {
                const spec = CANVAS_NODE_SPECS[type];
                const Icon = spec.icon;
                return (
                  <button
                    key={type}
                    type="button"
                    draggable={!compact}
                    aria-label={`添加${spec.label}节点`}
                    onDragStart={(event) => {
                      event.dataTransfer.setData(
                        "application/lumen-canvas-node",
                        type,
                      );
                      event.dataTransfer.effectAllowed = "copy";
                    }}
                    onClick={() => onAdd(type)}
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
                        group.iconClassName,
                      )}
                    >
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
                        {spec.label}
                      </span>
                      <span className="block truncate type-caption text-[var(--fg-2)]">
                        {spec.description}
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
          </section>
        ))
      ) : (
        <div
          className="grid min-h-36 place-items-center rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 px-4 py-6 text-center"
        >
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
