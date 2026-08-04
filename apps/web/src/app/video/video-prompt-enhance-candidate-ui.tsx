"use client";

import type { ReactNode } from "react";
import { CircleCheck, Copy } from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

type PromptEnhanceCandidateView = {
  title: string;
  prompt: string;
};

export function PromptEnhanceLoadingStateView({
  preview,
}: {
  preview: string;
}) {
  return (
    <div className="space-y-3 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="type-body-sm font-medium text-[var(--fg-0)]">
            正在生成可比较版本
          </p>
          <p className="mt-0.5 type-caption text-[var(--fg-2)]">
            完成后可逐个预览，不会直接覆盖当前描述。
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--accent-soft)] px-2 py-1 type-caption font-medium text-[var(--accent)]">
          AI 整理中
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div className="h-full w-1/2 animate-pulse rounded-full bg-[var(--accent)]" />
      </div>
      <div
        role="status"
        aria-live="polite"
        className="min-h-36 whitespace-pre-wrap break-words rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 type-body-sm leading-7 text-[var(--fg-1)]"
      >
        {preview || "等待模型返回优化方案..."}
      </div>
    </div>
  );
}

export function PromptEnhanceCandidateCardView({
  candidate,
  index,
  selected,
  previewing,
  actionLabel,
  onPreview,
}: {
  candidate: PromptEnhanceCandidateView;
  index: number;
  selected: boolean;
  previewing: boolean;
  actionLabel: string;
  onPreview: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={previewing}
      onClick={onPreview}
      className={cn(
        "flex min-h-32 w-[min(82vw,20rem)] shrink-0 flex-col rounded-[var(--radius-control)] border p-3 text-left transition-[background-color,border-color,box-shadow] lg:w-auto lg:min-w-0",
        selected
          ? "border-success-border bg-success-soft shadow-[var(--shadow-1)]"
          : previewing
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/72 hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border font-mono type-caption",
            selected
              ? "border-success-border text-success"
              : previewing
                ? "border-[var(--accent-border)] text-[var(--accent)]"
                : "border-[var(--border)] text-[var(--fg-2)]",
          )}
        >
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="min-w-0 flex-1 truncate type-body-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </span>
            {index === 0 && (
              <span className="shrink-0 rounded-full border border-[var(--accent-border)] bg-[var(--bg-0)] px-1.5 py-0.5 type-caption font-medium text-[var(--accent)]">
                推荐
              </span>
            )}
          </span>
          <span className="mt-0.5 block type-caption text-[var(--fg-2)]">
            {actionLabel}
          </span>
        </span>
        {selected && <CircleCheck className="h-4 w-4 shrink-0 text-success" />}
      </span>
      <span className="mt-2 line-clamp-2 type-caption leading-5 text-[var(--fg-1)]">
        {candidate.prompt}
      </span>
      <span className="mt-auto flex items-center justify-between gap-2 pt-2 type-caption text-[var(--fg-2)]">
        <span>{candidate.prompt.length.toLocaleString()} 字</span>
        <span
          className={
            selected ? "text-success" : previewing ? "text-[var(--accent)]" : ""
          }
        >
          {selected ? "已应用" : previewing ? "正在预览" : "查看方案"}
        </span>
      </span>
    </button>
  );
}

export function PromptEnhanceCandidatePreviewView({
  candidate,
  selected,
  applicable,
  actionLabel,
  buttonText,
  footer,
  onApply,
  onCopy,
}: {
  candidate: PromptEnhanceCandidateView;
  selected: boolean;
  applicable: boolean;
  actionLabel: string;
  buttonText: string;
  footer: ReactNode;
  onApply: () => void;
  onCopy: () => void;
}) {
  return (
    <article className="overflow-hidden rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/68 px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="type-body-sm font-semibold text-[var(--fg-0)]">
              {candidate.title}
            </h3>
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-0.5 type-caption text-[var(--fg-2)]">
              {actionLabel}
            </span>
            {selected && (
              <span className="rounded-full border border-success-border bg-success-soft px-2 py-0.5 type-caption font-medium text-success">
                当前已应用
              </span>
            )}
          </div>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            {applicable
              ? "先完整预览，再决定是否替换编辑器中的描述。"
              : "这是 AI 的判断与补充建议，仅供查看。"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            variant={selected ? "secondary" : "primary"}
            size="sm"
            disabled={selected || !applicable}
            onClick={onApply}
          >
            {buttonText}
          </Button>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onCopy}
            aria-label="复制优化提示词"
            tooltip="复制提示词"
          >
            <Copy className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </header>
      <div className="whitespace-pre-wrap break-words px-3 py-3 type-body-sm leading-7 text-[var(--fg-1)] sm:px-4 sm:py-4">
        {candidate.prompt}
      </div>
      <footer className="border-t border-[var(--border-subtle)] px-3 py-2 type-caption tabular-nums text-[var(--fg-2)] sm:px-4">
        {footer}
      </footer>
    </article>
  );
}
