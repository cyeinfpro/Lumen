"use client";

import { Select } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { AgentDraft, AgentImageDefaults } from "../model/contracts";

const AGENT_SUMMARY_ASPECT_RATIOS: AgentImageDefaults["aspect_ratio"][] = [
  "1:1",
  "16:9",
  "9:16",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
];

export function AgentExecutionSummary({
  draft,
  disabled,
  imageExecutionEnabled,
  runActive,
  summary,
  costLabel,
  costWarning,
  costLoading,
  onDefaultsChange,
}: {
  draft: AgentDraft;
  disabled: boolean;
  imageExecutionEnabled: boolean;
  runActive: boolean;
  summary: string;
  costLabel: string | null;
  costWarning: boolean;
  costLoading: boolean;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
}) {
  const defaults = draft.imageDefaults;
  return (
    <div
      data-testid="agent-execution-summary"
      className="flex min-h-11 min-w-0 items-center gap-2 border-t border-[var(--border-subtle)] px-2 py-1"
    >
      <div className="scrollbar-thin flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto">
        <span className="min-w-24 flex-1 truncate px-1 type-caption text-[var(--fg-2)]">
          {runActive ? "下一轮 · " : ""}
          {summary}
        </span>
        {imageExecutionEnabled ? (
          <>
            <SummarySelect
              label="执行图片数量"
              value={String(defaults.count)}
              disabled={disabled}
              width="w-[5.25rem]"
              onChange={(value) => onDefaultsChange({ count: Number(value) })}
              options={[1, 2, 3, 4].map((count) => ({
                value: String(count),
                label: `${count} 张`,
              }))}
            />
            <SummarySelect
              label="执行图片比例"
              value={defaults.aspect_ratio}
              disabled={disabled}
              width="w-[5.5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  aspect_ratio: value as AgentImageDefaults["aspect_ratio"],
                })
              }
              options={AGENT_SUMMARY_ASPECT_RATIOS.map((aspect) => ({
                value: aspect,
                label: aspect,
              }))}
            />
            <SummarySelect
              label="执行图片分辨率"
              value={defaults.quality}
              disabled={disabled}
              width="w-[5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  quality: value as AgentImageDefaults["quality"],
                })
              }
              options={["1k", "2k", "4k"].map((quality) => ({
                value: quality,
                label: quality.toUpperCase(),
              }))}
            />
            <SummarySelect
              label="执行渲染质量"
              value={defaults.render_quality}
              disabled={disabled}
              width="w-[5.5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  render_quality: value as AgentImageDefaults["render_quality"],
                })
              }
              options={[
                { value: "auto", label: "自动" },
                { value: "low", label: "草稿" },
                { value: "medium", label: "标准" },
                { value: "high", label: "精细" },
              ]}
            />
            <SummarySelect
              label="执行图片背景"
              value={defaults.background}
              disabled={disabled}
              width="w-[5.75rem]"
              onChange={(value) =>
                onDefaultsChange({
                  background: value as AgentImageDefaults["background"],
                })
              }
              options={[
                { value: "auto", label: "自动背景" },
                { value: "opaque", label: "不透明" },
                { value: "transparent", label: "透明底" },
              ]}
            />
          </>
        ) : null}
      </div>
      <span
        aria-live="polite"
        data-agent-cost-estimate
        className={cn(
          "min-w-28 shrink-0 text-right type-caption tabular-nums",
          costWarning
            ? "text-[var(--warning-fg)]"
            : "text-[var(--fg-2)]",
          costLoading && "opacity-70",
        )}
      >
        {costLabel ?? ""}
      </span>
    </div>
  );
}

function SummarySelect({
  label,
  value,
  disabled,
  width,
  options,
  onChange,
}: {
  label: string;
  value: string;
  disabled: boolean;
  width: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <Select
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      wrapperClassName={cn("shrink-0", width)}
      className="h-8 min-h-8 py-0 pl-2 pr-7 type-caption max-sm:min-h-11"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </Select>
  );
}
