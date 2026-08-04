import { Check, X } from "lucide-react";

import { Button, Input, Select } from "@/components/ui/primitives";
import {
  type MemoryScopeOut,
  type MemoryStagingOut,
} from "@/lib/apiClient";

import { EmptyBlock, LoadingBlock } from "../memoryStateBlocks";
import { formatTime } from "../memoryPageUtils";
import { SectionHeader, TypeBadge } from "./MemorySectionPrimitives";

export function MemoryStagingSection({
  staging,
  scopes,
  edits,
  pending,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  staging: MemoryStagingOut[];
  scopes: MemoryScopeOut[];
  edits: Record<string, string>;
  pending: boolean;
  onEdit: (id: string, value: string) => void;
  onScopeChange: (id: string, scopeId: string) => void;
  onAccept: (item: MemoryStagingOut) => void;
  onReject: (id: string) => void;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader
        title="建议加入记忆"
        suffix={`${staging.length} 条`}
      />
      <MemoryStagingList
        staging={staging}
        scopes={scopes}
        edits={edits}
        pending={pending}
        onEdit={onEdit}
        onScopeChange={onScopeChange}
        onAccept={onAccept}
        onReject={onReject}
      />
    </section>
  );
}

function MemoryStagingList({
  staging,
  scopes,
  edits,
  pending,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  staging: MemoryStagingOut[];
  scopes: MemoryScopeOut[];
  edits: Record<string, string>;
  pending: boolean;
  onEdit: (id: string, value: string) => void;
  onScopeChange: (id: string, scopeId: string) => void;
  onAccept: (item: MemoryStagingOut) => void;
  onReject: (id: string) => void;
}) {
  if (pending) return <LoadingBlock />;
  if (staging.length === 0) {
    return <EmptyBlock text="暂无待确认候选。" />;
  }
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {staging.map((item) => (
        <MemoryStagingRow
          key={item.id}
          item={item}
          scopes={scopes}
          value={edits[item.id] ?? item.content}
          onEdit={(value) => onEdit(item.id, value)}
          onScopeChange={(scopeId) => onScopeChange(item.id, scopeId)}
          onAccept={() => onAccept(item)}
          onReject={() => onReject(item.id)}
        />
      ))}
    </div>
  );
}

function MemoryStagingRow({
  item,
  scopes,
  value,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  item: MemoryStagingOut;
  scopes: MemoryScopeOut[];
  value: string;
  onEdit: (value: string) => void;
  onScopeChange: (scopeId: string) => void;
  onAccept: () => void;
  onReject: () => void;
}) {
  return (
    <div className="p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <TypeBadge type={item.type} />
        <span className="type-caption text-[var(--fg-2)]">
          置信度 {Math.round(item.confidence * 100)}%
        </span>
        <span className="type-caption text-[var(--fg-2)]">
          {formatTime(item.created_at)}
        </span>
      </div>
      <Input
        value={value}
        onChange={(event) => onEdit(event.target.value)}
        aria-label="候选记忆内容"
        wrapperClassName="mb-3"
      />
      <div className="flex flex-wrap gap-2">
        <Select
          value={item.scope_id}
          onChange={(event) => onScopeChange(event.target.value)}
          aria-label="候选记忆作用域"
          className="min-w-0 sm:h-9"
        >
          {scopes.map((scope) => (
            <option key={scope.id} value={scope.id}>
              {scope.is_default ? "默认" : scope.name}
              {item.recommended_scope_id === scope.id ? " · 推荐" : ""}
            </option>
          ))}
        </Select>
        <Button
          variant="ghost"
          size="sm"
          onClick={onAccept}
          leftIcon={<Check className="h-3.5 w-3.5" />}
          className="bg-success-soft text-success hover:bg-success/20"
        >
          接受
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onReject}
          leftIcon={<X className="h-3.5 w-3.5" />}
        >
          拒绝
        </Button>
      </div>
    </div>
  );
}
