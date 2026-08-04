import { Plus } from "lucide-react";

import { Button, Input, Select } from "@/components/ui/primitives";
import { type MemoryType } from "@/lib/apiClient";

import { TYPE_OPTIONS } from "../memoryPageUtils";

export function ManualMemorySection({
  typeCounts,
  memoryType,
  content,
  creating,
  onTypeChange,
  onContentChange,
  onCreate,
}: {
  typeCounts: Record<string, number>;
  memoryType: MemoryType;
  content: string;
  creating: boolean;
  onTypeChange: (type: MemoryType) => void;
  onContentChange: (content: string) => void;
  onCreate: () => void;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="type-card-title">手动添加</h2>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            手动记忆按 1.0 置信度写入。
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5 type-caption text-[var(--fg-2)]">
          {TYPE_OPTIONS.map((option) => (
            <span key={option.value}>
              {option.label} {typeCounts[option.value] ?? 0}
            </span>
          ))}
        </div>
      </div>
      <div className="grid gap-2 sm:grid-cols-[150px_minmax(0,1fr)_auto]">
        <Select
          value={memoryType}
          onChange={(event) =>
            onTypeChange(event.target.value as MemoryType)
          }
          aria-label="记忆类型"
        >
          {TYPE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
        <Input
          value={content}
          onChange={(event) => onContentChange(event.target.value)}
          placeholder="例如：偏好 200 字以内的回答"
          maxLength={200}
          aria-label="记忆内容"
        />
        <Button
          variant="primary"
          size="md"
          disabled={!content.trim() || creating}
          loading={creating}
          onClick={onCreate}
          leftIcon={!creating ? <Plus className="h-4 w-4" /> : undefined}
        >
          添加
        </Button>
      </div>
    </section>
  );
}
