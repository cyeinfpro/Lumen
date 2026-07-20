"use client";

import { Loader2, Plus, Search, X } from "lucide-react";
import type { RefObject } from "react";

import { SegmentedControl } from "@/components/ui/primitives/mobile";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { LumenMark } from "@/components/ui/brand/LumenMark";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";

import type { TabKind } from "./mobileConversationDrawerModel";
import {
  MobileConversationDrawerList,
  type MobileConversationDrawerListProps,
} from "./MobileConversationDrawerList";

export interface MobileConversationDrawerViewProps
  extends MobileConversationDrawerListProps {
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  activeTotal: number;
  archivedTotal: number;
  createPending: boolean;
  onClose: () => void;
  onQueryChange: (query: string) => void;
  onTabChange: (tab: TabKind) => void;
}

export function MobileConversationDrawerView({
  closeButtonRef,
  listScrollRef,
  activeTotal,
  archivedTotal,
  createPending,
  onClose,
  onQueryChange,
  onTabChange,
  ...listProps
}: MobileConversationDrawerViewProps) {
  return (
    <>
      <div className="flex shrink-0 items-center justify-between px-4 pt-3 pb-2">
        <div className="flex items-center gap-2 min-w-0">
          <LumenMark className="h-6 w-6 text-[var(--accent)]" />
          <span className="text-[16px] font-semibold tracking-tight text-[var(--fg-0)]">
            会话
          </span>
          <span className="ml-1.5 text-[12px] font-mono text-[var(--fg-2)]">
            {activeTotal + archivedTotal || ""}
          </span>
        </div>
        <Pressable
          ref={closeButtonRef}
          size="default"
          minHit
          pressScale="tight"
          haptic="light"
          onPress={onClose}
          aria-label={copy.action.close}
          className="h-11 w-11 rounded-full text-[var(--fg-1)]"
        >
          <X className="w-[18px] h-[18px]" />
        </Pressable>
      </div>

      <div className="shrink-0 px-4 pb-3">
        <Pressable
          size="default"
          minHit
          pressScale="soft"
          haptic="medium"
          onPress={listProps.onCreate}
          disabled={createPending}
          className={cn(
            "h-12 w-full gap-2 rounded-[var(--radius-card)]",
            "bg-[var(--accent)] text-[var(--accent-on)] text-[15px] font-medium",
            "shadow-[var(--shadow-1)]",
            "disabled:opacity-60 disabled:cursor-wait",
          )}
        >
          {createPending ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Plus className="w-[18px] h-[18px]" strokeWidth={2.4} />
          )}
          {createPending ? "新建中" : "新建会话"}
        </Pressable>
      </div>

      <div className="shrink-0 px-4 pb-3">
        <div
          className={cn(
            "flex min-h-11 items-center gap-2 px-3 rounded-full",
            "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
            "focus-within:border-[var(--amber-400)]/50",
            "transition-colors",
          )}
        >
          <Search className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
          <input
            value={listProps.query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="搜索会话标题"
            aria-label="搜索会话"
            className={cn(
              "min-w-0 flex-1 bg-transparent text-[14px] text-[var(--fg-0)]",
              "placeholder:text-[var(--fg-2)] outline-none",
            )}
          />
          {listProps.query && (
            <Pressable
              size="inline"
              minHit
              pressScale="tight"
              haptic="light"
              onPress={listProps.onClearQuery}
              aria-label="清除搜索"
              className="-mr-2 inline-flex h-11 w-11 items-center justify-center rounded-full text-[var(--fg-2)]"
            >
              <X className="w-3 h-3" />
            </Pressable>
          )}
        </div>
      </div>

      <div className="shrink-0 px-4 pb-2">
        <SegmentedControl<TabKind>
          value={listProps.tab}
          onChange={onTabChange}
          ariaLabel="会话类型"
          items={[
            {
              value: "active",
              label: "对话",
              badge: activeTotal || undefined,
            },
            {
              value: "archived",
              label: "归档",
              badge: archivedTotal || undefined,
            },
          ]}
        />
      </div>

      <MobileConversationDrawerList
        listScrollRef={listScrollRef}
        {...listProps}
      />
    </>
  );
}
