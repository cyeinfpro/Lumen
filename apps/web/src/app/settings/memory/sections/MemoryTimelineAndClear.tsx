import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/primitives";

import { EmptyBlock, LoadingBlock } from "../memoryStateBlocks";
import { formatTime } from "../memoryPageUtils";
import type { MemoryTimelineEvent } from "../types";
import { SectionHeader } from "./MemorySectionPrimitives";

export function MemoryTimelineAndClear({
  events,
  timelinePending,
  clearText,
  clearing,
  onClearTextChange,
  onClear,
}: {
  events: MemoryTimelineEvent[];
  timelinePending: boolean;
  clearText: string;
  clearing: boolean;
  onClearTextChange: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
      <MemoryTimelinePanel events={events} pending={timelinePending} />
      <MemoryClearPanel
        clearText={clearText}
        clearing={clearing}
        onClearTextChange={onClearTextChange}
        onClear={onClear}
      />
    </section>
  );
}

function MemoryTimelinePanel({
  events,
  pending,
}: {
  events: MemoryTimelineEvent[];
  pending: boolean;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader title="最近变化" />
      <MemoryTimelineList events={events} pending={pending} />
    </div>
  );
}

function MemoryTimelineList({
  events,
  pending,
}: {
  events: MemoryTimelineEvent[];
  pending: boolean;
}) {
  if (pending) return <LoadingBlock />;
  if (events.length === 0) {
    return <EmptyBlock text="还没有审计事件。" />;
  }
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {events.map((event) => (
        <div
          key={event.id}
          className="grid gap-1 p-4 sm:grid-cols-[100px_minmax(0,1fr)]"
        >
          <span className="type-caption text-[var(--fg-2)]">
            {formatTime(event.created_at)}
          </span>
          <div className="min-w-0">
            <div className="type-caption font-mono text-[var(--fg-1)]">
              {event.event_type}
            </div>
            <div className="mt-1 truncate type-body-sm text-[var(--fg-0)]">
              {event.new_content ?? event.old_content ?? "设置变更"}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function MemoryClearPanel({
  clearText,
  clearing,
  onClearTextChange,
  onClear,
}: {
  clearText: string;
  clearing: boolean;
  onClearTextChange: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-4">
      <h2 className="type-card-title text-[var(--danger-fg)]">清空记忆</h2>
      <p className="mt-1 type-caption leading-5 text-[var(--danger-fg)]/70">
        输入“清空”后软删全部，30 天后物理删除。
      </p>
      <input
        value={clearText}
        onChange={(event) => onClearTextChange(event.target.value)}
        placeholder="清空"
        aria-label="清空确认文字"
        className="control-shell type-body-sm mt-3 h-10 w-full border-danger-border bg-[var(--bg-0)]/70 px-3 text-[var(--danger-fg)] outline-none placeholder:text-[var(--danger-fg)]/50 focus:border-danger focus:shadow-[var(--ring)] max-sm:min-h-11"
      />
      <Button
        variant="danger"
        size="md"
        disabled={clearText !== "清空" || clearing}
        loading={clearing}
        onClick={onClear}
        leftIcon={!clearing ? <Trash2 className="h-4 w-4" /> : undefined}
        fullWidth
        className="mt-3"
      >
        清空全部
      </Button>
    </div>
  );
}
