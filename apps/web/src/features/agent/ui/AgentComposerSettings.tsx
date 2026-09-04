"use client";

import { FileText, Globe2, ImageIcon } from "lucide-react";
import { AspectRatioPicker } from "@/components/ui/composer/shared/AspectRatioPicker";
import { Select, Switch } from "@/components/ui/primitives";
import type {
  AgentDraft,
  AgentImageDefaults,
  AgentModelOption,
  AgentReasoningEffort,
} from "../model/contracts";

export interface AgentQuickSettingsProps {
  draft: AgentDraft;
  disabled: boolean;
  defaultModel: string | null;
  modelOptions: AgentModelOption[];
  imageGenerationAvailable: boolean;
  onModelChange: (model: string | null) => void;
  onReasoningEffortChange: (effort: AgentReasoningEffort) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
}

export function AgentQuickSettings({
  draft,
  disabled,
  defaultModel,
  modelOptions,
  imageGenerationAvailable,
  onModelChange,
  onReasoningEffortChange,
  onDefaultsChange,
}: AgentQuickSettingsProps) {
  const defaults = draft.imageDefaults;
  const selectedModel = draft.model ?? defaultModel;
  const selectedOption = modelOptions.find(
    (option) => option.model === selectedModel,
  );
  const reasoningDisabled =
    disabled || selectedOption?.reasoning_supported === false;
  const imageSettingsDisabled =
    disabled || !draft.allowImage || !imageGenerationAvailable;

  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <SettingField label="模型">
          <Select
            value={draft.model ?? ""}
            onChange={(event) => {
              const model = event.target.value || null;
              onModelChange(model);
              const option = modelOptions.find((item) => item.model === model);
              if (option && !option.reasoning_supported) {
                onReasoningEffortChange("none");
              }
            }}
            disabled={disabled}
            aria-label="Agent 模型"
          >
            <option value="">
              自动{defaultModel ? ` · ${defaultModel}` : ""}
            </option>
            {modelOptions.map((option) => (
              <option key={option.model} value={option.model}>
                {option.model}
              </option>
            ))}
          </Select>
        </SettingField>
        <SettingField label="推理强度">
          <Select
            value={reasoningDisabled ? "none" : draft.reasoningEffort ?? "auto"}
            onChange={(event) =>
              onReasoningEffortChange(
                event.target.value as AgentReasoningEffort,
              )
            }
            disabled={reasoningDisabled}
            aria-label="Agent 推理强度"
          >
            <option value="auto">自动</option>
            <option value="none">关闭</option>
            <option value="minimal">极低</option>
            <option value="low">低</option>
            <option value="medium">中</option>
            <option value="high">高</option>
            <option value="xhigh">超高</option>
            <option value="max">最大</option>
          </Select>
        </SettingField>
      </div>

      <fieldset
        disabled={imageSettingsDisabled}
        className="grid gap-4 disabled:opacity-50"
      >
        <legend className="mb-2 type-caption text-[var(--fg-2)]">
          生图参数
        </legend>
        <div className="grid grid-cols-2 gap-3">
          <SettingField label="数量">
            <Select
              value={defaults.count}
              onChange={(event) =>
                onDefaultsChange({ count: Number(event.target.value) })
              }
              aria-label="默认图片数量"
            >
              {[1, 2, 3, 4].map((count) => (
                <option key={count} value={count}>{count} 张</option>
              ))}
            </Select>
          </SettingField>
          <SettingField label="分辨率">
            <Select
              value={defaults.quality}
              onChange={(event) =>
                onDefaultsChange({
                  quality: event.target.value as AgentImageDefaults["quality"],
                })
              }
              aria-label="默认图片分辨率"
            >
              <option value="1k">1K</option>
              <option value="2k">2K</option>
              <option value="4k">4K</option>
            </Select>
          </SettingField>
        </div>

        <SettingField label="宽高比">
          <AspectRatioPicker
            value={defaults.aspect_ratio}
            onChange={(aspect_ratio) => onDefaultsChange({ aspect_ratio })}
            variant="sheet"
            className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-2"
          />
        </SettingField>

        <div className="grid grid-cols-2 gap-3">
          <SettingField label="渲染质量">
            <Select
              value={defaults.render_quality}
              onChange={(event) =>
                onDefaultsChange({
                  render_quality: event.target.value as AgentImageDefaults["render_quality"],
                })
              }
              aria-label="默认渲染质量"
            >
              <option value="auto">自动</option>
              <option value="low">草稿</option>
              <option value="medium">标准</option>
              <option value="high">精细</option>
            </Select>
          </SettingField>
          <SettingField label="背景">
            <Select
              value={defaults.background}
              onChange={(event) =>
                onDefaultsChange({
                  background: event.target.value as AgentImageDefaults["background"],
                })
              }
              aria-label="默认背景"
            >
              <option value="auto">自动</option>
              <option value="opaque">不透明</option>
              <option value="transparent">透明</option>
            </Select>
          </SettingField>
        </div>

        <SettingField label="输出格式">
          <Select
            value={defaults.output_format}
            onChange={(event) =>
              onDefaultsChange({
                output_format: event.target.value as AgentImageDefaults["output_format"],
              })
            }
            aria-label="默认输出格式"
          >
            <option value="png">PNG</option>
            <option value="jpeg">JPEG</option>
            <option value="webp">WebP</option>
          </Select>
        </SettingField>
      </fieldset>
    </div>
  );
}

export function AgentComposerSettings({
  draft,
  disabled,
  defaultModel,
  modelOptions,
  imageGenerationAvailable,
  onModelChange,
  onAllowImageChange,
  onAllowWebSearchChange,
  onAllowFileToolsChange,
  onReasoningEffortChange,
  onDefaultsChange,
}: AgentQuickSettingsProps & {
  onAllowImageChange: (enabled: boolean) => void;
  onAllowWebSearchChange: (enabled: boolean) => void;
  onAllowFileToolsChange: (enabled: boolean) => void;
}) {
  return (
    <div className="grid gap-4 p-4">
      <AgentQuickSettings
        draft={draft}
        disabled={disabled}
        defaultModel={defaultModel}
        modelOptions={modelOptions}
        imageGenerationAvailable={imageGenerationAvailable}
        onModelChange={onModelChange}
        onReasoningEffortChange={onReasoningEffortChange}
        onDefaultsChange={onDefaultsChange}
      />

      <div className="grid divide-y divide-[var(--border-subtle)] border-y border-[var(--border-subtle)]">
        <ToolToggle
          icon={<Globe2 className="h-4 w-4" aria-hidden />}
          label="联网搜索"
          detail="查询公开网页与来源"
          checked={draft.allowWebSearch}
          disabled={disabled}
          onChange={onAllowWebSearchChange}
        />
        <ToolToggle
          icon={<FileText className="h-4 w-4" aria-hidden />}
          label="文件工具"
          detail={draft.files.length > 0 ? `已添加 ${draft.files.length} 个文件` : "读取本轮文本文件"}
          checked={draft.allowFileTools}
          disabled={disabled}
          onChange={onAllowFileToolsChange}
        />
        <ToolToggle
          icon={<ImageIcon className="h-4 w-4" aria-hidden />}
          label="生成图片"
          detail={imageGenerationAvailable ? "允许提交异步生图任务" : "生图工具未就绪"}
          checked={draft.allowImage && imageGenerationAvailable}
          disabled={disabled || !imageGenerationAvailable}
          onChange={onAllowImageChange}
        />
      </div>
    </div>
  );
}

function ToolToggle({
  icon,
  label,
  detail,
  checked,
  disabled,
  onChange,
}: {
  icon: React.ReactNode;
  label: string;
  detail: string;
  checked: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex min-h-14 items-center gap-3 py-2.5">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-accent">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block type-label text-[var(--fg-0)]">{label}</span>
        <span className="block truncate type-caption text-[var(--fg-2)]">{detail}</span>
      </span>
      <Switch
        checked={checked}
        onCheckedChange={onChange}
        disabled={disabled}
        aria-label={label}
      />
    </div>
  );
}

function SettingField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid min-w-0 gap-1.5 type-caption text-[var(--fg-2)]">
      <span>{label}</span>
      {children}
    </label>
  );
}
