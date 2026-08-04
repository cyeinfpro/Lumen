"use client";

import { ChevronDown, Zap } from "lucide-react";
import { useRef, useState } from "react";

import { Button, Select } from "@/components/ui/primitives";
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
          "border-[var(--border-subtle)] bg-[var(--bg-2)]",
        )}
      >
        <span className="type-label shrink-0 px-1 text-accent">
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

        <Button
          ref={aspectAnchorRef}
          variant="outline"
          size="sm"
          aria-label="宽高比"
          aria-haspopup="dialog"
          aria-expanded={aspectOpen}
          title="宽高比"
          onClick={() => setAspectOpen((open) => !open)}
          className={cn(
            "h-8 w-[66px] shrink-0 justify-between border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 type-caption text-[var(--fg-0)] font-mono",
          )}
        >
          {aspect}
          <ChevronDown
            className="h-3 w-3 text-[var(--fg-2)]"
            aria-hidden
          />
        </Button>

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

        <Button
          variant="outline"
          size="sm"
          aria-pressed={fast}
          aria-label={fast ? "关闭 Fast" : "开启 Fast"}
          title="Fast"
          onClick={() => onFastChange(!fast)}
          className={cn(
            "h-8 shrink-0 px-2",
            fast
              ? "border-accent-border bg-accent-soft text-accent"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:text-[var(--fg-0)]",
          )}
          leftIcon={
            <Zap
              className="h-3.5 w-3.5"
              fill={fast ? "currentColor" : "none"}
              aria-hidden
            />
          }
        >
          Fast
        </Button>

        {attachmentCount > 0 && (
          <span className="type-overline shrink-0 text-[var(--fg-2)]">
            {attachmentCount} 张参考
          </span>
        )}

        {costLabel && (
          <span
            className={cn(
              "ml-auto shrink-0 px-1 type-overline tabular-nums",
              costWarning ? "text-warning" : "text-[var(--fg-2)]",
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
      <Select
        aria-label={ariaLabel}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        wrapperClassName="shrink-0"
        className={cn(
          "h-8 min-h-8 border-[var(--border-subtle)] bg-[var(--bg-1)] pl-2 pr-6 type-label text-[var(--fg-0)] hover:border-[var(--border)]",
          className,
        )}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </Select>
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
          <Button
            key={item.value}
            variant="ghost"
            size="sm"
            aria-pressed={active}
            title={`${ariaLabel}：${item.label}`}
            onClick={() => onChange(item.value)}
            className={cn(
              "h-6 min-w-7 rounded-[var(--radius-control)] px-1.5 type-overline",
              active
                ? "bg-[var(--bg-0)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
            )}
          >
            {item.label}
          </Button>
        );
      })}
    </div>
  );
}
