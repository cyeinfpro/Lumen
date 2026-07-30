"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Loader2,
  Plus,
  Settings2,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import {
  PromptRow,
  SystemPromptDialogFooter,
  SystemPromptEditorFields,
  SystemPromptEditorHeader,
} from "./SystemPromptManagerPresentation";
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
              : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
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
