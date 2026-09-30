"use client";

import { useId } from "react";
import { Select } from "@/components/ui/primitives";
import type { PromptEnhancementModel } from "@/lib/textModelCapabilities";

export function PromptEnhancementModelSelect({ model, onChange, disabled = false }: {
  model?: PromptEnhancementModel;
  onChange: (model: PromptEnhancementModel | undefined) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="mb-4 grid gap-2 border-b border-[var(--border-subtle)] pb-4">
      <label htmlFor={id} className="type-caption text-[var(--fg-2)]">提示词优化模型</label>
      <Select id={id} value={model ?? ""} disabled={disabled} aria-describedby={`${id}-hint`}
        onChange={(event) => onChange((event.target.value || undefined) as PromptEnhancementModel | undefined)}>
        <option value="">默认 · 保持原有策略</option>
        <option value="gpt-6-astra">GPT-6 Astra</option>
        <option value="gpt-6-sol">GPT-6 Sol</option>
        <option value="gpt-6-luna">GPT-6 Luna</option>
      </Select>
      <p id={`${id}-hint`} className="type-caption text-[var(--fg-2)]">
        仅用于润色，不改变聊天或生图模型。需供应商支持，按实际用量计费；原文在应用候选前保持不变。
      </p>
    </div>
  );
}
