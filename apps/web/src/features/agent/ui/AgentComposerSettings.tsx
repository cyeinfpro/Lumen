"use client";

import { AspectRatioPicker } from "@/components/ui/composer/shared/AspectRatioPicker";
import { Select, Switch } from "@/components/ui/primitives";
import type { AgentDraft, AgentImageDefaults } from "../model/contracts";

export function AgentComposerSettings({
  draft,
  disabled,
  onAllowImageChange,
  onDefaultsChange,
}: {
  draft: AgentDraft;
  disabled: boolean;
  onAllowImageChange: (enabled: boolean) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
}) {
  const defaults = draft.imageDefaults;
  const settingsDisabled = disabled || !draft.allowImage;
  return (
    <div className="grid gap-4 p-4">
      <div className="flex min-h-11 items-center justify-between gap-4 border-b border-[var(--border-subtle)] pb-3">
        <div>
          <p className="type-label text-[var(--fg-0)]">允许生图</p>
          <p className="type-caption">关闭后只进行文本对话</p>
        </div>
        <Switch
          checked={draft.allowImage}
          onCheckedChange={onAllowImageChange}
          disabled={disabled}
          aria-label="允许 Agent 调用生图工具"
        />
      </div>

      <fieldset disabled={settingsDisabled} className="grid gap-4 disabled:opacity-50">
        <div className="grid grid-cols-2 gap-3">
          <SettingField label="数量">
            <Select
              value={defaults.count}
              onChange={(event) => onDefaultsChange({ count: Number(event.target.value) })}
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
                onDefaultsChange({ quality: event.target.value as AgentImageDefaults["quality"] })
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
                onDefaultsChange({ render_quality: event.target.value as AgentImageDefaults["render_quality"] })
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
                onDefaultsChange({ background: event.target.value as AgentImageDefaults["background"] })
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
              onDefaultsChange({ output_format: event.target.value as AgentImageDefaults["output_format"] })
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

function SettingField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid min-w-0 gap-1.5 type-caption text-[var(--fg-2)]">
      <span>{label}</span>
      {children}
    </label>
  );
}
