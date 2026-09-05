"use client";

import {
  ChevronDown,
  Code2,
  FileSearch,
  Globe2,
  ImagePlus,
  Layers2,
  Zap,
} from "lucide-react";
import { Button, Select } from "@/components/ui/primitives";
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

const COUNT_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

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
  transparentBackground: boolean;
  onTransparentBackgroundChange: (value: boolean) => void;
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
  transparentBackground,
  onTransparentBackgroundChange,
}: MobileAdvancedSettingsProps) {
  const imageMode = mode === "image";

  return (
    <div className="mobile-dialog-scroll px-4 pb-5">
      <div className="border-b border-[var(--border-subtle)] py-3.5">
        <h3 className="type-card-title">
          执行设置
        </h3>
        <p className="mt-1 type-caption text-[var(--fg-2)]">
          仅用于下一次提交
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
            <Button
              variant="outline"
              size="md"
              onClick={onOpenAspect}
              className="grid h-auto min-h-14 justify-stretch gap-1 rounded-[var(--radius-card)] bg-[var(--bg-1)] px-3 py-2 text-left"
            >
              <span className="type-overline text-[var(--fg-2)]">宽高比</span>
              <span className="type-label flex items-center justify-between text-[var(--fg-0)]">
                {aspect}
                <ChevronDown
                  className="h-3.5 w-3.5 text-[var(--fg-2)]"
                  aria-hidden
                />
              </span>
            </Button>
          </div>
          <Chip
            active={transparentBackground}
            onClick={() =>
              onTransparentBackgroundChange(!transparentBackground)
            }
            icon={<Layers2 className="h-3.5 w-3.5" aria-hidden />}
            className="min-h-11 justify-center"
          >
            透明底
          </Chip>
        </div>
      ) : (
        <div className="grid gap-4 pt-4">
          <Button
            variant="outline"
            size="md"
            onClick={onOpenReasoning}
            className="h-auto min-h-14 justify-between rounded-[var(--radius-card)] bg-[var(--bg-1)] px-3 text-left"
          >
            <span>
              <span className="type-caption block text-[var(--fg-2)]">
                推理强度
              </span>
              <span className="mt-0.5 block type-label text-[var(--fg-0)]">
                {MOBILE_REASONING_OPTIONS.find(
                  (option) => option.value === reasoningEffort,
                )?.label ?? "默认"}
              </span>
            </span>
            <ChevronDown
              className="h-4 w-4 text-[var(--fg-2)]"
              aria-hidden
            />
          </Button>
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
      <span className="type-overline text-[var(--fg-2)]">{label}</span>
      <Select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          wrapperClassName="w-full"
          className="h-8 min-h-8 border-0 bg-transparent px-0 pr-7 type-label text-[var(--fg-0)] shadow-none focus:border-transparent"
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
