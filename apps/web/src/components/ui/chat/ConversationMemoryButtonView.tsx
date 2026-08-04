import Link from "next/link";
import { Brain, ChevronDown, Power, SlidersHorizontal } from "lucide-react";

import type { MemoryScopeOut } from "@/lib/apiClient";
import { Button, IconButton, Select } from "@/components/ui/primitives";

type UsedMemory = {
  id: string;
  type: string;
  content: string;
};

export type ConversationMemoryButtonViewProps = {
  compact: boolean;
  open: boolean;
  onToggleOpen: () => void;
  onClose: () => void;
  canQueryConversation: boolean;
  disabled: boolean;
  activeScopeName?: string;
  activeScopeId: string | null;
  scopes: MemoryScopeOut[];
  used: UsedMemory[];
  togglePending: boolean;
  scopePending: boolean;
  onToggleDisabled: () => void;
  onScopeChange: (scopeId: string | null) => void;
};

type MemoryTriggerProps = Pick<
  ConversationMemoryButtonViewProps,
  | "compact"
  | "onToggleOpen"
  | "canQueryConversation"
  | "disabled"
  | "activeScopeName"
>;

function MemoryTrigger({
  compact,
  onToggleOpen,
  canQueryConversation,
  disabled,
  activeScopeName,
}: MemoryTriggerProps) {
  const label = disabled ? "记忆关" : activeScopeName ?? "记忆";

  const title = disabled ? "本会话未使用记忆" : "本会话记忆";
  if (compact) {
    return (
      <IconButton
        size="sm"
        disabled={!canQueryConversation}
        onClick={onToggleOpen}
        aria-label="本会话记忆"
        tooltip={title}
        className={disabled ? "text-[var(--fg-3)]" : "text-[var(--fg-2)]"}
      >
        <Brain className="h-4 w-4" aria-hidden />
      </IconButton>
    );
  }

  return (
    <Button
      size="sm"
      variant="ghost"
      disabled={!canQueryConversation}
      onClick={onToggleOpen}
      aria-label="本会话记忆"
      title={title}
      className={disabled ? "h-8 px-2 text-[var(--fg-3)]" : "h-8 px-2 text-[var(--fg-2)]"}
      leftIcon={<Brain className="h-4 w-4" aria-hidden />}
      rightIcon={<ChevronDown className="h-3 w-3" aria-hidden />}
    >
      <span className="hidden type-caption lg:inline">{label}</span>
    </Button>
  );
}

type MemoryPanelHeaderProps = Pick<
  ConversationMemoryButtonViewProps,
  "disabled" | "togglePending" | "canQueryConversation" | "onToggleDisabled"
>;

function MemoryPanelHeader({
  disabled,
  togglePending,
  canQueryConversation,
  onToggleDisabled,
}: MemoryPanelHeaderProps) {
  return (
    <div className="border-b border-[var(--border-subtle)] p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="type-card-title">本会话记忆</div>
          <div className="mt-0.5 type-caption">
            控制下一轮是否注入账号记忆。
          </div>
        </div>
        <Button
          type="button"
          size="sm"
          variant={disabled ? "outline" : "secondary"}
          disabled={togglePending || !canQueryConversation}
          onClick={onToggleDisabled}
          leftIcon={<Power className="h-3.5 w-3.5" />}
          className={
            disabled
              ? "h-8 text-[var(--fg-2)]"
              : "h-8 border-accent-border bg-accent-soft text-accent"
          }
        >
          {disabled ? "已关闭" : "已开启"}
        </Button>
      </div>
    </div>
  );
}

type MemoryScopeControlProps = Pick<
  ConversationMemoryButtonViewProps,
  | "activeScopeId"
  | "scopes"
  | "scopePending"
  | "canQueryConversation"
  | "onScopeChange"
>;

function MemoryScopeControl({
  activeScopeId,
  scopes,
  scopePending,
  canQueryConversation,
  onScopeChange,
}: MemoryScopeControlProps) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        <SlidersHorizontal className="h-3.5 w-3.5" />
        作用域
      </div>
      <Select
        value={activeScopeId ?? ""}
        disabled={scopePending || scopes.length === 0 || !canQueryConversation}
        onChange={(event) => onScopeChange(event.target.value || null)}
        className="h-9 min-h-9 bg-[var(--bg-2)] type-body-sm text-[var(--fg-0)]"
      >
        <option value="">默认</option>
        {scopes
          .filter((scope) => !scope.is_default)
          .map((scope) => (
            <option key={scope.id} value={scope.id}>
              {scope.emoji ? `${scope.emoji} ` : ""}
              {scope.name}
            </option>
          ))}
      </Select>
    </div>
  );
}

function UsedMemoryList({ used }: { used: UsedMemory[] }) {
  if (used.length === 0) {
    return (
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-3 type-caption">
        最近一轮没有使用记忆。
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {used.slice(0, 6).map((memory) => (
        <div
          key={memory.id}
          className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-2 py-1.5 type-caption"
        >
          <span className="text-[var(--fg-2)]">{memory.type}</span>
          <span className="mx-1 text-[var(--fg-3)]">·</span>
          <span className="text-[var(--fg-1)]">{memory.content}</span>
        </div>
      ))}
    </div>
  );
}

function UsedMemorySection({ used }: { used: UsedMemory[] }) {
  return (
    <div>
      <div className="mb-2 type-caption text-[var(--fg-2)]">最近参考</div>
      <UsedMemoryList used={used} />
    </div>
  );
}

type MemoryPanelProps = Pick<
  ConversationMemoryButtonViewProps,
  | "disabled"
  | "togglePending"
  | "canQueryConversation"
  | "onToggleDisabled"
  | "activeScopeId"
  | "scopes"
  | "scopePending"
  | "onScopeChange"
  | "used"
  | "onClose"
>;

function MemoryPanel({
  disabled,
  togglePending,
  canQueryConversation,
  onToggleDisabled,
  activeScopeId,
  scopes,
  scopePending,
  onScopeChange,
  used,
  onClose,
}: MemoryPanelProps) {
  return (
    <div className="absolute right-0 top-full z-[var(--z-tray)] mt-2 w-[310px] overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/95 shadow-[var(--shadow-2)] backdrop-blur-xl">
      <MemoryPanelHeader
        disabled={disabled}
        togglePending={togglePending}
        canQueryConversation={canQueryConversation}
        onToggleDisabled={onToggleDisabled}
      />

      <div className="space-y-3 p-3">
        <MemoryScopeControl
          activeScopeId={activeScopeId}
          scopes={scopes}
          scopePending={scopePending}
          canQueryConversation={canQueryConversation}
          onScopeChange={onScopeChange}
        />
        <UsedMemorySection used={used} />
        <Link
          href="/settings/memory"
          onClick={onClose}
          className="block rounded-[var(--radius-control)] border border-[var(--border)] px-3 py-2 text-center type-body-sm text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
        >
          管理全部记忆
        </Link>
      </div>
    </div>
  );
}

export function ConversationMemoryButtonView(
  props: ConversationMemoryButtonViewProps,
) {
  const {
    compact,
    open,
    onToggleOpen,
    onClose,
    canQueryConversation,
    disabled,
    activeScopeName,
    activeScopeId,
    scopes,
    used,
    togglePending,
    scopePending,
    onToggleDisabled,
    onScopeChange,
  } = props;

  return (
    <div className="relative">
      <MemoryTrigger
        compact={compact}
        onToggleOpen={onToggleOpen}
        canQueryConversation={canQueryConversation}
        disabled={disabled}
        activeScopeName={activeScopeName}
      />
      {open && (
        <MemoryPanel
          disabled={disabled}
          togglePending={togglePending}
          canQueryConversation={canQueryConversation}
          onToggleDisabled={onToggleDisabled}
          activeScopeId={activeScopeId}
          scopes={scopes}
          scopePending={scopePending}
          onScopeChange={onScopeChange}
          used={used}
          onClose={onClose}
        />
      )}
    </div>
  );
}
