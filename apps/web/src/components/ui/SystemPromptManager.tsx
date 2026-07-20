"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  FileText,
  Loader2,
  Plus,
  Save,
  Settings2,
  Star,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { copy } from "@/lib/copy";
import { Button, IconButton } from "./primitives";
import {
  getConversation,
  type ConversationSummary,
  type SystemPrompt,
} from "@/lib/apiClient";
import {
  qk,
  useCreateSystemPromptMutation,
  useDeleteSystemPromptMutation,
  usePatchConversationMutation,
  usePatchSystemPromptMutation,
  useSetDefaultSystemPromptMutation,
  useSystemPromptsQuery,
} from "@/lib/queries";
import { useUserQueryScope } from "@/components/QueryProvider";
import { useChatStore } from "@/store/useChatStore";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useModalLayer } from "./primitives/mobile/useModalLayer";

interface SystemPromptManagerProps {
  compact?: boolean;
  mode?: "modal" | "embedded";
  /** 挂载即打开 dialog；用于作为独立页面（/settings/prompts）的主体。 */
  defaultOpen?: boolean;
  /** dialog 关闭时回调，通常用于 router.back/push 导航。 */
  onDialogClose?: () => void;
  /** 为 true 时不渲染触发按钮（配合 defaultOpen 用于独立页面）。 */
  hideTrigger?: boolean;
}

const EMPTY_PROMPT = "";

function isPromptDialogOpen(embedded: boolean, open: boolean): boolean {
  return embedded || open;
}

function useCurrentConversationQuery(currentConvId: string | null) {
  const userScope = useUserQueryScope();
  const conversationId = currentConvId ?? "";
  return useQuery({
    queryKey: qk.user(userScope.userId).conversationDetail(conversationId),
    queryFn: () => getConversation(conversationId),
    enabled: userScope.enabled && Boolean(currentConvId),
    staleTime: 10_000,
  });
}

function SystemPromptTrigger({
  compact,
  activePrompt,
  onOpen,
}: {
  compact: boolean;
  activePrompt: SystemPrompt | null;
  onOpen: () => void;
}) {
  return (
    <Button
      variant="secondary"
      size={compact ? "sm" : "md"}
      onClick={onOpen}
      className="rounded-full"
      aria-label="管理系统提示词"
      title="系统提示词"
      leftIcon={
        <Settings2 className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
      }
    >
      <span className={compact ? "hidden sm:inline" : "hidden md:inline"}>
        {activePrompt ? activePrompt.name : "系统提示词"}
      </span>
    </Button>
  );
}

function firstMessage(
  ...messages: Array<string | null | undefined>
): string | null {
  return messages.find(Boolean) ?? null;
}

function PromptList({
  prompts,
  loading,
  error,
  selectedId,
  defaultId,
  currentPromptId,
  onSelect,
}: {
  prompts: SystemPrompt[];
  loading: boolean;
  error: string | null;
  selectedId: string | "new";
  defaultId: string | null;
  currentPromptId: string | null;
  onSelect: (prompt: SystemPrompt) => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 px-3 py-8 type-body-sm text-[var(--fg-2)]">
        <Loader2 className="h-4 w-4 animate-spin" />
        {copy.state.loading}
      </div>
    );
  }
  if (error) {
    return (
      <p className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger">
        加载失败：{error}
      </p>
    );
  }
  if (prompts.length === 0) {
    return (
      <p className="px-3 py-8 text-center text-xs leading-relaxed text-[var(--fg-2)]">
        还没有提示词。可以直接输入，或导入一份 Markdown。
      </p>
    );
  }
  return (
    <div className="space-y-1.5">
      {prompts.map((prompt) => (
        <PromptRow
          key={prompt.id}
          prompt={prompt}
          active={selectedId === prompt.id}
          isDefault={prompt.id === defaultId || prompt.is_default}
          current={currentPromptId === prompt.id}
          onClick={() => onSelect(prompt)}
        />
      ))}
    </div>
  );
}

export function SystemPromptManager({
  compact = false,
  mode = "modal",
  defaultOpen = false,
  onDialogClose,
  hideTrigger = false,
}: SystemPromptManagerProps) {
  const currentConvId = useChatStore((s) => s.currentConvId);
  const embedded = mode === "embedded";
  const [open, setOpen] = useState(defaultOpen);
  const dialogOpen = isPromptDialogOpen(embedded, open);

  const promptsQuery = useSystemPromptsQuery({ enabled: dialogOpen });
  const currentConversationQuery = useCurrentConversationQuery(currentConvId);

  const prompts = useMemo(
    () => promptsQuery.data?.items ?? [],
    [promptsQuery.data?.items],
  );
  const defaultId = promptsQuery.data?.default_id ?? null;
  const currentConversation = currentConversationQuery.data ?? null;
  const activePrompt = useMemo(
    () => resolveActivePrompt(prompts, currentConversation, defaultId),
    [prompts, currentConversation, defaultId],
  );

  const handleClose = () => {
    if (embedded) return;
    setOpen(false);
    onDialogClose?.();
  };

  const dialog = dialogOpen ? (
    <SystemPromptDialog
      prompts={prompts}
      defaultId={defaultId}
      currentConversation={currentConversation}
      loading={promptsQuery.isLoading}
      error={promptsQuery.error?.message ?? null}
      onClose={handleClose}
      embedded={embedded}
    />
  ) : null;

  if (embedded) return dialog;

  return (
    <SystemPromptModalPresentation
      compact={compact}
      activePrompt={activePrompt}
      hideTrigger={hideTrigger}
      dialogOpen={dialogOpen}
      dialog={dialog}
      onOpen={() => setOpen(true)}
    />
  );
}

function SystemPromptModalPresentation({
  compact,
  activePrompt,
  hideTrigger,
  dialogOpen,
  dialog,
  onOpen,
}: {
  compact: boolean;
  activePrompt: SystemPrompt | null;
  hideTrigger: boolean;
  dialogOpen: boolean;
  dialog: React.ReactNode;
  onOpen: () => void;
}) {
  return (
    <>
      {hideTrigger ? null : (
        <SystemPromptTrigger
          compact={compact}
          activePrompt={activePrompt}
          onOpen={onOpen}
        />
      )}
      <SystemPromptDialogPortal open={dialogOpen}>
        {dialog}
      </SystemPromptDialogPortal>
    </>
  );
}

function SystemPromptDialogPortal({
  open,
  children,
}: {
  open: boolean;
  children: React.ReactNode;
}) {
  if (!open || typeof document === "undefined") return null;
  return createPortal(children, document.body);
}

function resolveActivePrompt(
  prompts: SystemPrompt[],
  conversation: ConversationSummary | null,
  defaultId: string | null,
) {
  const convPromptId = conversation?.default_system_prompt_id ?? null;
  return (
    prompts.find((prompt) => prompt.id === convPromptId) ??
    prompts.find((prompt) => prompt.id === defaultId) ??
    prompts.find((prompt) => prompt.is_default) ??
    null
  );
}

function selectedPromptForEditor(
  prompts: SystemPrompt[],
  selectedId: string | "new",
): SystemPrompt | null {
  if (selectedId === "new") return null;
  return prompts.find((prompt) => prompt.id === selectedId) ?? null;
}

function promptMutationsPending(...pending: boolean[]): boolean {
  return pending.some(Boolean);
}

function selectedPromptIsDefault(
  prompt: SystemPrompt | null,
  defaultId: string | null,
): boolean {
  if (!prompt) return false;
  return prompt.id === defaultId || prompt.is_default;
}

function selectedPromptIsApplied(
  prompt: SystemPrompt | null,
  conversation: ConversationSummary | null,
): boolean {
  if (!prompt) return false;
  return conversation?.default_system_prompt_id === prompt.id;
}

function SystemPromptDialog({
  prompts,
  defaultId,
  currentConversation,
  loading,
  error,
  onClose,
  embedded = false,
}: {
  prompts: SystemPrompt[];
  defaultId: string | null;
  currentConversation: ConversationSummary | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  embedded?: boolean;
}) {
  const [selectedId, setSelectedId] = useState<string | "new">("new");
  const [name, setName] = useState("新提示词");
  const [content, setContent] = useState(EMPTY_PROMPT);
  const [localError, setLocalError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  useBodyScrollLock(!embedded);
  const closeDialog = useCallback(() => {
    if (!embedded) onClose();
  }, [embedded, onClose]);
  const onDialogKeyDown = useModalLayer({
    open: !embedded,
    rootRef: dialogRef,
    onClose: closeDialog,
    initialFocusRef: nameInputRef,
  });

  const selectedPrompt = selectedPromptForEditor(prompts, selectedId);

  const createMutation = useCreateSystemPromptMutation({
    onSuccess: (prompt) => {
      setSelectedId(prompt.id);
      setName(prompt.name);
      setContent(prompt.content);
      setLocalError(null);
    },
  });
  const patchMutation = usePatchSystemPromptMutation({
    onSuccess: (prompt) => {
      setName(prompt.name);
      setContent(prompt.content);
      setLocalError(null);
    },
  });
  const deleteMutation = useDeleteSystemPromptMutation({
    onSuccess: () => {
      setSelectedId("new");
      setName("新提示词");
      setContent(EMPTY_PROMPT);
      setLocalError(null);
    },
  });
  const setDefaultMutation = useSetDefaultSystemPromptMutation();
  const patchConversationMutation = usePatchConversationMutation();

  const busy = promptMutationsPending(
    createMutation.isPending,
    patchMutation.isPending,
    deleteMutation.isPending,
    setDefaultMutation.isPending,
    patchConversationMutation.isPending,
  );

  const validate = () => {
    if (!name.trim()) return "名称必填";
    if (!content.trim()) return "内容必填";
    if (content.length > 10000) return "超过 10000 字";
    return null;
  };

  const savePrompt = (makeDefault = false) => {
    const validationError = validate();
    if (validationError) {
      setLocalError(validationError);
      return;
    }
    if (selectedPrompt) {
      patchMutation.mutate({
        id: selectedPrompt.id,
        name: name.trim(),
        content,
        make_default: makeDefault || undefined,
      });
    } else {
      createMutation.mutate({
        name: name.trim(),
        content,
        make_default: makeDefault,
      });
    }
  };

  const importMarkdown = async (file: File | undefined) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".md")) {
      setLocalError("只支持导入 .md 文件");
      return;
    }
    try {
      const text = await file.text();
      if (text.length > 10000) {
        setLocalError("MD 内容超过 10000 字");
        return;
      }
      setContent(text);
      if (selectedId === "new") {
        setName(file.name.replace(/\.md$/i, "") || "新提示词");
      }
      setLocalError(null);
    } catch (err) {
      setLocalError(
        err instanceof Error ? `读取文件失败：${err.message}` : "读取文件失败",
      );
    }
  };

  const applyToCurrentConversation = () => {
    if (!selectedPrompt || !currentConversation) return;
    patchConversationMutation.mutate({
      id: currentConversation.id,
      default_system_prompt_id: selectedPrompt.id,
    });
  };

  const setSelectedAsDefault = () => {
    setLocalError(null);
    if (selectedPrompt) {
      if (selectedPrompt.id !== defaultId) {
        setDefaultMutation.mutate(selectedPrompt.id);
      }
      return;
    }
    savePrompt(true);
  };
  const selectPrompt = (prompt: SystemPrompt) => {
    setSelectedId(prompt.id);
    setName(prompt.name);
    setContent(prompt.content);
    setLocalError(null);
  };

  const isDefault = selectedPromptIsDefault(selectedPrompt, defaultId);
  const isAppliedToCurrent = selectedPromptIsApplied(
    selectedPrompt,
    currentConversation,
  );
  const errorMessage = firstMessage(
    localError,
    createMutation.error?.message,
    patchMutation.error?.message,
    deleteMutation.error?.message,
    setDefaultMutation.error?.message,
    patchConversationMutation.error?.message,
  );

  return (
    <SystemPromptDialogLayout
      embedded={embedded}
      dialogRef={dialogRef}
      onDialogKeyDown={onDialogKeyDown}
      onClose={onClose}
      sidebar={
        <SystemPromptSidebar
          embedded={embedded}
          prompts={prompts}
          loading={loading}
          error={error}
          selectedId={selectedId}
          defaultId={defaultId}
          currentPromptId={
            currentConversation?.default_system_prompt_id ?? null
          }
          onClose={onClose}
          onCreateNew={() => {
            setSelectedId("new");
            setName("新提示词");
            setContent(EMPTY_PROMPT);
            setLocalError(null);
          }}
          onSelect={selectPrompt}
        />
      }
      editor={
        <SystemPromptEditorPanel
          embedded={embedded}
          selectedPrompt={selectedPrompt}
          currentConversation={currentConversation}
          name={name}
          content={content}
          errorMessage={errorMessage}
          busy={busy}
          isDefault={isDefault}
          isAppliedToCurrent={Boolean(isAppliedToCurrent)}
          settingDefault={setDefaultMutation.isPending}
          nameInputRef={nameInputRef}
          fileInputRef={fileInputRef}
          onClose={onClose}
          onNameChange={setName}
          onContentChange={setContent}
          onImport={importMarkdown}
          onDelete={() => {
            if (selectedPrompt) deleteMutation.mutate(selectedPrompt.id);
          }}
          onApply={applyToCurrentConversation}
          onSetDefault={setSelectedAsDefault}
          onSave={() => savePrompt(false)}
        />
      }
    />
  );
}

function SystemPromptDialogLayout({
  embedded,
  dialogRef,
  onDialogKeyDown,
  onClose,
  sidebar,
  editor,
}: {
  embedded: boolean;
  dialogRef: React.RefObject<HTMLElement | null>;
  onDialogKeyDown: React.KeyboardEventHandler<HTMLElement>;
  onClose: () => void;
  sidebar: React.ReactNode;
  editor: React.ReactNode;
}) {
  return (
    <div
      className={
        embedded
          ? "w-full"
          : "fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center mobile-dialog-shell sm:items-center sm:p-6"
      }
    >
      {embedded ? null : <SystemPromptBackdrop onClose={onClose} />}
      <section
        ref={dialogRef}
        role={embedded ? undefined : "dialog"}
        aria-modal={embedded ? undefined : true}
        aria-labelledby="system-prompt-title"
        tabIndex={embedded ? undefined : -1}
        onKeyDown={embedded ? undefined : onDialogKeyDown}
        className={cn(
          "mobile-dialog-panel relative grid w-full overflow-hidden",
          embedded
            ? "min-h-[620px] h-[calc(100dvh-14rem)] rounded-[var(--radius-dialog)] max-sm:min-h-0 max-sm:h-[calc(100dvh-10rem)]"
            : "h-[var(--mobile-dialog-max-height)] max-w-5xl rounded-t-[var(--radius-sheet)] border-b-0 sm:h-[760px] sm:max-h-[calc(100dvh-1.5rem)] sm:rounded-[var(--radius-sheet)] sm:border-b",
          "grid-rows-[minmax(112px,180px)_minmax(0,1fr)] md:grid-rows-1",
          "border border-[var(--border)] bg-[var(--bg-0)]/95 backdrop-blur-2xl",
          embedded ? null : "shadow-[var(--shadow-3)]",
          "md:grid-cols-[280px_minmax(0,1fr)]",
        )}
      >
        {sidebar}
        {editor}
      </section>
    </div>
  );
}

function SystemPromptBackdrop({ onClose }: { onClose: () => void }) {
  return (
    /* @backdrop-button: 全屏 dialog backdrop，需要 button role 让 a11y 拿到 click & focus 但样式不能走 Button primitive */
    <button
      type="button"
      className="absolute inset-0 bg-black/60 backdrop-blur-sm"
      aria-label="关闭系统提示词管理"
      onMouseDown={(event) => {
        // 只在鼠标真的按在 backdrop 自身时响应，避免把 input 内正在选中的 mouseup 误判为 outside-click
        if (event.target !== event.currentTarget) return;
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        onClose();
      }}
    />
  );
}

function SystemPromptSidebar({
  embedded,
  prompts,
  loading,
  error,
  selectedId,
  defaultId,
  currentPromptId,
  onClose,
  onCreateNew,
  onSelect,
}: {
  embedded: boolean;
  prompts: SystemPrompt[];
  loading: boolean;
  error: string | null;
  selectedId: string | "new";
  defaultId: string | null;
  currentPromptId: string | null;
  onClose: () => void;
  onCreateNew: () => void;
  onSelect: (prompt: SystemPrompt) => void;
}) {
  return (
    <div className="flex min-h-0 flex-col border-b border-[var(--border)] bg-[var(--bg-1)]/72 md:border-b-0 md:border-r">
      <div className="flex items-center justify-between px-4 py-4">
        <div>
          <h2
            id="system-prompt-title"
            className="text-sm font-semibold text-[var(--fg-0)]"
          >
            系统提示词
          </h2>
          <p className="mt-0.5 text-xs text-[var(--fg-2)]">
            管理全局默认和当前会话提示词。
          </p>
        </div>
        {embedded ? null : (
          <IconButton
            variant="ghost"
            size="lg"
            onClick={onClose}
            className="rounded-full md:hidden"
            aria-label={copy.action.close}
          >
            <X className="h-4 w-4" />
          </IconButton>
        )}
      </div>

      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-3 pb-3 scrollbar-thin">
        {/* @list-item-ok: PromptRow 风格的菜单项，特化的 active/inactive 边框 + 行高，不走 Button primitive */}
        <button
          type="button"
          onClick={onCreateNew}
          className={cn(
            "mb-2 flex min-h-11 w-full items-center gap-2 rounded-[var(--radius-dialog)] border px-3 py-2 text-left type-body-sm transition-colors",
            selectedId === "new"
              ? "border-[var(--accent)]/45 bg-[var(--accent)]/10 text-[var(--fg-0)]"
              : "border-[var(--border)] bg-white/[0.03] text-[var(--fg-1)] hover:bg-white/[0.06] hover:text-[var(--fg-0)]",
          )}
        >
          <Plus className="h-4 w-4" />
          新建提示词
        </button>

        <PromptList
          prompts={prompts}
          loading={loading}
          error={error}
          selectedId={selectedId}
          defaultId={defaultId}
          currentPromptId={currentPromptId}
          onSelect={onSelect}
        />
      </div>
    </div>
  );
}

function SystemPromptEditorPanel({
  embedded,
  selectedPrompt,
  currentConversation,
  name,
  content,
  errorMessage,
  busy,
  isDefault,
  isAppliedToCurrent,
  settingDefault,
  nameInputRef,
  fileInputRef,
  onClose,
  onNameChange,
  onContentChange,
  onImport,
  onDelete,
  onApply,
  onSetDefault,
  onSave,
}: {
  embedded: boolean;
  selectedPrompt: SystemPrompt | null;
  currentConversation: ConversationSummary | null;
  name: string;
  content: string;
  errorMessage: string | null;
  busy: boolean;
  isDefault: boolean;
  isAppliedToCurrent: boolean;
  settingDefault: boolean;
  nameInputRef: React.RefObject<HTMLInputElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onClose: () => void;
  onNameChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onImport: (file: File | undefined) => Promise<void>;
  onDelete: () => void;
  onApply: () => void;
  onSetDefault: () => void;
  onSave: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-col">
      <SystemPromptEditorHeader
        embedded={embedded}
        editing={Boolean(selectedPrompt)}
        onClose={onClose}
      />
      <SystemPromptEditorFields
        name={name}
        content={content}
        errorMessage={errorMessage}
        nameInputRef={nameInputRef}
        fileInputRef={fileInputRef}
        onNameChange={onNameChange}
        onContentChange={onContentChange}
        onImport={onImport}
      />
      <SystemPromptDialogFooter
        selectedPrompt={selectedPrompt}
        currentConversation={currentConversation}
        busy={busy}
        isDefault={isDefault}
        isAppliedToCurrent={isAppliedToCurrent}
        settingDefault={settingDefault}
        fileInputRef={fileInputRef}
        onDelete={onDelete}
        onApply={onApply}
        onSetDefault={onSetDefault}
        onSave={onSave}
      />
    </div>
  );
}

function SystemPromptEditorHeader({
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

function SystemPromptEditorFields({
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

function SystemPromptDialogFooter({
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

function PromptRow({
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
          : "border-[var(--border-subtle)] bg-white/[0.025] hover:border-[var(--border)] hover:bg-white/[0.05]",
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
