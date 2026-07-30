"use client";

import { Loader2, Save } from "lucide-react";
import type { ReactNode } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { cn } from "@/lib/utils";

export const STORYBOARD_SEED_MIN = -1;
export const STORYBOARD_SEED_MAX = 4_294_967_295;

export const STATUS_TEXT: Record<string, string> = {
  draft: "草稿",
  in_progress: "进行中",
  completed: "完成",
  waiting_input: "待输入",
  generating: "生成中",
  ready: "待批准",
  approved: "已批准",
  keyframe_generating: "关键帧生成中",
  keyframe_ready: "关键帧待批准",
  keyframe_approved: "关键帧已批准",
  done: "完成",
  compositing: "合成中",
  failed: "失败",
};

export function notifyStoryboardError(action: string) {
  return (error: Error) => {
    toast.error(`${action}失败`, {
      description: error.message || "请稍后重试",
    });
  };
}

export function parseStoryboardSeed(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) &&
    parsed >= STORYBOARD_SEED_MIN &&
    parsed <= STORYBOARD_SEED_MAX
    ? parsed
    : null;
}

export function StageShell({
  title,
  children,
  actionLabel,
  loading,
  disabled,
  onAction,
}: {
  title: string;
  children: ReactNode;
  actionLabel: string;
  loading?: boolean;
  disabled?: boolean;
  onAction: () => void;
}) {
  return (
    <section className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="type-section-title">{title}</h2>
        <button
          type="button"
          onClick={onAction}
          disabled={disabled || loading}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] disabled:opacity-60 sm:min-h-10"
        >
          {loading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          {actionLabel}
        </button>
      </div>
      {children}
    </section>
  );
}

export function LabeledInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1.5 text-sm">
      <span className="text-xs font-medium text-[var(--fg-2)]">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[16px] text-[var(--fg-0)] outline-none transition focus:border-[var(--border-strong)] sm:min-h-10 md:text-base"
      />
    </label>
  );
}

export function LabeledTextarea({
  label,
  value,
  onChange,
  rows,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  rows: number;
}) {
  return (
    <label className="grid gap-1.5 text-sm">
      <span className="text-xs font-medium text-[var(--fg-2)]">{label}</span>
      <textarea
        value={value}
        rows={rows}
        onChange={(event) => onChange(event.target.value)}
        className="resize-y rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-[16px] text-[var(--fg-0)] outline-none transition focus:border-[var(--border-strong)] md:text-base"
      />
    </label>
  );
}

export function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2">
      <p className="text-[10px] text-[var(--fg-2)]">{label}</p>
      <p className="mt-0.5 font-mono text-xs text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const success = [
    "approved",
    "keyframe_approved",
    "done",
    "completed",
  ].includes(status);
  const busy = [
    "generating",
    "keyframe_generating",
    "compositing",
    "running",
    "queued",
    "submitting",
    "submit_unknown",
    "submitted",
  ].includes(status);
  return (
    <span
      className={cn(
        "inline-flex min-h-6 items-center gap-1 rounded-full border px-2 text-[11px] font-medium",
        success
          ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
          : busy
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
            : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-1)]",
      )}
    >
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
      {STATUS_TEXT[status] ?? status}
    </span>
  );
}

export function InfoLine({
  text,
  tone = "neutral",
}: {
  text: string;
  tone?: "neutral" | "success";
}) {
  return (
    <p
      className={cn(
        "rounded-[var(--radius-control)] border px-3 py-2 text-xs leading-5",
        tone === "success"
          ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
          : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
      )}
    >
      {text}
    </p>
  );
}

export function IconAction({
  icon: Icon,
  label,
  loading,
  disabled,
  onClick,
}: {
  icon: typeof Save;
  label: string;
  loading?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-xs font-medium text-[var(--fg-0)] transition hover:bg-[var(--bg-2)] disabled:opacity-55 sm:min-h-9"
    >
      {loading ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : (
        <Icon className="h-3.5 w-3.5" />
      )}
      {label}
    </button>
  );
}
