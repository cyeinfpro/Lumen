"use client";

import { Button } from "@/components/ui/primitives";
import { gpt6ModelFamily, normalizeTextReasoning } from "@/lib/textModelCapabilities";
import type { AgentReasoningEffort } from "../model/contracts";

const PRESETS = [
  { value: "low", label: "快速", detail: "较低推理强度，适合明确、简单的任务" },
  { value: "medium", label: "均衡", detail: "中等推理强度，兼顾速度与分析" },
  { value: "high", label: "深入", detail: "较高推理强度，适合复杂任务" },
] as const;

export function AgentReasoningPresets({ model, effort, disabled, onChange }: {
  model: string | null;
  effort: AgentReasoningEffort;
  disabled: boolean;
  onChange: (effort: AgentReasoningEffort) => void;
}) {
  const effective = normalizeTextReasoning(model, effort);
  return (
    <div className="grid gap-2">
      <div role="group" aria-label="推理强度快捷设置" className="grid grid-cols-3 gap-2">
        {PRESETS.map((preset) => (
          <Button key={preset.value} variant={effective === preset.value ? "secondary" : "ghost"}
            size="sm" disabled={disabled} aria-pressed={effective === preset.value}
            title={preset.detail} onClick={() => onChange(preset.value)}>
            {preset.label}
          </Button>
        ))}
      </div>
      <p className="type-caption text-[var(--fg-2)]">
        {gpt6ModelFamily(model) === "astra" ? "Astra 始终启用推理，最低为低；自动由模型决定。" : "自动由模型决定；快捷设置只调整推理强度。"}
        {effective !== effort ? "旧的关闭或极低设置将按低强度执行。" : ""}
        {effective === "high" || effective === "xhigh" || effective === "max" ? "更高强度可能增加耗时和费用。" : ""}
      </p>
    </div>
  );
}
