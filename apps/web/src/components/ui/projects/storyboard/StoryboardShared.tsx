"use client";

import { Loader2, Save } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
import { StatusBadge } from "@/components/ui/primitives/StatusBadge";
import { Textarea } from "@/components/ui/primitives/Textarea";
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
      description: error.message || "稍后重试",
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
        <Button
          variant="primary"
          onClick={onAction}
          loading={loading}
          disabled={disabled}
          leftIcon={<Save className="h-4 w-4" />}
        >
          {actionLabel}
        </Button>
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
    <Input
      label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
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
    <Textarea
      label={label}
      value={value}
      rows={rows}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2">
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="type-body-sm mt-0.5 tabular-nums text-[var(--fg-0)]">
        {value}
      </p>
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
    <StatusBadge
      status={status}
      tone={success ? "success" : busy ? "accent" : "info"}
      label={
        <span className="inline-flex items-center gap-1">
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
          {STATUS_TEXT[status] ?? status}
        </span>
      }
    />
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
        "type-caption rounded-[var(--radius-control)] border px-3 py-2",
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
    <Button
      variant="outline"
      size="sm"
      onClick={onClick}
      disabled={disabled}
      loading={loading}
      leftIcon={<Icon className="h-3.5 w-3.5" />}
    >
      {label}
    </Button>
  );
}
