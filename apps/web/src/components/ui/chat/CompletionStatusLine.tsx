"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import type { AssistantMessage } from "@/lib/types";

type StatusTone = "active" | "muted" | "warn" | "danger";

interface CompletionStatusLineProps {
  msg: AssistantMessage;
  compact?: boolean;
}

interface CompletionStatus {
  label: string;
  tone: StatusTone;
  active: boolean;
}

const WAITING_MS = 10_000;
const STALLED_MS = 12_000;

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    const tick = () => setNow(Date.now());
    const id = window.setInterval(tick, 1000);
    return () => {
      window.clearInterval(id);
    };
  }, [active]);

  return now;
}

function secondsSince(now: number, then: number | undefined): number {
  const base = then && Number.isFinite(then) ? then : now;
  return Math.max(0, Math.floor((now - base) / 1000));
}

function timestampOrNow(now: number, then: number | undefined): number {
  return then && Number.isFinite(then) ? then : now;
}

function activeToolLabel(msg: AssistantMessage): CompletionStatus | null {
  const calls = msg.tool_calls ?? [];
  const failed = calls.find((call) => call.status === "failed");
  if (failed) {
    return {
      label: `${failed.label}失败`,
      tone: "warn",
      active: false,
    };
  }
  const running = calls.find(
    (call) => call.status === "running" || call.status === "queued",
  );
  if (running) {
    return {
      label: `${running.label}中`,
      tone: "active",
      active: true,
    };
  }
  const latest = calls[calls.length - 1];
  if (latest?.status === "succeeded") {
    return {
      label: `${latest.label}完成`,
      tone: "muted",
      active: false,
    };
  }
  return null;
}

function resolveCompletionStatus(
  msg: AssistantMessage,
  now: number,
): CompletionStatus | null {
  const isChatLike =
    msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
  if (!isChatLike) return null;

  const hasText = Boolean(msg.text?.trim());
  const hasThinking = Boolean(msg.thinking?.trim());
  const hasGeneration = Boolean(
    msg.generation_id || (msg.generation_ids && msg.generation_ids.length > 0),
  );

  if (msg.status === "pending") {
    // 乐观更新消息的 created_at 可能为 0（尚未校正），此时以 stream_started_at
    // 或当前时间为起点，避免显示 "排队中 0s" 不动。
    const createdAt = msg.created_at && msg.created_at > 0
      ? msg.created_at
      : msg.stream_started_at && msg.stream_started_at > 0
        ? msg.stream_started_at
        : now;
    const elapsed = secondsSince(now, createdAt);
    const label =
      now - createdAt >= WAITING_MS
        ? `等待模型响应 ${elapsed}s`
        : `排队中 ${elapsed}s`;
    return { label, tone: "muted", active: true };
  }

  if (msg.status === "streaming") {
    const toolStatus = activeToolLabel(msg);
    if (toolStatus) return toolStatus;

    const streamStartedAt = timestampOrNow(
      now,
      msg.stream_started_at ?? msg.created_at,
    );
    const outputIdleMs = now - (msg.last_delta_at ?? streamStartedAt);

    if (hasText || hasGeneration) {
      if (outputIdleMs < STALLED_MS) return null;
      return {
        label: `等待后续输出 ${secondsSince(now, msg.last_delta_at)}s`,
        tone: "warn",
        active: true,
      };
    }

    const elapsed = secondsSince(now, streamStartedAt);
    if (hasThinking) {
      return {
        label:
          outputIdleMs >= STALLED_MS
            ? `仍在思考 ${secondsSince(now, msg.last_delta_at ?? streamStartedAt)}s`
            : `正在思考 ${elapsed}s`,
        tone: outputIdleMs >= STALLED_MS ? "warn" : "active",
        active: true,
      };
    }

    return {
      label:
        outputIdleMs >= STALLED_MS
          ? `仍在等待输出 ${elapsed}s`
          : `正在连接模型 ${elapsed}s`,
      tone: outputIdleMs >= STALLED_MS ? "warn" : "active",
      active: true,
    };
  }

  if (msg.status === "failed" && !hasText) {
    return { label: "回复失败", tone: "danger", active: false };
  }

  if (msg.status === "canceled") {
    return { label: "已取消", tone: "muted", active: false };
  }

  return null;
}

export function CompletionStatusLine({
  msg,
  compact = false,
}: CompletionStatusLineProps) {
  const active = msg.status === "pending" || msg.status === "streaming";
  const now = useNow(active);
  const status = resolveCompletionStatus(msg, now);
  if (!status) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-full border px-2",
        compact ? "h-6 text-[11px]" : "h-6 text-[12px]",
        status.tone === "active" &&
          "border-[var(--amber-400)]/25 bg-[var(--amber-400)]/10 text-[var(--amber-500)]",
        status.tone === "muted" &&
          "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
        status.tone === "warn" &&
          "border-[var(--amber-400)]/40 bg-[var(--amber-400)]/15 text-[var(--amber-500)]",
        status.tone === "danger" &&
          "border-[var(--danger)]/25 bg-[var(--danger)]/10 text-[var(--danger)]",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status.tone === "danger"
            ? "bg-[var(--danger)]"
            : status.tone === "muted"
              ? "bg-[var(--fg-3)]"
              : "bg-[var(--amber-400)]",
          status.active && "animate-pulse",
        )}
      />
      <span>{status.label}</span>
    </div>
  );
}
