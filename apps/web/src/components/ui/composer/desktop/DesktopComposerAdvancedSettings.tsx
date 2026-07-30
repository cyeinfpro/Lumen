"use client";

import type { ReactNode } from "react";
import {
  ChevronDown,
  Code2,
  FileSearch,
  Globe2,
  ImagePlus,
  X,
  Zap,
} from "lucide-react";

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
  onClose,
}: AdvancedComposerSettingsProps) {
  const imageMode = mode === "image";

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="text-[13px] font-semibold text-[var(--fg-0)]">
          执行设置
        </p>
        {/* @hit-area-ok: desktop-only popover; mobile uses MobileAdvancedSettings. */}
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭执行设置"
          className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="min-h-0 overflow-y-auto p-4">
        {imageMode ? (
          <div className="grid gap-5 lg:grid-cols-[minmax(220px,0.72fr)_minmax(360px,1.28fr)]">
            <div className="grid content-start gap-4">
              <section className="grid gap-2" aria-labelledby="image-output-settings">
                <h3
                  id="image-output-settings"
                  className="text-[11px] font-medium text-[var(--fg-2)]"
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

              <section className="grid gap-2" aria-labelledby="image-speed-settings">
                <h3
                  id="image-speed-settings"
                  className="text-[11px] font-medium text-[var(--fg-2)]"
                >
                  执行
                </h3>
                <ToggleRow
                  active={fast}
                  onClick={() => onFastChange(!fast)}
                  icon={<Zap className="h-4 w-4" aria-hidden />}
                  label="Fast"
                  detail="优先更快完成"
                />
              </section>
            </div>

            <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/56">
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
                className="text-[11px] font-medium text-[var(--fg-2)]"
              >
                推理强度
              </h3>
              <div className="grid gap-2 sm:grid-cols-5">
                {REASONING_OPTIONS.map((option) => {
                  const active = option.value === reasoningEffort;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => onReasoningEffortChange(option.value)}
                      aria-pressed={active}
                      className={cn(
                        "min-h-14 rounded-[var(--radius-card)] border px-3 py-2 text-left",
                        "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                        active
                          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
                          : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
                      )}
                    >
                      <span className="block text-[12px] font-medium">
                        {option.label}
                      </span>
                      <span className="mt-0.5 block text-[10px] text-[var(--fg-2)]">
                        {option.hint}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>

            <section className="grid gap-2" aria-labelledby="tool-settings">
              <h3
                id="tool-settings"
                className="text-[11px] font-medium text-[var(--fg-2)]"
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
      <span className="text-[10px] text-[var(--fg-2)]">{label}</span>
      <span className="relative">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-10 w-full appearance-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 pr-8 text-[12px] text-[var(--fg-0)] outline-none transition-colors hover:bg-[var(--bg-2)] focus-visible:shadow-[var(--ring)]"
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]"
          aria-hidden
        />
      </span>
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
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex min-h-14 items-center gap-3 rounded-[var(--radius-card)] border px-3 text-left",
        "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
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
        <span className="block text-[12px] font-medium">{label}</span>
        <span className="mt-0.5 block truncate text-[10px] text-[var(--fg-2)]">
          {detail}
        </span>
      </span>
    </button>
  );
}
