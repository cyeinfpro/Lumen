import Link from "next/link";
import { Brain, ChevronDown, Power, SlidersHorizontal } from "lucide-react";

import type { MemoryScopeOut } from "@/lib/apiClient";
import { Button } from "@/components/ui/primitives";

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

  return (
    <button
      type="button"
      disabled={!canQueryConversation}
      onClick={onToggleOpen}
      className={[
        "inline-flex min-h-11 min-w-11 items-center justify-center gap-1 rounded-full transition-colors disabled:opacity-40",
        compact ? "h-9 w-9" : "h-7 px-2",
        disabled
          ? "text-[var(--fg-3)] hover:bg-white/8"
          : "text-[var(--fg-2)] hover:bg-white/8 hover:text-[var(--fg-0)]",
      ].join(" ")}
      aria-label="本会话记忆"
      title={disabled ? "本会话未使用记忆" : "本会话记忆"}
    >
      <Brain className={compact ? "h-4.5 w-4.5" : "h-4 w-4"} />
      {!compact && (
        <>
          <span className="hidden type-caption lg:inline">{label}</span>
          <ChevronDown className="h-3 w-3" />
        </>
      )}
    </button>
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
              ? "h-8 text-xs text-[var(--fg-2)]"
              : "h-8 text-xs border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
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
      <select
        value={activeScopeId ?? ""}
        disabled={scopePending || scopes.length === 0 || !canQueryConversation}
        onChange={(event) => onScopeChange(event.target.value || null)}
        className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 type-body-sm text-[var(--fg-0)] outline-none focus:border-[var(--color-lumen-amber)]/60"
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
      </select>
    </div>
  );
}

function UsedMemoryList({ used }: { used: UsedMemory[] }) {
  if (used.length === 0) {
    return (
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-white/[0.02] p-3 type-caption">
        最近一轮没有使用记忆。
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {used.slice(0, 6).map((memory) => (
        <div
          key={memory.id}
          className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-white/[0.02] px-2 py-1.5 type-caption"
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
    <div className="absolute right-0 top-full z-50 mt-2 w-[310px] overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/95 shadow-[var(--shadow-3)] backdrop-blur-xl">
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
          className="block rounded-[var(--radius-control)] border border-[var(--border)] px-3 py-2 text-center type-body-sm text-[var(--fg-1)] transition-colors hover:bg-white/[0.04] hover:text-[var(--fg-0)]"
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
