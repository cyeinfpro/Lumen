"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
import type { ConversationSummary, SystemPrompt } from "@/lib/apiClient";
import {
  useCreateSystemPromptMutation,
  useDeleteSystemPromptMutation,
  useListConversationsQuery,
  usePatchConversationMutation,
  usePatchSystemPromptMutation,
  useSetDefaultSystemPromptMutation,
  useSystemPromptsQuery,
} from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";

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
  const dialogOpen = embedded || open;

  const promptsQuery = useSystemPromptsQuery({ enabled: dialogOpen });
  const conversationsQuery = useListConversationsQuery(
    { limit: 100 },
    { enabled: dialogOpen || Boolean(currentConvId) },
  );

  const prompts = useMemo(
    () => promptsQuery.data?.items ?? [],
    [promptsQuery.data?.items],
  );
  const defaultId = promptsQuery.data?.default_id ?? null;
  const currentConversation = useMemo(
    () =>
      (conversationsQuery.data?.items ?? []).find(
        (conv) => conv.id === currentConvId,
      ) ?? null,
    [conversationsQuery.data?.items, currentConvId],
  );
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
    <>
      {!hideTrigger && (
        <Button
          variant="secondary"
          size={compact ? "sm" : "md"}
          onClick={() => setOpen(true)}
          className="rounded-full"
          aria-label="管理系统提示词"
          title="系统提示词"
          leftIcon={<Settings2 className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />}
        >
          <span className={compact ? "hidden sm:inline" : "hidden md:inline"}>
            {activePrompt ? activePrompt.name : "系统提示词"}
          </span>
        </Button>
      )}
      {dialogOpen && typeof document !== "undefined"
        ? createPortal(dialog, document.body)
        : null}
    </>
  );
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

  const selectedPrompt =
    selectedId === "new"
      ? null
      : (prompts.find((prompt) => prompt.id === selectedId) ?? null);

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

  useEffect(() => {
    if (embedded) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [embedded, onClose]);

  const busy =
    createMutation.isPending ||
    patchMutation.isPending ||
    deleteMutation.isPending ||
    setDefaultMutation.isPending ||
    patchConversationMutation.isPending;

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

  const isDefault = selectedPrompt
    ? selectedPrompt.id === defaultId || selectedPrompt.is_default
    : false;
  const isAppliedToCurrent =
    selectedPrompt &&
    currentConversation?.default_system_prompt_id === selectedPrompt.id;

  return (
    <div
      className={
        embedded
          ? "w-full"
          : "fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center mobile-dialog-shell sm:items-center sm:p-6"
      }
    >
      {!embedded && (
        /* @backdrop-button: 全屏 dialog backdrop，需要 button role 让 a11y 拿到 click & focus 但样式不能走 Button primitive */
<button
          type="button"
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          aria-label="关闭系统提示词管理"
          onMouseDown={(e) => {
            // 只在鼠标真的按在 backdrop 自身时响应，避免把 input 内正在选中的 mouseup 误判为 outside-click
            if (e.target !== e.currentTarget) return;
          }}
          onClick={(e) => {
            if (e.target !== e.currentTarget) return;
            onClose();
          }}
        />
      )}
      <section
        role={embedded ? undefined : "dialog"}
        aria-modal={embedded ? undefined : true}
        aria-labelledby="system-prompt-title"
        className={cn(
          "relative grid w-full overflow-hidden",
          embedded
            ? "min-h-[620px] h-[calc(100dvh-14rem)] rounded-[var(--radius-dialog)] max-sm:min-h-0 max-sm:h-[calc(100dvh-10rem)]"
            : "h-[760px] max-h-[calc(100dvh-1.5rem)] max-w-5xl rounded-t-[var(--radius-sheet)] sm:rounded-[var(--radius-sheet)] max-sm:h-auto max-sm:max-h-[var(--mobile-dialog-max-height)] max-sm:border-b-0",
          "grid-rows-[minmax(180px,240px)_minmax(0,1fr)] md:grid-rows-1",
          "border border-[var(--border)] bg-[var(--bg-0)]/95 backdrop-blur-2xl",
          !embedded && "shadow-[var(--shadow-3)]",
          "md:grid-cols-[280px_minmax(0,1fr)]",
        )}
      >
        <div className="flex min-h-0 flex-col border-b border-[var(--border)] bg-[var(--bg-1)]/72 md:border-b-0 md:border-r">
          <div className="flex items-center justify-between px-4 py-4">
            <div>
              <h2
                id="system-prompt-title"
                className="text-sm font-semibold text-[var(--fg-0)]"
              >
                系统提示词
              </h2>
              <p className="mt-0.5 text-xs text-neutral-500">
                管理全局默认和当前会话提示词。
              </p>
            </div>
            {!embedded && (
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
              onClick={() => {
                setSelectedId("new");
                setName("新提示词");
                setContent(EMPTY_PROMPT);
                setLocalError(null);
              }}
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

            {loading ? (
              <div className="flex items-center gap-2 px-3 py-8 type-body-sm text-[var(--fg-2)]">
                <Loader2 className="h-4 w-4 animate-spin" />
                {copy.state.loading}
              </div>
            ) : error ? (
              <p className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger">
                加载失败：{error}
              </p>
            ) : prompts.length === 0 ? (
              <p className="px-3 py-8 text-center text-xs leading-relaxed text-neutral-500">
                还没有提示词。可以直接输入，或导入一份 Markdown。
              </p>
            ) : (
              <div className="space-y-1.5">
                {prompts.map((prompt) => (
                  <PromptRow
                    key={prompt.id}
                    prompt={prompt}
                    active={selectedId === prompt.id}
                    isDefault={prompt.id === defaultId || prompt.is_default}
                    current={
                      currentConversation?.default_system_prompt_id ===
                      prompt.id
                    }
                    onClick={() => {
                      setSelectedId(prompt.id);
                      setName(prompt.name);
                      setContent(prompt.content);
                      setLocalError(null);
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-col">
          <div className="hidden items-center justify-between border-b border-[var(--border)] px-5 py-4 md:flex">
            <div className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
              <FileText className="h-4 w-4 text-[var(--accent)]" />
              {selectedPrompt ? "编辑提示词方案" : "创建提示词方案"}
            </div>
            {!embedded && (
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

          <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-4 sm:p-5 scrollbar-thin">
            <label className="block text-xs font-medium text-neutral-400">
              名称
            </label>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={120}
              className="mt-1.5 h-11 w-full rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 text-base text-[var(--fg-0)] placeholder:text-neutral-600 focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20 md:h-10 md:text-sm"
              placeholder="例如：图片导演"
            />

            <div className="mt-4 flex items-center justify-between gap-3">
              <label className="text-xs font-medium text-neutral-400">
                内容
              </label>
              <div className="text-[11px] tabular-nums text-neutral-500">
                {content.length}/10000
              </div>
            </div>
            <textarea
              value={content}
              onChange={(event) => setContent(event.target.value)}
              rows={14}
              className="mt-1.5 min-h-[180px] md:min-h-[280px] w-full resize-none rounded-2xl border border-[var(--border)] bg-[var(--bg-1)]/72 px-3.5 py-3 text-sm leading-6 text-[var(--fg-0)] placeholder:text-neutral-600 focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20"
              placeholder="写入这个会话要遵守的角色、风格、限制和输出格式…"
            />

            {(localError ||
              createMutation.error ||
              patchMutation.error ||
              deleteMutation.error ||
              setDefaultMutation.error ||
              patchConversationMutation.error) && (
              <p className="mt-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger">
                {localError ||
                  createMutation.error?.message ||
                  patchMutation.error?.message ||
                  deleteMutation.error?.message ||
                  setDefaultMutation.error?.message ||
                  patchConversationMutation.error?.message}
              </p>
            )}

            <input
              ref={fileInputRef}
              type="file"
              accept=".md,text/markdown,text/plain"
              hidden
              onChange={(event) => {
                void importMarkdown(event.target.files?.[0]);
                event.target.value = "";
              }}
            />
          </div>

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
              {selectedPrompt && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => deleteMutation.mutate(selectedPrompt.id)}
                  disabled={busy}
                  className="rounded-full border-danger-border bg-danger-soft text-danger hover:opacity-90"
                  leftIcon={<Trash2 className="h-3.5 w-3.5" />}
                >
                  {copy.action.delete}
                </Button>
              )}
            </div>

            <div className="flex flex-wrap gap-2 sm:justify-end">
              {selectedPrompt && currentConversation && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={applyToCurrentConversation}
                  disabled={busy || Boolean(isAppliedToCurrent)}
                  className="rounded-full"
                  leftIcon={<CheckCircle2 className="h-3.5 w-3.5" />}
                >
                  {isAppliedToCurrent ? "已应用当前会话" : "应用当前会话"}
                </Button>
              )}
              <Button
                variant="secondary"
                size="sm"
                onClick={setSelectedAsDefault}
                disabled={busy || Boolean(isDefault && selectedPrompt)}
                aria-disabled={
                  busy || Boolean(isDefault && selectedPrompt) || undefined
                }
                aria-busy={setDefaultMutation.isPending || undefined}
                className={cn(
                  "rounded-full border-[var(--accent)]/35 bg-[var(--accent)]/10 text-[var(--accent)] hover:bg-[var(--accent)]/15",
                  busy && "pointer-events-none",
                )}
                leftIcon={<Star className="h-3.5 w-3.5" />}
              >
                {isDefault
                  ? "全局默认"
                  : selectedPrompt
                    ? "设为默认"
                    : "保存并设默认"}
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => savePrompt(false)}
                disabled={busy}
                aria-disabled={busy || undefined}
                aria-busy={busy || undefined}
                loading={busy}
                className={cn(
                  "rounded-full",
                  busy && "pointer-events-none",
                )}
                leftIcon={!busy ? <Save className="h-3.5 w-3.5" /> : undefined}
              >
                {copy.action.save}
              </Button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
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
