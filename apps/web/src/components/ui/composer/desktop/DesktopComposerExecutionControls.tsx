"use client";

import { ChevronDown, Zap } from "lucide-react";
import { useRef, useState } from "react";

import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import { cn } from "@/lib/utils";

import { AspectRatioPicker } from "../shared/AspectRatioPicker";
import { ExecutionSummaryBar } from "../shared/ExecutionSummaryBar";
import type { ComposerExecutionSummary } from "../shared/executionSummary";
import { DesktopPopover } from "./DesktopPopover";

export const COUNT_OPTIONS = [1, 2, 4, 8, 10] as const;

export const QUALITY_OPTIONS: ReadonlyArray<{
  value: Quality;
  label: string;
}> = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
];

export const RENDER_QUALITY_OPTIONS: ReadonlyArray<{
  value: RenderQualityChoice;
  label: string;
}> = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

export function ComposerExecutionControls({
  mode,
  summary,
  count,
  onCountChange,
  aspect,
  onAspectChange,
  quality,
  onQualityChange,
  renderQuality,
  onRenderQualityChange,
  fast,
  onFastChange,
  attachmentCount,
  costLabel,
  costWarning,
  onAdjust,
}: {
  mode: "chat" | "image";
  summary: ComposerExecutionSummary;
  count: number;
  onCountChange: (value: number) => void;
  aspect: AspectRatio;
  onAspectChange: (value: AspectRatio) => void;
  quality: Quality;
  onQualityChange: (value: Quality) => void;
  renderQuality: RenderQualityChoice;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  fast: boolean;
  onFastChange: (value: boolean) => void;
  attachmentCount: number;
  costLabel?: string | null;
  costWarning?: boolean;
  onAdjust: () => void;
}) {
  if (mode === "image") {
    return (
      <ImageQuickSettingsBar
        summary={summary}
        count={count}
        onCountChange={onCountChange}
        aspect={aspect}
        onAspectChange={onAspectChange}
        quality={quality}
        onQualityChange={onQualityChange}
        renderQuality={renderQuality}
        onRenderQualityChange={onRenderQualityChange}
        fast={fast}
        onFastChange={onFastChange}
        attachmentCount={attachmentCount}
        costLabel={costLabel}
        costWarning={costWarning}
      />
    );
  }

  return <ExecutionSummaryBar summary={summary} onAdjust={onAdjust} />;
}

function ImageQuickSettingsBar({
  summary,
  count,
  onCountChange,
  aspect,
  onAspectChange,
  quality,
  onQualityChange,
  renderQuality,
  onRenderQualityChange,
  fast,
  onFastChange,
  attachmentCount,
  costLabel,
  costWarning,
}: {
  summary: ComposerExecutionSummary;
  count: number;
  onCountChange: (value: number) => void;
  aspect: AspectRatio;
  onAspectChange: (value: AspectRatio) => void;
  quality: Quality;
  onQualityChange: (value: Quality) => void;
  renderQuality: RenderQualityChoice;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  fast: boolean;
  onFastChange: (value: boolean) => void;
  attachmentCount: number;
  costLabel?: string | null;
  costWarning?: boolean;
}) {
  const [aspectOpen, setAspectOpen] = useState(false);
  const aspectAnchorRef = useRef<HTMLButtonElement | null>(null);

  return (
    <>
      <div
        aria-label={summary.text}
        title={summary.text}
        className={cn(
          "mx-3 mt-1.5 flex min-h-10 items-center gap-1.5 overflow-x-auto overscroll-x-contain rounded-[var(--radius-card)] border px-2 py-1 no-scrollbar",
          "border-[var(--border-subtle)] bg-[var(--bg-2)]/55",
        )}
      >
        <span className="shrink-0 px-1 text-[11px] font-medium text-[var(--accent)]">
          {summary.taskLabel}
        </span>

        <span
          aria-hidden
          className="h-5 w-px shrink-0 bg-[var(--border-subtle)]"
        />

        <QuickSelect
          ariaLabel="生成数量"
          value={String(count)}
          onChange={(value) => onCountChange(Number(value))}
          options={COUNT_OPTIONS.map((value) => ({
            value: String(value),
            label: `${value} 张`,
          }))}
          className="w-[62px]"
        />

        <button
          ref={aspectAnchorRef}
          type="button"
          aria-label="宽高比"
          aria-haspopup="dialog"
          aria-expanded={aspectOpen}
          title="宽高比"
          onClick={() => setAspectOpen((open) => !open)}
          className={cn(
            "inline-flex h-8 w-[66px] shrink-0 items-center justify-between rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/68 px-2",
            "font-mono text-[11px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-1)]",
            "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          )}
        >
          {aspect}
          <ChevronDown
            className="h-3 w-3 text-[var(--fg-2)]"
            aria-hidden
          />
        </button>

        <InlineChoiceGroup
          ariaLabel="输出尺寸"
          value={quality}
          onChange={onQualityChange}
          items={QUALITY_OPTIONS}
        />

        <InlineChoiceGroup
          ariaLabel="生成质量"
          value={renderQuality}
          onChange={onRenderQualityChange}
          items={RENDER_QUALITY_OPTIONS}
        />

        <button
          type="button"
          aria-pressed={fast}
          aria-label={fast ? "关闭 Fast" : "开启 Fast"}
          title="Fast"
          onClick={() => onFastChange(!fast)}
          className={cn(
            "inline-flex h-8 shrink-0 items-center gap-1 rounded-[var(--radius-control)] border px-2 text-[11px] font-medium",
            "transition-colors focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
            fast
              ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)]/68 text-[var(--fg-1)] hover:text-[var(--fg-0)]",
          )}
        >
          <Zap
            className="h-3.5 w-3.5"
            fill={fast ? "currentColor" : "none"}
            aria-hidden
          />
          Fast
        </button>

        {attachmentCount > 0 && (
          <span className="shrink-0 text-[10px] text-[var(--fg-2)]">
            {attachmentCount} 张参考
          </span>
        )}

        {costLabel && (
          <span
            className={cn(
              "ml-auto shrink-0 px-1 text-[10px] tabular-nums",
              costWarning ? "text-[var(--danger)]" : "text-[var(--fg-2)]",
            )}
          >
            {costLabel}
          </span>
        )}
      </div>

      <DesktopPopover
        open={aspectOpen}
        onClose={() => setAspectOpen(false)}
        anchorRef={aspectAnchorRef}
        ariaLabel="选择宽高比"
        align="left"
        maxHeight="min(72vh, 560px)"
        className="w-auto p-0"
      >
        <AspectRatioPicker
          value={aspect}
          onChange={onAspectChange}
          onClose={() => setAspectOpen(false)}
        />
      </DesktopPopover>
    </>
  );
}

function QuickSelect({
  ariaLabel,
  value,
  onChange,
  options,
  className,
}: {
  ariaLabel: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<{ value: string; label: string }>;
  className?: string;
}) {
  return (
    <label className="relative shrink-0" title={ariaLabel}>
      <span className="sr-only">{ariaLabel}</span>
      <select
        aria-label={ariaLabel}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn(
          "h-8 appearance-none rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/68 pl-2 pr-6",
          "text-[11px] font-medium text-[var(--fg-0)] outline-none transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-1)] focus-visible:shadow-[var(--ring)]",
          className,
        )}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-[var(--fg-2)]"
        aria-hidden
      />
    </label>
  );
}

function InlineChoiceGroup<V extends string>({
  ariaLabel,
  value,
  onChange,
  items,
}: {
  ariaLabel: string;
  value: V;
  onChange: (value: V) => void;
  items: ReadonlyArray<{ value: V; label: string }>;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="flex h-8 shrink-0 items-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/68 p-0.5"
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            aria-pressed={active}
            title={`${ariaLabel}：${item.label}`}
            onClick={() => onChange(item.value)}
            className={cn(
              "inline-flex h-6 min-w-7 items-center justify-center rounded-[calc(var(--radius-control)-2px)] px-1.5 text-[10px] font-medium",
              "transition-colors focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
              active
                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
