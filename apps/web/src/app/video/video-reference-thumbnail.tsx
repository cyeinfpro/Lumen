"use client";

import type { ReactNode } from "react";
import {
  CircleCheck,
  ImageIcon,
  Maximize2,
  Tags,
  Video as VideoIcon,
} from "lucide-react";

import type { ReferenceDraft } from "@/lib/video/types";

export function ReferenceThumbnailView({
  item,
  active,
  className,
  preview,
  failed,
  showPreview,
}: {
  item: ReferenceDraft;
  active: boolean;
  className: string;
  preview: ReactNode;
  failed: boolean;
  showPreview: boolean;
}) {
  const Icon = item.kind === "video" ? VideoIcon : item.url ? Tags : ImageIcon;

  return (
    <span className={className}>
      {showPreview ? (
        preview
      ) : (
        <span className="flex h-full w-full flex-col items-center justify-center gap-1 px-2 text-center">
          <Icon className="h-5 w-5" aria-hidden="true" />
          <span className="text-[10px] font-medium leading-3">
            {failed ? "预览失败" : "暂无预览"}
          </span>
        </span>
      )}
      {showPreview && (
        <span className="absolute bottom-1.5 left-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-0)]/82 p-1 text-[var(--fg-1)] shadow-[var(--shadow-1)]">
          <Maximize2 className="h-3 w-3" aria-hidden="true" />
        </span>
      )}
      {active && (
        <span className="absolute right-1.5 top-1.5 rounded-full border border-[var(--bg-1)] bg-[var(--accent)] p-0.5 text-[var(--accent-on)] shadow-[var(--shadow-1)]">
          <CircleCheck className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
      {item.kind === "video" && showPreview && (
        <span className="absolute bottom-1.5 right-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/85 p-0.5 text-[var(--fg-1)]">
          <VideoIcon className="h-2.5 w-2.5" aria-hidden="true" />
        </span>
      )}
    </span>
  );
}
