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
        <button
          type="button"
          onClick={() => setOpen(true)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5",
            "text-neutral-300 hover:border-[var(--accent)]/45 hover:text-white hover:bg-white/8",
            "cursor-pointer active:scale-[0.97] transition-all duration-150",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
            compact ? "h-8 px-2.5 text-xs" : "h-9 px-3 text-sm",
          )}
          aria-label="管理系统提示词"
          title="系统提示词"
        >
          <Settings2 className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
          <span className={compact ? "hidden sm:inline" : "hidden md:inline"}>
            {activePrompt ? activePrompt.name : "系统提示词"}
          </span>
        </button>
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
    selectedId === "new" ? null : prompts.find((prompt) => prompt.id === selectedId) ?? null;

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
    if (!name.trim()) return "请填写提示词名称";
    if (!content.trim()) return "请填写系统提示词内容";
    if (content.length > 10000) return "系统提示词不能超过 10000 字";
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
        setLocalError("MD 内容超过 10000 字，请精简后再导入");
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
    selectedPrompt && currentConversation?.default_system_prompt_id === selectedPrompt.id;

  return (
    <div
      className={
        embedded
          ? "w-full"
          : "fixed inset-0 z-[80] flex items-center justify-center p-3 sm:p-6"
      }
    >
      {!embedded && (
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
            ? "min-h-[620px] h-[calc(100dvh-14rem)] rounded-2xl"
            : "h-[760px] max-h-[calc(100dvh-1.5rem)] max-w-5xl rounded-3xl",
          "grid-rows-[minmax(180px,240px)_minmax(0,1fr)] md:grid-rows-1",
          "border border-white/10 bg-neutral-950/95 backdrop-blur-2xl",
          !embedded && "shadow-[0_32px_120px_-40px_rgba(0,0,0,0.9)]",
          "md:grid-cols-[280px_minmax(0,1fr)]",
        )}
      >
        <div className="flex min-h-0 flex-col border-b border-white/10 bg-white/[0.025] md:border-b-0 md:border-r">
          <div className="flex items-center justify-between px-4 py-4">
            <div>
              <h2 id="system-prompt-title" className="text-sm font-semibold text-white">
                系统提示词
              </h2>
              <p className="mt-0.5 text-xs text-neutral-500">
                管理全局默认和当前会话提示词。
              </p>
            </div>
            {!embedded && (
              /* @hit-area-ok: system prompt manager mobile close button, tightly packed in modal header */
              <button
                type="button"
                onClick={onClose}
                className="inline-flex h-8 w-8 items-center justify-center rounded-full text-neutral-400 hover:bg-white/8 hover:text-white md:hidden"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3 scrollbar-thin">
            <button
              type="button"
              onClick={() => {
                setSelectedId("new");
                setName("新提示词");
                setContent(EMPTY_PROMPT);
                setLocalError(null);
              }}
              className={cn(
                "mb-2 flex w-full items-center gap-2 rounded-xl border px-3 py-2 text-left text-sm transition-colors",
                selectedId === "new"
                  ? "border-[var(--accent)]/45 bg-[var(--accent)]/10 text-white"
                  : "border-white/10 bg-white/[0.03] text-neutral-300 hover:bg-white/[0.06] hover:text-white",
              )}
            >
              <Plus className="h-4 w-4" />
              新建提示词
            </button>

            {loading ? (
              <div className="flex items-center gap-2 px-3 py-8 text-sm text-neutral-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                加载中
              </div>
            ) : error ? (
              <p className="rounded-xl border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
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
                    current={currentConversation?.default_system_prompt_id === prompt.id}
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
          <div className="hidden items-center justify-between border-b border-white/10 px-5 py-4 md:flex">
            <div className="flex items-center gap-2 text-sm text-neutral-300">
              <FileText className="h-4 w-4 text-[var(--accent)]" />
              {selectedPrompt ? "编辑提示词方案" : "创建提示词方案"}
            </div>
            {!embedded && (
              /* @hit-area-ok: desktop-only close button (md:flex), desktop-only context */
              <button
                type="button"
                onClick={onClose}
                className="inline-flex h-8 w-8 items-center justify-center rounded-full text-neutral-400 hover:bg-white/8 hover:text-white"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5 scrollbar-thin">
            <label className="block text-xs font-medium text-neutral-400">名称</label>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={120}
              className="mt-1.5 h-10 w-full rounded-xl border border-white/10 bg-white/[0.04] px-3 text-sm text-white placeholder:text-neutral-600 focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20"
              placeholder="例如：图片导演"
            />

            <div className="mt-4 flex items-center justify-between gap-3">
              <label className="text-xs font-medium text-neutral-400">内容</label>
              <div className="text-[11px] tabular-nums text-neutral-500">
                {content.length}/10000
              </div>
            </div>
            <textarea
              value={content}
              onChange={(event) => setContent(event.target.value)}
              rows={14}
              className="mt-1.5 min-h-[180px] md:min-h-[280px] w-full resize-none rounded-2xl border border-white/10 bg-black/35 px-3.5 py-3 text-sm leading-6 text-neutral-100 placeholder:text-neutral-600 focus:border-[var(--accent)]/60 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/20"
              placeholder="写入这个会话要遵守的角色、风格、限制和输出格式…"
            />

            {(localError ||
              createMutation.error ||
              patchMutation.error ||
              deleteMutation.error ||
              setDefaultMutation.error ||
              patchConversationMutation.error) && (
              <p className="mt-3 rounded-xl border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200">
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

          <div className="flex flex-col gap-2 border-t border-white/10 bg-black/20 p-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="inline-flex h-9 items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 text-xs text-neutral-200 hover:bg-white/8"
              >
                <Upload className="h-3.5 w-3.5" />
                导入 MD
              </button>
              {selectedPrompt && (
                <button
                  type="button"
                  onClick={() => deleteMutation.mutate(selectedPrompt.id)}
                  disabled={busy}
                  className="inline-flex h-9 items-center gap-1.5 rounded-full border border-red-500/20 bg-red-500/10 px-3 text-xs text-red-200 hover:bg-red-500/15 disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </button>
              )}
            </div>

            <div className="flex flex-wrap gap-2 sm:justify-end">
              {selectedPrompt && currentConversation && (
                // @hit-area-ok: desktop system prompt modal action button, desktop modal context
                <button
                  type="button"
                  onClick={applyToCurrentConversation}
                  disabled={busy || Boolean(isAppliedToCurrent)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 text-xs text-neutral-200 hover:bg-white/8 disabled:opacity-50"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {isAppliedToCurrent ? "已应用当前会话" : "应用当前会话"}
                </button>
              )}
              <button
                type="button"
                onClick={setSelectedAsDefault}
                disabled={busy || Boolean(isDefault && selectedPrompt)}
                aria-disabled={busy || Boolean(isDefault && selectedPrompt) || undefined}
                aria-busy={setDefaultMutation.isPending || undefined}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-full border border-[var(--accent)]/35 bg-[var(--accent)]/10 px-3 text-xs text-[var(--accent)] hover:bg-[var(--accent)]/15 disabled:opacity-50",
                  busy && "pointer-events-none",
                )}
              >
                <Star className="h-3.5 w-3.5" />
                {isDefault ? "全局默认" : selectedPrompt ? "设为默认" : "保存并设默认"}
              </button>
              <button
                type="button"
                onClick={() => savePrompt(false)}
                disabled={busy}
                aria-disabled={busy || undefined}
                aria-busy={busy || undefined}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-full bg-[var(--accent)] px-4 text-xs font-medium text-black hover:brightness-110 disabled:opacity-50",
                  busy && "pointer-events-none",
                )}
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                保存
              </button>
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
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={cn(
        "group w-full rounded-xl border px-3 py-2 text-left transition-colors",
        active
          ? "border-[var(--accent)]/50 bg-[var(--accent)]/10"
          : "border-white/8 bg-white/[0.025] hover:border-white/15 hover:bg-white/[0.05]",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-neutral-100">
          {prompt.name}
        </span>
        {isDefault && (
          <span className="rounded-full bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]">
            默认
          </span>
        )}
        {current && (
          <span className="rounded-full bg-emerald-500/12 px-1.5 py-0.5 text-[10px] text-emerald-300">
            当前
          </span>
        )}
      </div>
      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">
        {prompt.content || "空提示词"}
      </p>
    </button>
  );
}
