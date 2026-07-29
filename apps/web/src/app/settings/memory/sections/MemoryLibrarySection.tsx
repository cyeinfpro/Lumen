import {
  Download,
  Pin,
  RefreshCw,
  Search,
} from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import {
  type MemoryItemOut,
  type MemoryScopeOut,
} from "@/lib/apiClient";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "../memoryStateBlocks";
import { formatTime } from "../memoryPageUtils";
import type { MemoryPatchBody } from "../types";
import { SectionHeader, TypeBadge } from "./MemorySectionPrimitives";

export function MemoryLibrarySection({
  memories,
  filteredMemories,
  scopes,
  selectedScope,
  selectedMemoryIds,
  editing,
  search,
  pending,
  error,
  bulkMoving,
  onRefresh,
  onExport,
  onSearchChange,
  onBulkMove,
  onToggleSelected,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memories: MemoryItemOut[];
  filteredMemories: MemoryItemOut[];
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  editing: Record<string, string>;
  search: string;
  pending: boolean;
  error: unknown;
  bulkMoving: boolean;
  onRefresh: () => void;
  onExport: () => void;
  onSearchChange: (value: string) => void;
  onBulkMove: (scopeId: string) => void;
  onToggleSelected: (id: string, checked: boolean) => void;
  onEditValue: (id: string, value: string) => void;
  onSaveEdit: (memory: MemoryItemOut) => void;
  onCancelEdit: (id: string) => void;
  onPatch: (id: string, body: MemoryPatchBody) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader
        title="记忆库"
        suffix={`${filteredMemories.length}/${memories.length} 条`}
        actions={
          <>
            <IconButton
              variant="outline"
              size="md"
              onClick={onRefresh}
              aria-label="刷新记忆"
              tooltip="刷新"
            >
              <RefreshCw className="h-4 w-4" />
            </IconButton>
            <Button
              variant="outline"
              size="sm"
              onClick={onExport}
              leftIcon={<Download className="h-3.5 w-3.5" />}
            >
              {copy.action.export}
            </Button>
          </>
        }
      />
      <MemoryLibraryToolbar
        scopes={scopes}
        selectedScope={selectedScope}
        selectedMemoryIds={selectedMemoryIds}
        search={search}
        bulkMoving={bulkMoving}
        onSearchChange={onSearchChange}
        onBulkMove={onBulkMove}
      />
      <MemoryLibraryList
        memories={filteredMemories}
        scopes={scopes}
        selectedScope={selectedScope}
        selectedMemoryIds={selectedMemoryIds}
        editing={editing}
        pending={pending}
        error={error}
        onRetry={onRefresh}
        onToggleSelected={onToggleSelected}
        onEditValue={onEditValue}
        onSaveEdit={onSaveEdit}
        onCancelEdit={onCancelEdit}
        onPatch={onPatch}
        onDelete={onDelete}
      />
    </section>
  );
}

function MemoryLibraryToolbar({
  scopes,
  selectedScope,
  selectedMemoryIds,
  search,
  bulkMoving,
  onSearchChange,
  onBulkMove,
}: {
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  search: string;
  bulkMoving: boolean;
  onSearchChange: (value: string) => void;
  onBulkMove: (scopeId: string) => void;
}) {
  const showBulkActions =
    selectedScope === "all" && selectedMemoryIds.size > 0;
  const searchPlaceholder =
    selectedScope === "all" ? "跨作用域搜索" : "搜索当前作用域";
  return (
    <div className="flex flex-col gap-2 border-t border-[var(--border-subtle)] p-3 sm:flex-row sm:items-center sm:justify-between">
      <label className="relative min-w-0 flex-1">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={searchPlaceholder}
          className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] pl-9 pr-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60 sm:h-9"
        />
      </label>
      {showBulkActions ? (
        <div className="flex flex-wrap items-center gap-2 type-caption text-[var(--fg-1)]">
          <span>已选 {selectedMemoryIds.size} 条</span>
          <select
            disabled={bulkMoving}
            onChange={(event) => {
              const scopeId = event.target.value;
              if (!scopeId) return;
              onBulkMove(scopeId);
              event.currentTarget.value = "";
            }}
            className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-xs text-[var(--fg-0)] outline-none sm:h-9"
            defaultValue=""
          >
            <option value="" disabled>
              批量改作用域
            </option>
            {scopes.map((scope) => (
              <option key={scope.id} value={scope.id}>
                {scope.is_default ? "默认" : scope.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </div>
  );
}

function MemoryLibraryList({
  memories,
  scopes,
  selectedScope,
  selectedMemoryIds,
  editing,
  pending,
  error,
  onRetry,
  onToggleSelected,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memories: MemoryItemOut[];
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  editing: Record<string, string>;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  onToggleSelected: (id: string, checked: boolean) => void;
  onEditValue: (id: string, value: string) => void;
  onSaveEdit: (memory: MemoryItemOut) => void;
  onCancelEdit: (id: string) => void;
  onPatch: (id: string, body: MemoryPatchBody) => void;
  onDelete: (id: string) => void;
}) {
  if (pending) return <LoadingBlock />;
  if (error) return <ErrorBlock error={error} onRetry={onRetry} />;
  if (memories.length === 0) {
    return <EmptyBlock text="当前作用域还没有记忆。" />;
  }
  const selectable = selectedScope === "all";
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {memories.map((memory) => (
        <MemoryRow
          key={memory.id}
          memory={memory}
          scopes={scopes}
          selectable={selectable}
          selected={selectedMemoryIds.has(memory.id)}
          onToggleSelected={(checked) =>
            onToggleSelected(memory.id, checked)
          }
          editingValue={editing[memory.id]}
          onEditValue={(value) => onEditValue(memory.id, value)}
          onSaveEdit={() => onSaveEdit(memory)}
          onCancelEdit={() => onCancelEdit(memory.id)}
          onPatch={(body) => onPatch(memory.id, body)}
          onDelete={() => onDelete(memory.id)}
        />
      ))}
    </div>
  );
}

function MemoryRow({
  memory,
  scopes,
  selectable = false,
  selected = false,
  onToggleSelected,
  editingValue,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memory: MemoryItemOut;
  scopes: MemoryScopeOut[];
  selectable?: boolean;
  selected?: boolean;
  onToggleSelected?: (checked: boolean) => void;
  editingValue?: string;
  onEditValue: (value: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onPatch: (body: MemoryPatchBody) => void;
  onDelete: () => void;
}) {
  const isEditing = editingValue != null;
  return (
    <div className={["p-4", memory.disabled ? "opacity-55" : ""].join(" ")}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {selectable ? (
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onToggleSelected?.(event.target.checked)}
            className="h-4 w-4 rounded border-[var(--border-strong)] bg-[var(--bg-2)]"
            aria-label="选择记忆"
          />
        ) : null}
        <TypeBadge type={memory.type} />
        <span className="type-caption text-[var(--fg-2)]">
          {memory.source}
        </span>
        <span className="type-caption text-[var(--fg-2)]">
          {formatTime(memory.updated_at)}
        </span>
        {memory.pinned ? (
          <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
            <Pin className="h-2.5 w-2.5" />
            pinned
          </span>
        ) : null}
      </div>
      {isEditing ? (
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <input
            value={editingValue}
            onChange={(event) => onEditValue(event.target.value)}
            className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-3 text-base text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 sm:h-10 sm:text-sm"
          />
          <div className="flex gap-2">
            <Button variant="primary" size="md" onClick={onSaveEdit}>
              {copy.action.save}
            </Button>
            <Button variant="outline" size="md" onClick={onCancelEdit}>
              {copy.action.cancel}
            </Button>
          </div>
        </div>
      ) : (
        <p className="type-body-sm leading-6 text-[var(--fg-0)]">
          {memory.content}
        </p>
      )}
      {memory.source_excerpt ? (
        <p className="mt-2 truncate type-caption text-[var(--fg-2)]">
          来源：{memory.source_excerpt}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onEditValue(memory.content)}
        >
          {copy.action.edit}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPatch({ pinned: !memory.pinned })}
        >
          {memory.pinned ? "取消 Pin" : "Pin"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPatch({ disabled: !memory.disabled })}
        >
          {memory.disabled ? "启用" : "停用"}
        </Button>
        <select
          value={memory.scope_id}
          onChange={(event) => onPatch({ scope_id: event.target.value })}
          className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-2 text-base text-[var(--fg-1)] outline-none sm:h-8 sm:text-xs"
        >
          {scopes.map((scope) => (
            <option key={scope.id} value={scope.id}>
              {scope.is_default ? "默认" : scope.name}
            </option>
          ))}
        </select>
        <Button
          variant="outline"
          size="sm"
          onClick={onDelete}
          className="text-danger hover:text-danger"
        >
          {copy.action.delete}
        </Button>
      </div>
    </div>
  );
}
