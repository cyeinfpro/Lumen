"use client";

import { ChevronDown, SlidersHorizontal, Zap } from "lucide-react";

import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ExecutionSummaryBar } from "../shared/ExecutionSummaryBar";
import type { ComposerExecutionSummary } from "../shared/executionSummary";

const COUNT_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

const QUALITY_OPTIONS: ReadonlyArray<{
  value: Quality;
  label: string;
}> = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
];

const RENDER_QUALITY_OPTIONS: ReadonlyArray<{
  value: RenderQualityChoice;
  label: string;
}> = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

export function MobileComposerExecutionControls({
  mode,
  summary,
  count,
  onCountChange,
  aspect,
  onOpenAspect,
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
  onOpenAspect: () => void;
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
      <MobileImageQuickSettingsBar
        summary={summary}
        count={count}
        onCountChange={onCountChange}
        aspect={aspect}
        onOpenAspect={onOpenAspect}
        quality={quality}
        onQualityChange={onQualityChange}
        renderQuality={renderQuality}
        onRenderQualityChange={onRenderQualityChange}
        fast={fast}
        onFastChange={onFastChange}
        attachmentCount={attachmentCount}
        costLabel={costLabel}
        costWarning={costWarning}
        onAdjust={onAdjust}
      />
    );
  }

  return <ExecutionSummaryBar summary={summary} compact onAdjust={onAdjust} />;
}

function MobileImageQuickSettingsBar({
  summary,
  count,
  onCountChange,
  aspect,
  onOpenAspect,
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
  summary: ComposerExecutionSummary;
  count: number;
  onCountChange: (value: number) => void;
  aspect: AspectRatio;
  onOpenAspect: () => void;
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
  return (
    <div
      aria-label={summary.text}
      className="mx-3 mt-1.5 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)]/55 p-1"
    >
      <div className="flex min-h-6 items-center gap-1.5 px-1.5">
        <span className="text-[10px] font-medium text-[var(--accent)]">
          {summary.taskLabel}
        </span>
        {attachmentCount > 0 ? (
          <span className="text-[9px] text-[var(--fg-2)]">
            {attachmentCount} 张参考
          </span>
        ) : null}
        {costLabel ? (
          <span
            className={cn(
              "text-[9px] tabular-nums",
              costWarning ? "text-[var(--danger)]" : "text-[var(--fg-2)]",
            )}
          >
            {costLabel}
          </span>
        ) : null}
        <button
          type="button"
          aria-label="更多执行设置"
          aria-haspopup="dialog"
          title="更多执行设置"
          onClick={onAdjust}
          className="ml-auto grid h-6 w-6 shrink-0 place-items-center rounded-[var(--radius-control)] text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>

      <div className="grid grid-cols-5 gap-1">
        <MobileQuickSelect
          ariaLabel="生成数量"
          label="张数"
          value={String(count)}
          onChange={(value) => onCountChange(Number(value))}
          options={COUNT_OPTIONS.map((value) => ({
            value: String(value),
            label: `${value}张`,
          }))}
        />

        <button
          type="button"
          aria-label={`宽高比 ${aspect}`}
          aria-haspopup="dialog"
          onClick={onOpenAspect}
          className={cn(
            "grid min-h-12 min-w-0 content-center gap-0.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/72 px-1.5 text-left",
            "touch-manipulation transition-colors active:bg-[var(--bg-3)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          )}
        >
          <span className="text-[9px] leading-3 text-[var(--fg-2)]">比例</span>
          <span className="flex min-w-0 items-center justify-between gap-0.5 font-mono text-[10px] font-medium leading-4 text-[var(--fg-0)]">
            <span className="truncate">{aspect}</span>
            <ChevronDown
              className="h-3 w-3 shrink-0 text-[var(--fg-2)]"
              aria-hidden
            />
          </span>
        </button>

        <MobileQuickSelect
          ariaLabel="输出尺寸"
          label="尺寸"
          value={quality}
          onChange={(value) => onQualityChange(value as Quality)}
          options={QUALITY_OPTIONS}
        />

        <MobileQuickSelect
          ariaLabel="生成质量"
          label="质量"
          value={renderQuality}
          onChange={(value) =>
            onRenderQualityChange(value as RenderQualityChoice)
          }
          options={RENDER_QUALITY_OPTIONS}
        />

        <button
          type="button"
          aria-pressed={fast}
          aria-label={fast ? "关闭 Fast" : "开启 Fast"}
          onClick={() => onFastChange(!fast)}
          className={cn(
            "grid min-h-12 min-w-0 content-center justify-items-start gap-0.5 rounded-[var(--radius-control)] border px-1.5 text-left",
            "touch-manipulation transition-colors focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
            fast
              ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)]/72 text-[var(--fg-1)] active:bg-[var(--bg-3)]",
          )}
        >
          <span className="text-[9px] leading-3 opacity-70">加速</span>
          <span className="flex min-w-0 items-center gap-0.5 text-[10px] font-medium leading-4">
            <Zap
              className="h-3 w-3 shrink-0"
              fill={fast ? "currentColor" : "none"}
              aria-hidden
            />
            <span className="truncate">Fast</span>
          </span>
        </button>
      </div>
    </div>
  );
}

function MobileQuickSelect({
  ariaLabel,
  label,
  value,
  onChange,
  options,
}: {
  ariaLabel: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<{ value: string; label: string }>;
}) {
  const selectedLabel =
    options.find((option) => option.value === value)?.label ?? value;

  return (
    <label
      className={cn(
        "relative grid min-h-12 min-w-0 content-center gap-0.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/72 px-1.5",
        "touch-manipulation focus-within:shadow-[var(--ring)]",
      )}
    >
      <span aria-hidden className="text-[9px] leading-3 text-[var(--fg-2)]">
        {label}
      </span>
      <span
        aria-hidden
        className="flex min-w-0 items-center justify-between gap-0.5 text-[10px] font-medium leading-4 text-[var(--fg-0)]"
      >
        <span className="truncate">{selectedLabel}</span>
        <ChevronDown
          className="h-3 w-3 shrink-0 text-[var(--fg-2)]"
        />
      </span>
      <select
        aria-label={ariaLabel}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="absolute inset-0 h-full w-full cursor-pointer appearance-none opacity-0"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
