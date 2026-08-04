"use client";

import { useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { type MemoryScopeOut } from "@/lib/apiClient";

export function MemoryScopeSidebar({
  scopes,
  selectedScope,
  newScopeName,
  newScopeEmoji,
  creating,
  onSelectScope,
  onRenameScope,
  onDeleteScope,
  onNewScopeNameChange,
  onNewScopeEmojiChange,
  onCreateScope,
}: {
  scopes: MemoryScopeOut[];
  selectedScope: string;
  newScopeName: string;
  newScopeEmoji: string;
  creating: boolean;
  onSelectScope: (scopeId: string) => void;
  onRenameScope: (scopeId: string, name: string) => void;
  onDeleteScope: (scopeId: string) => void;
  onNewScopeNameChange: (name: string) => void;
  onNewScopeEmojiChange: (emoji: string) => void;
  onCreateScope: () => void;
}) {
  const totalCount = scopes.reduce((sum, scope) => sum + scope.count, 0);
  return (
    <aside className="min-w-0 space-y-3">
      <div className="flex min-w-0 gap-1 overflow-x-auto rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-2 [scrollbar-width:none] lg:block lg:overflow-visible lg:p-3 [&::-webkit-scrollbar]:hidden">
        <button
          type="button"
          onClick={() => onSelectScope("all")}
          className={scopeButtonClass(selectedScope === "all")}
        >
          <span>全部</span>
          <span>{totalCount}</span>
        </button>
        {scopes.map((scope) => (
          <ScopeButton
            key={scope.id}
            scope={scope}
            active={selectedScope === scope.id}
            onSelect={() => onSelectScope(scope.id)}
            onRename={(name) => onRenameScope(scope.id, name)}
            onDelete={() => onDeleteScope(scope.id)}
          />
        ))}
      </div>

      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-3">
        <div className="mb-2 type-caption font-medium text-[var(--fg-1)]">
          新作用域
        </div>
        <div className="flex gap-2">
          <input
            value={newScopeEmoji}
            onChange={(event) =>
              onNewScopeEmojiChange(event.target.value.slice(0, 4))
            }
            placeholder="图标"
            aria-label="作用域图标"
            className="control-shell type-body-sm h-10 w-14 px-2 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-accent-border focus:shadow-[var(--ring)] max-sm:min-h-11"
          />
          <input
            value={newScopeName}
            onChange={(event) => onNewScopeNameChange(event.target.value)}
            placeholder="工作"
            aria-label="作用域名称"
            className="control-shell type-body-sm h-10 min-w-0 flex-1 px-3 text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-accent-border focus:shadow-[var(--ring)] max-sm:min-h-11"
          />
          <IconButton
            variant="primary"
            disabled={!newScopeName.trim() || creating}
            onClick={onCreateScope}
            aria-label="创建作用域"
          >
            <Plus className="h-4 w-4" />
          </IconButton>
        </div>
      </div>
    </aside>
  );
}

function ScopeButton({
  scope,
  active,
  onSelect,
  onRename,
  onDelete,
}: {
  scope: MemoryScopeOut;
  active: boolean;
  onSelect: () => void;
  onRename: (name: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(scope.name);
  if (editing) {
    return (
      <div className="mt-1 flex gap-1">
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-label="作用域名称"
          className="control-shell type-body-sm h-10 min-w-0 flex-1 px-2 text-[var(--fg-0)] outline-none focus:border-accent-border focus:shadow-[var(--ring)] max-sm:min-h-11"
        />
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            onRename(name.trim() || scope.name);
            setEditing(false);
          }}
        >
          {copy.action.save}
        </Button>
      </div>
    );
  }
  return (
    <div className="group mt-1 flex items-center gap-1">
      <button
        type="button"
        onClick={onSelect}
        className={scopeButtonClass(active)}
      >
        <span className="truncate">
          {scope.emoji ? `${scope.emoji} ` : ""}
          {scope.is_default ? "默认" : scope.name}
        </span>
        <span>{scope.count}</span>
      </button>
      {!scope.is_default ? (
        <div className="flex shrink-0 items-center gap-1">
          <IconButton
            variant="ghost"
            size="sm"
            onClick={() => setEditing(true)}
            aria-label="编辑作用域"
            tooltip="编辑"
          >
            <Pencil className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton
            variant="danger"
            size="sm"
            onClick={onDelete}
            aria-label="删除作用域"
            tooltip="删除"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      ) : null}
    </div>
  );
}

function scopeButtonClass(active: boolean): string {
  return [
    "flex min-h-11 min-w-max flex-1 items-center justify-between gap-2 rounded-[var(--radius-control)] px-3 type-body-sm transition-colors lg:h-9 lg:min-h-0 lg:min-w-0",
    active
      ? "bg-accent-soft text-accent"
      : "text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
  ].join(" ");
}
