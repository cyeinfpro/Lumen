"use client";

import { Inbox, Plus, Search } from "lucide-react";
import type { RefObject } from "react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { Spinner } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";

import {
  SKELETON_ROWS,
  type TabKind,
} from "./mobileConversationDrawerModel";

export function DrawerErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="mx-4 my-4 px-3 py-3 rounded-[var(--radius-card)] bg-danger-soft border border-danger-border text-[12.5px] text-danger">
      加载失败
      <Pressable
        size="inline"
        minHit
        pressScale="tight"
        haptic="light"
        onPress={onRetry}
        className="ml-1 min-h-11 px-2 underline"
      >
        {copy.action.retry}
      </Pressable>
    </div>
  );
}

export function DrawerPagination({
  sentinelRef,
  hasNextPage,
  hasError,
  loading,
  onLoadMore,
}: {
  sentinelRef: RefObject<HTMLDivElement | null>;
  hasNextPage: boolean;
  hasError: boolean;
  loading: boolean;
  onLoadMore: () => void;
}) {
  if (!hasNextPage) return null;

  return (
    <div
      ref={sentinelRef}
      className="flex items-center justify-center py-5"
    >
      {hasError ? (
        <div role="alert">
          <Pressable
            size="default"
            minHit
            pressScale="soft"
            haptic="light"
            onPress={onLoadMore}
            disabled={loading}
            className="rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-4 text-[12px] text-danger"
          >
            {loading ? "重试中" : "加载失败，点击重试"}
          </Pressable>
        </div>
      ) : loading ? (
        <Spinner />
      ) : null}
    </div>
  );
}

export function ListSkeleton() {
  return (
    <ul aria-hidden>
      {SKELETON_ROWS.map((row) => (
        <li
          key={row.id}
          className="flex items-center gap-3.5 min-h-[68px] pl-4 pr-3 border-b border-[var(--border-subtle)]"
        >
          <div className="w-11 h-11 rounded-[var(--radius-panel)] bg-[var(--bg-2)] animate-pulse" />
          <div className="flex-1 space-y-1.5">
            <div
              className="h-3 rounded bg-[var(--bg-2)] animate-pulse"
              style={{ width: `${row.titleWidth}%` }}
            />
            <div className="h-2.5 w-24 rounded bg-[var(--bg-2)] animate-pulse" />
          </div>
        </li>
      ))}
    </ul>
  );
}

export function EmptyState({
  query,
  tab,
  onClearQuery,
  onCreate,
}: {
  query: string;
  tab: TabKind;
  onClearQuery: () => void;
  onCreate: () => void;
}) {
  if (query) {
    return (
      <div className="px-6 py-12 text-center">
        <div className="mx-auto w-12 h-12 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Search className="w-5 h-5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">{copy.state.noResult}</p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          换个关键词试试
        </p>
        <Pressable
          size="default"
          minHit
          pressScale="soft"
          haptic="light"
          onPress={onClearQuery}
          className="mt-3 text-[12.5px] text-[var(--amber-400)]"
        >
          清除搜索
        </Pressable>
      </div>
    );
  }

  if (tab === "archived") {
    return (
      <div className="px-6 py-12 text-center">
        <div className="mx-auto w-12 h-12 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Inbox className="w-5 h-5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">{copy.state.empty}</p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          长按或左滑会话可归档
        </p>
      </div>
    );
  }

  return (
    <div className="px-6 py-12 text-center">
      <div className="mx-auto w-12 h-12 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] flex items-center justify-center mb-3">
        <LumenMark className="h-5 w-5 text-[var(--accent)]" />
      </div>
      <p className="text-[14px] text-[var(--fg-1)]">还没有会话</p>
      <p className="text-[12px] text-[var(--fg-2)] mt-1 mb-4">
        从这里开始你的第一次对话
      </p>
      <Pressable
        size="default"
        minHit
        pressScale="soft"
        haptic="medium"
        onPress={onCreate}
        className={cn(
          "inline-flex items-center gap-1.5 h-9 px-4 rounded-full",
          "bg-[var(--amber-400)] text-black text-[13px] font-medium",
        )}
      >
        <Plus className="w-3.5 h-3.5" />
        新建会话
      </Pressable>
    </div>
  );
}
