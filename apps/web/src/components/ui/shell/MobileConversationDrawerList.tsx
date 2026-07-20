"use client";

import type { RefObject } from "react";

import { ConversationRowMobile } from "@/components/ui/me/ConversationRowMobile";
import type { ConversationSummary } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

import {
  BUCKET_LABEL,
  BUCKET_ORDER,
  type ConversationDrawerGroups,
  type TabKind,
} from "./mobileConversationDrawerModel";
import {
  DrawerErrorState,
  DrawerPagination,
  EmptyState,
  ListSkeleton,
} from "./MobileConversationDrawerStates";

export interface MobileConversationDrawerListProps {
  listScrollRef: RefObject<HTMLDivElement | null>;
  sentinelRef: RefObject<HTMLDivElement | null>;
  isInitialLoading: boolean;
  isError: boolean;
  isFetchingNextPage: boolean;
  isFetchNextPageError: boolean;
  hasNextPage: boolean;
  hasResults: boolean;
  query: string;
  tab: TabKind;
  filtered: ConversationSummary[];
  grouped: ConversationDrawerGroups;
  currentConvId: string | null;
  onRetry: () => void;
  onLoadMore: () => void;
  onClearQuery: () => void;
  onCreate: () => void;
  onSelect: (conversation: ConversationSummary) => void;
  onRename: (conversation: ConversationSummary, title: string) => void;
  onArchive: (conversation: ConversationSummary) => void;
  onDelete: (conversation: ConversationSummary) => void;
}

interface ConversationDrawerRowsProps {
  items: ConversationSummary[];
  currentConvId: string | null;
  onSelect: (conversation: ConversationSummary) => void;
  onRename: (conversation: ConversationSummary, title: string) => void;
  onArchive: (conversation: ConversationSummary) => void;
  onDelete: (conversation: ConversationSummary) => void;
}

function ConversationDrawerRows({
  items,
  currentConvId,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: ConversationDrawerRowsProps) {
  return (
    <ul>
      {items.map((conversation) => (
        <li key={conversation.id}>
          <ConversationRowMobile
            conv={conversation}
            active={conversation.id === currentConvId}
            onSelect={() => onSelect(conversation)}
            onRename={(title) => onRename(conversation, title)}
            onArchive={() => onArchive(conversation)}
            onDelete={() => onDelete(conversation)}
          />
        </li>
      ))}
    </ul>
  );
}

function ActiveConversationGroups({
  grouped,
  currentConvId,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: Omit<ConversationDrawerRowsProps, "items"> & {
  grouped: ConversationDrawerGroups;
}) {
  return (
    <>
      {BUCKET_ORDER.map((bucket) => {
        const items = grouped[bucket];
        if (items.length === 0) return null;
        return (
          <section key={bucket} aria-label={BUCKET_LABEL[bucket]}>
            <h3
              className={cn(
                "px-4 pt-5 pb-2 text-[11px] font-semibold",
                "tracking-[0.1em] uppercase text-[var(--fg-2)]",
              )}
            >
              {BUCKET_LABEL[bucket]}
            </h3>
            <ConversationDrawerRows
              items={items}
              currentConvId={currentConvId}
              onSelect={onSelect}
              onRename={onRename}
              onArchive={onArchive}
              onDelete={onDelete}
            />
          </section>
        );
      })}
    </>
  );
}

export function MobileConversationDrawerList({
  listScrollRef,
  sentinelRef,
  isInitialLoading,
  isError,
  isFetchingNextPage,
  isFetchNextPageError,
  hasNextPage,
  hasResults,
  query,
  tab,
  filtered,
  grouped,
  currentConvId,
  onRetry,
  onLoadMore,
  onClearQuery,
  onCreate,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: MobileConversationDrawerListProps) {
  return (
    <div
      ref={listScrollRef}
      data-app-scroll
      aria-busy={isInitialLoading || isFetchingNextPage}
      className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain touch-pan-y [scrollbar-gutter:stable]"
    >
      {isInitialLoading && <ListSkeleton />}

      {!isInitialLoading && isError && (
        <DrawerErrorState onRetry={onRetry} />
      )}

      {!isInitialLoading && !isError && !hasResults && (
        <EmptyState
          query={query}
          tab={tab}
          onClearQuery={onClearQuery}
          onCreate={onCreate}
        />
      )}

      {tab === "archived" && hasResults && (
        <ConversationDrawerRows
          items={filtered}
          currentConvId={currentConvId}
          onSelect={onSelect}
          onRename={onRename}
          onArchive={onArchive}
          onDelete={onDelete}
        />
      )}

      {tab === "active" && hasResults && (
        <ActiveConversationGroups
          grouped={grouped}
          currentConvId={currentConvId}
          onSelect={onSelect}
          onRename={onRename}
          onArchive={onArchive}
          onDelete={onDelete}
        />
      )}

      <DrawerPagination
        sentinelRef={sentinelRef}
        hasNextPage={hasNextPage}
        hasError={isFetchNextPageError}
        loading={isFetchingNextPage}
        onLoadMore={onLoadMore}
      />

      <div className="h-3 shrink-0" />
    </div>
  );
}
