"use client";

import type React from "react";
import {
  CheckCircle2,
  FileText,
  Save,
  Star,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import type {
  ConversationSummary,
  SystemPrompt,
} from "@/lib/apiClient";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";
import { Button, IconButton } from "./primitives";

export function SystemPromptEditorHeader({
  embedded,
  editing,
  onClose,
}: {
  embedded: boolean;
  editing: boolean;
  onClose: () => void;
}) {
  return (
    <div className="hidden items-center justify-between border-b border-[var(--border)] px-5 py-4 md:flex">
      <div className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
        <FileText className="h-4 w-4 text-[var(--accent)]" />
        {editing ? "编辑提示词方案" : "创建提示词方案"}
      </div>
      {embedded ? null : (
        <IconButton
          variant="ghost"
          size="sm"
          onClick={onClose}
          className="rounded-full"
          aria-label={copy.action.close}
        >
          <X className="h-4 w-4" />
        </IconButton>
      )}
    </div>
  );
}

export function SystemPromptEditorFields({
  name,
  content,
  errorMessage,
  nameInputRef,
  fileInputRef,
  onNameChange,
  onContentChange,
  onImport,
}: {
  name: string;
  content: string;
  errorMessage: string | null;
  nameInputRef: React.RefObject<HTMLInputElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onNameChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onImport: (file: File | undefined) => Promise<void>;
}) {
  return (
    <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-4 sm:p-5 scrollbar-thin">
      <label className="block text-xs font-medium text-[var(--fg-1)]">
        名称
      </label>
      <input
        ref={nameInputRef}
        value={name}
        onChange={(event) => onNameChange(event.target.value)}
        maxLength={120}
        className="mt-1.5 h-11 w-full rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 text-base text-[var(--fg-0)] placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20 md:h-10 md:text-sm"
        placeholder="例如：图片导演"
      />

      <div className="mt-4 flex items-center justify-between gap-3">
        <label className="text-xs font-medium text-[var(--fg-1)]">内容</label>
        <div className="text-[11px] tabular-nums text-[var(--fg-2)]">
          {content.length}/10000
        </div>
      </div>
      <textarea
        value={content}
        onChange={(event) => onContentChange(event.target.value)}
        rows={14}
        className="mt-1.5 min-h-[180px] md:min-h-[280px] w-full resize-none rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/72 px-3.5 py-3 text-sm leading-6 text-[var(--fg-0)] placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20"
        placeholder="写入这个会话要遵守的角色、风格、限制和输出格式…"
      />

      {errorMessage ? (
        <p
          role="alert"
          aria-live="assertive"
          className="mt-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger"
        >
          {errorMessage}
        </p>
      ) : null}

      <input
        ref={fileInputRef}
        type="file"
        accept=".md,text/markdown,text/plain"
        hidden
        onChange={(event) => {
          void onImport(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </div>
  );
}

export function SystemPromptDialogFooter({
  selectedPrompt,
  currentConversation,
  busy,
  isDefault,
  isAppliedToCurrent,
  settingDefault,
  fileInputRef,
  onDelete,
  onApply,
  onSetDefault,
  onSave,
}: {
  selectedPrompt: SystemPrompt | null;
  currentConversation: ConversationSummary | null;
  busy: boolean;
  isDefault: boolean;
  isAppliedToCurrent: boolean;
  settingDefault: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onDelete: () => void;
  onApply: () => void;
  onSetDefault: () => void;
  onSave: () => void;
}) {
  const showApply = Boolean(selectedPrompt && currentConversation);
  const defaultDisabled = busy || Boolean(isDefault && selectedPrompt);
  const defaultLabel = systemPromptDefaultActionLabel(
    isDefault,
    Boolean(selectedPrompt),
  );
  return (
    <div className="mobile-dialog-footer flex flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/72 p-3 sm:flex-row sm:items-center sm:justify-between sm:px-5 sm:pb-3">
      <div className="flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => fileInputRef.current?.click()}
          className="rounded-full"
          leftIcon={<Upload className="h-3.5 w-3.5" />}
        >
          {copy.action.import} MD
        </Button>
        {selectedPrompt ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={onDelete}
            disabled={busy}
            className="rounded-full border-danger-border bg-danger-soft text-danger hover:opacity-90"
            leftIcon={<Trash2 className="h-3.5 w-3.5" />}
          >
            {copy.action.delete}
          </Button>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-2 sm:justify-end">
        {showApply ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={onApply}
            disabled={busy || isAppliedToCurrent}
            className="rounded-full"
            leftIcon={<CheckCircle2 className="h-3.5 w-3.5" />}
          >
            {isAppliedToCurrent ? "已应用当前会话" : "应用当前会话"}
          </Button>
        ) : null}
        <Button
          variant="secondary"
          size="sm"
          onClick={onSetDefault}
          disabled={defaultDisabled}
          aria-disabled={defaultDisabled || undefined}
          aria-busy={settingDefault || undefined}
          className={cn(
            "rounded-full border-[var(--accent)]/35 bg-[var(--accent)]/10 text-[var(--accent)] hover:bg-[var(--accent)]/15",
            busy && "pointer-events-none",
          )}
          leftIcon={<Star className="h-3.5 w-3.5" />}
        >
          {defaultLabel}
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={onSave}
          disabled={busy}
          aria-disabled={busy || undefined}
          aria-busy={busy || undefined}
          loading={busy}
          className={cn("rounded-full", busy && "pointer-events-none")}
          leftIcon={!busy ? <Save className="h-3.5 w-3.5" /> : undefined}
        >
          {copy.action.save}
        </Button>
      </div>
    </div>
  );
}

function systemPromptDefaultActionLabel(
  isDefault: boolean,
  hasSelectedPrompt: boolean,
): string {
  if (isDefault) return "全局默认";
  return hasSelectedPrompt ? "设为默认" : "保存并设默认";
}

export function PromptRow({
  prompt,
  active,
  isDefault,
  current,
  onClick,
}: {
  prompt: SystemPrompt;
  active: boolean;
  isDefault: boolean;
  current: boolean;
  onClick: () => void;
}) {
  return (
    /* @list-item-ok: 多行 list-item 含 badge + 描述行，不适合 Button primitive 的 inline 排版 */
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={cn(
        "group min-h-11 w-full rounded-[var(--radius-dialog)] border px-3 py-2 text-left transition-colors",
        active
          ? "border-[var(--accent)]/50 bg-[var(--accent)]/10"
          : "border-[var(--border-subtle)] bg-[var(--bg-2)] hover:border-[var(--border)] hover:bg-[var(--bg-3)]",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate type-body-sm font-medium text-[var(--fg-0)]">
          {prompt.name}
        </span>
        {isDefault && (
          <span className="rounded-full bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]">
            默认
          </span>
        )}
        {current && (
          <span className="rounded-full bg-success-soft px-1.5 py-0.5 text-[10px] text-success">
            当前
          </span>
        )}
      </div>
      <p className="mt-1 line-clamp-2 type-caption leading-relaxed text-[var(--fg-2)]">
        {prompt.content || "空提示词"}
      </p>
    </button>
  );
}
