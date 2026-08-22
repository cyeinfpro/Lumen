"use client";

import { Check, ImageIcon, Loader2, TriangleAlert, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentToolCall as AgentToolCallContract } from "../model/contracts";

const TOOL_STATUS: Record<AgentToolCallContract["status"], string> = {
  queued: "等待提交",
  running: "提交中",
  succeeded: "已提交",
  failed: "提交失败",
  cancelled: "已取消",
  timed_out: "提交超时",
};

export function AgentToolCall({ tool }: { tool: AgentToolCallContract }) {
  const active = tool.status === "queued" || tool.status === "running";
  const failed = tool.status === "failed" || tool.status === "timed_out";
  const mode = tool.mode === "image_to_image" ? "图生图" : "文生图";
  const count = tool.generation_count || tool.generation_ids.length;
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex min-h-11 items-center gap-2 rounded-[var(--radius-card)] border px-3 py-2 type-caption",
        failed
          ? "border-danger-border bg-danger-soft"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)]">
        <ImageIcon className="h-4 w-4 text-[var(--fg-1)]" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block type-label text-[var(--fg-0)]">{mode}</span>
        <span className={cn("block", failed ? "text-[var(--danger-fg)]" : "text-[var(--fg-2)]")}>
          {TOOL_STATUS[tool.status]}
          {count > 0 ? ` · ${count} 个任务` : ""}
        </span>
      </span>
      {active ? (
        <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden />
      ) : tool.status === "succeeded" ? (
        <Check className="h-4 w-4 text-success" aria-hidden />
      ) : failed ? (
        <TriangleAlert className="h-4 w-4 text-[var(--danger-fg)]" aria-hidden />
      ) : (
        <X className="h-4 w-4 text-[var(--fg-2)]" aria-hidden />
      )}
    </div>
  );
}
