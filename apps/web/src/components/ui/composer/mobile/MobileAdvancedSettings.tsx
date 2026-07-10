"use client";

import {
  ChevronDown,
  Code2,
  FileSearch,
  Globe2,
  ImagePlus,
  Zap,
} from "lucide-react";
import { Chip } from "@/components/ui/primitives/mobile";
import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import type { ReasoningEffort } from "@/store/useChatStore";

export const MOBILE_REASONING_OPTIONS: ReadonlyArray<{
  value: ReasoningEffort;
  label: string;
  hint: string;
}> = [
  { value: "none", label: "最快", hint: "直接回复" },
  { value: "low", label: "低", hint: "轻量思考" },
  { value: "medium", label: "中", hint: "平衡" },
  { value: "high", label: "高", hint: "多想一步" },
  { value: "xhigh", label: "很高", hint: "更慢，适合复杂问题" },
];

const COUNT_OPTIONS = [1, 2, 4, 8, 10] as const;

const QUALITY_OPTIONS: ReadonlyArray<{ value: Quality; label: string }> = [
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

interface MobileAdvancedSettingsProps {
  mode: "chat" | "image";
  quality: Quality;
  onQualityChange: (value: Quality) => void;
  renderQuality: RenderQualityChoice;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  aspect: AspectRatio;
  onOpenAspect: () => void;
  count: number;
  onCountChange: (value: number) => void;
  reasoningEffort: ReasoningEffort;
  onOpenReasoning: () => void;
  webSearch: boolean;
  onWebSearchChange: (value: boolean) => void;
  fileSearch: boolean;
  onFileSearchChange: (value: boolean) => void;
  codeInterpreter: boolean;
  onCodeInterpreterChange: (value: boolean) => void;
  imageGeneration: boolean;
  onImageGenerationChange: (value: boolean) => void;
  fast: boolean;
  onFastChange: (value: boolean) => void;
}

export function MobileAdvancedSettings({
  mode,
  quality,
  onQualityChange,
  renderQuality,
  onRenderQualityChange,
  aspect,
  onOpenAspect,
  count,
  onCountChange,
  reasoningEffort,
  onOpenReasoning,
  webSearch,
  onWebSearchChange,
  fileSearch,
  onFileSearchChange,
  codeInterpreter,
  onCodeInterpreterChange,
  imageGeneration,
  onImageGenerationChange,
  fast,
  onFastChange,
}: MobileAdvancedSettingsProps) {
  const imageMode = mode === "image";

  return (
    <div className="mobile-dialog-scroll px-4 pb-5">
      <div className="border-b border-[var(--border-subtle)] py-3.5">
        <h3 className="text-[15px] font-semibold text-[var(--fg-0)]">
          执行设置
        </h3>
        <p className="mt-1 text-[12px] text-[var(--fg-2)]">
          主输入区只保留高频操作，参数在这里集中调整。
        </p>
      </div>

      {imageMode ? (
        <div className="grid gap-4 pt-4">
          <div className="grid grid-cols-2 gap-2">
            <MobileSettingSelect
              label="尺寸"
              value={quality}
              onChange={(value) => onQualityChange(value as Quality)}
              options={QUALITY_OPTIONS}
            />
            <MobileSettingSelect
              label="质量"
              value={renderQuality}
              onChange={(value) =>
                onRenderQualityChange(value as RenderQualityChoice)
              }
              options={RENDER_QUALITY_OPTIONS}
            />
            <MobileSettingSelect
              label="数量"
              value={String(count)}
              onChange={(value) => onCountChange(Number(value))}
              options={COUNT_OPTIONS.map((value) => ({
                value: String(value),
                label: `${value} 张`,
              }))}
            />
            <button
              type="button"
              onClick={onOpenAspect}
              className="grid min-h-14 gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2 text-left focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
            >
              <span className="text-[10px] text-[var(--fg-2)]">宽高比</span>
              <span className="flex items-center justify-between text-[13px] font-medium text-[var(--fg-0)]">
                {aspect}
                <ChevronDown
                  className="h-3.5 w-3.5 text-[var(--fg-2)]"
                  aria-hidden
                />
              </span>
            </button>
          </div>
          <Chip
            active={fast}
            onClick={() => onFastChange(!fast)}
            icon={<Zap className="h-3.5 w-3.5" aria-hidden />}
            className="min-h-11 justify-center"
          >
            Fast
          </Chip>
        </div>
      ) : (
        <div className="grid gap-4 pt-4">
          <button
            type="button"
            onClick={onOpenReasoning}
            className="flex min-h-14 items-center justify-between rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-left focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
          >
            <span>
              <span className="block text-[11px] text-[var(--fg-2)]">
                推理强度
              </span>
              <span className="mt-0.5 block text-[13px] font-medium text-[var(--fg-0)]">
                {MOBILE_REASONING_OPTIONS.find(
                  (option) => option.value === reasoningEffort,
                )?.label ?? "默认"}
              </span>
            </span>
            <ChevronDown
              className="h-4 w-4 text-[var(--fg-2)]"
              aria-hidden
            />
          </button>
          <div className="grid grid-cols-2 gap-2">
            <Chip
              active={webSearch}
              onClick={() => onWebSearchChange(!webSearch)}
              icon={<Globe2 className="h-3.5 w-3.5" aria-hidden />}
              className="min-h-11 justify-center"
            >
              搜索
            </Chip>
            <Chip
              active={fileSearch}
              onClick={() => onFileSearchChange(!fileSearch)}
              icon={<FileSearch className="h-3.5 w-3.5" aria-hidden />}
              className="min-h-11 justify-center"
            >
              文件
            </Chip>
            <Chip
              active={codeInterpreter}
              onClick={() => onCodeInterpreterChange(!codeInterpreter)}
              icon={<Code2 className="h-3.5 w-3.5" aria-hidden />}
              className="min-h-11 justify-center"
            >
              代码
            </Chip>
            <Chip
              active={imageGeneration}
              onClick={() => onImageGenerationChange(!imageGeneration)}
              icon={<ImagePlus className="h-3.5 w-3.5" aria-hidden />}
              className="min-h-11 justify-center"
            >
              生图
            </Chip>
            <Chip
              active={fast}
              onClick={() => onFastChange(!fast)}
              icon={<Zap className="h-3.5 w-3.5" aria-hidden />}
              className="col-span-2 min-h-11 justify-center"
            >
              Fast
            </Chip>
          </div>
        </div>
      )}
    </div>
  );
}

function MobileSettingSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: ReadonlyArray<{ value: string; label: string }>;
}) {
  return (
    <label className="grid min-h-14 gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2">
      <span className="text-[10px] text-[var(--fg-2)]">{label}</span>
      <span className="relative">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-7 w-full appearance-none bg-transparent pr-6 text-[13px] font-medium text-[var(--fg-0)] outline-none"
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-0 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]"
          aria-hidden
        />
      </span>
    </label>
  );
}
