"use client";

import { Check, Image as ImageIcon } from "lucide-react";
import { Dialog, Button } from "@/components/ui/primitives";
import type { GenerationSummary } from "@/features/assets";
import { cn } from "@/lib/utils";
import { AGENT_MAX_REFERENCES } from "../model/contracts";

export function AgentReferencePicker({
  open,
  items,
  selectedIds,
  loading,
  onClose,
  onSelect,
  onLoadMore,
  hasMore,
}: {
  open: boolean;
  items: GenerationSummary[];
  selectedIds: Set<string>;
  loading: boolean;
  onClose: () => void;
  onSelect: (item: GenerationSummary) => void;
  onLoadMore: () => void;
  hasMore: boolean;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      aria-label="选择参考图"
      className="h-[var(--mobile-dialog-max-height)] max-w-3xl sm:h-[min(760px,calc(100dvh-2rem))]"
    >
      <Dialog.Header>
        <div className="flex items-center justify-between gap-3">
          <h2 className="type-card-title">选择参考图</h2>
          <span className="type-caption tabular-nums text-[var(--fg-2)]">
            {selectedIds.size} / {AGENT_MAX_REFERENCES}
          </span>
        </div>
      </Dialog.Header>
      <Dialog.Body className="mobile-dialog-scroll p-3">
        {items.length > 0 ? (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
            {items.map((item) => {
              const selected = selectedIds.has(item.image.id);
              const limitReached =
                !selected && selectedIds.size >= AGENT_MAX_REFERENCES;
              return (
                <button
                  key={item.image.id}
                  type="button"
                  onClick={() => onSelect(item)}
                  disabled={selected || limitReached}
                  aria-label={
                    selected
                      ? "已添加参考图"
                      : limitReached
                        ? "参考图已达上限"
                        : `添加参考图：${item.prompt}`
                  }
                  className={cn(
                    "group relative aspect-square min-h-11 overflow-hidden rounded-[var(--radius-card)] border bg-[var(--bg-2)]",
                    selected ? "border-accent-border" : "border-[var(--border-subtle)] hover:border-[var(--border-strong)]",
                  )}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={item.image.thumb_url ?? item.image.preview_url ?? item.image.url}
                    alt={item.prompt || "生成图片"}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                  {selected ? (
                    <span className="absolute inset-0 flex items-center justify-center bg-[var(--surface-scrim)] text-[var(--media-control-fg)]">
                      <Check className="h-6 w-6" aria-hidden />
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ) : !loading ? (
          <div className="flex min-h-64 flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
            <ImageIcon className="h-6 w-6" aria-hidden />
            <p className="type-body-sm">暂无可选图片</p>
          </div>
        ) : null}
        {loading ? (
          <p role="status" className="py-6 text-center type-caption text-[var(--fg-2)]">加载中</p>
        ) : null}
      </Dialog.Body>
      <Dialog.Footer>
        {hasMore ? (
          <Button variant="secondary" onClick={onLoadMore} disabled={loading}>加载更多</Button>
        ) : null}
        <Button variant="primary" onClick={onClose}>确认</Button>
      </Dialog.Footer>
    </Dialog>
  );
}
