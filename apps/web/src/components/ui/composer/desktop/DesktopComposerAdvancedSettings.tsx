"use client";

import type { ReactNode } from "react";
import {
  Code2,
  FileSearch,
  Globe2,
  ImagePlus,
  Layers2,
  X,
  Zap,
} from "lucide-react";

import { Button, IconButton, Select } from "@/components/ui/primitives";
import { AspectRatioPicker } from "../shared/AspectRatioPicker";
import type { ComposerMode } from "@/store/chat/types";
import type { ReasoningEffort } from "@/store/useChatStore";
import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  COUNT_OPTIONS,
  QUALITY_OPTIONS,
  RENDER_QUALITY_OPTIONS,
} from "./DesktopComposerExecutionControls";

const REASONING_OPTIONS: {
  value: ReasoningEffort;
  label: string;
  hint: string;
}[] = [
  { value: "none", label: "最快", hint: "直接回复" },
  { value: "low", label: "低", hint: "轻量思考" },
  { value: "medium", label: "中", hint: "平衡" },
  { value: "high", label: "高", hint: "多想一步" },
  { value: "xhigh", label: "很高", hint: "更慢，适合复杂问题" },
];

interface AdvancedComposerSettingsProps {
  mode: ComposerMode;
  quality: Quality;
  onQualityChange: (value: Quality) => void;
  renderQuality: RenderQualityChoice;
  onRenderQualityChange: (value: RenderQualityChoice) => void;
  aspect: AspectRatio;
  onAspectChange: (value: AspectRatio) => void;
  count: number;
  onCountChange: (value: number) => void;
  reasoningEffort: ReasoningEffort;
  onReasoningEffortChange: (value: ReasoningEffort) => void;
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
  onClose: () => void;
}

export function AdvancedComposerSettings({
  mode,
  quality,
  onQualityChange,
  renderQuality,
  onRenderQualityChange,
  aspect,
  onAspectChange,
  count,
  onCountChange,
  reasoningEffort,
  onReasoningEffortChange,
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
  onClose,
}: AdvancedComposerSettingsProps) {
  const imageMode = mode === "image";

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <div>
          <p className="type-label text-[var(--fg-0)]">执行设置</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">仅用于下一次提交</p>
        </div>
        {/* @hit-area-ok: desktop-only popover; mobile uses MobileAdvancedSettings. */}
        <IconButton
          size="sm"
          onClick={onClose}
          aria-label="关闭执行设置"
          tooltip="关闭执行设置"
          className="text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
        >
          <X className="h-4 w-4" aria-hidden />
        </IconButton>
      </div>

      <div className="min-h-0 overflow-y-auto p-4">
        {imageMode ? (
          <div className="grid gap-5 lg:grid-cols-[minmax(220px,0.72fr)_minmax(360px,1.28fr)]">
            <div className="grid content-start gap-4">
              <section className="grid gap-2" aria-labelledby="image-output-settings">
                <h3
                  id="image-output-settings"
                  className="type-caption text-[var(--fg-2)]"
                >
                  输出
                </h3>
                <div className="grid grid-cols-2 gap-2">
                  <SettingSelect
                    label="尺寸"
                    value={quality}
                    onChange={(value) => onQualityChange(value as Quality)}
                    options={QUALITY_OPTIONS}
                  />
                  <SettingSelect
                    label="质量"
                    value={renderQuality}
                    onChange={(value) =>
                      onRenderQualityChange(value as RenderQualityChoice)
                    }
                    options={RENDER_QUALITY_OPTIONS}
                  />
                  <SettingSelect
                    label="数量"
                    value={String(count)}
                    onChange={(value) => onCountChange(Number(value))}
                    options={COUNT_OPTIONS.map((value) => ({
                      value: String(value),
                      label: `${value} 张`,
                    }))}
                  />
                </div>
              </section>

              <section className="grid gap-2" aria-labelledby="image-background-settings">
                <h3
                  id="image-background-settings"
                  className="type-caption text-[var(--fg-2)]"
                >
                  背景
                </h3>
                <ToggleRow
                  active={transparentBackground}
                  onClick={() =>
                    onTransparentBackgroundChange(!transparentBackground)
                  }
                  icon={<Layers2 className="h-4 w-4" aria-hidden />}
                  label="透明底"
                  detail="Alpha 通道"
                />
              </section>
            </div>

            <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]">
              <AspectRatioPicker
                value={aspect}
                onChange={onAspectChange}
                className="w-full max-w-none"
              />
            </div>
          </div>
        ) : (
          <div className="grid gap-5">
            <section className="grid gap-2" aria-labelledby="reasoning-settings">
              <h3
                id="reasoning-settings"
                className="type-caption text-[var(--fg-2)]"
              >
                推理强度
              </h3>
              <div className="grid gap-2 sm:grid-cols-5">
                {REASONING_OPTIONS.map((option) => {
                  const active = option.value === reasoningEffort;
                  return (
                    <Button
                      key={option.value}
                      variant="outline"
                      size="md"
                      onClick={() => onReasoningEffortChange(option.value)}
                      aria-pressed={active}
                      className={cn(
                        "h-auto min-h-14 justify-start rounded-[var(--radius-card)] px-3 py-2 text-left",
                        active
                          ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
                          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
                      )}
                    >
                      <span className="type-label block">
                        {option.label}
                      </span>
                      <span className="mt-0.5 block type-overline text-[var(--fg-2)]">
                        {option.hint}
                      </span>
                    </Button>
                  );
                })}
              </div>
            </section>

            <section className="grid gap-2" aria-labelledby="tool-settings">
              <h3
                id="tool-settings"
                className="type-caption text-[var(--fg-2)]"
              >
                工具
              </h3>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                <ToggleRow
                  active={webSearch}
                  onClick={() => onWebSearchChange(!webSearch)}
                  icon={<Globe2 className="h-4 w-4" aria-hidden />}
                  label="联网搜索"
                  detail="获取最新网页信息"
                />
                <ToggleRow
                  active={fileSearch}
                  onClick={() => onFileSearchChange(!fileSearch)}
                  icon={<FileSearch className="h-4 w-4" aria-hidden />}
                  label="文件检索"
                  detail="搜索已配置资料"
                />
                <ToggleRow
                  active={codeInterpreter}
                  onClick={() => onCodeInterpreterChange(!codeInterpreter)}
                  icon={<Code2 className="h-4 w-4" aria-hidden />}
                  label="代码工具"
                  detail="运行分析与计算"
                />
                <ToggleRow
                  active={imageGeneration}
                  onClick={() => onImageGenerationChange(!imageGeneration)}
                  icon={<ImagePlus className="h-4 w-4" aria-hidden />}
                  label="对话生图"
                  detail="允许回答中生成图片"
                />
                <ToggleRow
                  active={fast}
                  onClick={() => onFastChange(!fast)}
                  icon={<Zap className="h-4 w-4" aria-hidden />}
                  label="Fast"
                  detail="优先更快完成"
                />
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function SettingSelect({
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
    <label className="grid gap-1.5">
      <span className="type-overline text-[var(--fg-2)]">{label}</span>
      <Select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-10 bg-[var(--bg-1)] type-caption text-[var(--fg-0)] hover:bg-[var(--bg-2)]"
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

function ToggleRow({
  active,
  onClick,
  icon,
  label,
  detail,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  label: string;
  detail: string;
}) {
  return (
    <Button
      variant="outline"
      size="md"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "h-auto min-h-14 justify-start gap-3 rounded-[var(--radius-card)] px-3 text-left",
        active
          ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
      )}
    >
      <span
        className={cn(
          "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)]",
          active
            ? "bg-[var(--accent)] text-[var(--accent-on)]"
            : "bg-[var(--bg-2)] text-[var(--fg-2)]",
        )}
      >
        {icon}
      </span>
      <span className="min-w-0">
        <span className="type-label block">{label}</span>
        <span className="mt-0.5 block truncate type-overline text-[var(--fg-2)]">
          {detail}
        </span>
      </span>
    </Button>
  );
}
