"use client";

import { Select } from "@/components/ui/primitives";
import { IMAGE_MODEL_OPTIONS, normalizeImageModel } from "@/lib/imageModels";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/useChatStore";

export function ImageModelSelect({ compact = false }: { compact?: boolean }) {
  const model = useChatStore((state) => normalizeImageModel(state.composer.params.model));
  const setModel = useChatStore((state) => state.setImageModel);
  return (
    <label className="grid min-w-40 shrink-0 gap-1 text-[var(--fg-0)]" title="生图模型">
      <span className={compact ? "sr-only" : "type-overline text-[var(--fg-2)]"}>生图模型</span>
      <Select
        aria-label="生图模型"
        value={model}
        onChange={(event) => setModel(normalizeImageModel(event.target.value))}
        className={cn(compact && "h-8 min-h-8 w-[220px] pl-2 pr-7 type-caption")}
      >
        {IMAGE_MODEL_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </Select>
    </label>
  );
}
