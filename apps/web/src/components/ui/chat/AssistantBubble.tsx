"use client";

// 助手消息气泡：靠左玻璃卡；包含文本 + 可选的生成卡 + 底部工具条（重试/复制/意图切换）。
// IntentBadge 放在文本气泡内部（右上）或生成卡右上；工具条在气泡外底部，hover/focus 显现。

import { motion, AnimatePresence } from "framer-motion";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookmarkPlus,
  Brain,
  Check,
  ChevronDown,
  Copy,
  RotateCw,
  Sparkles,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Markdown } from "../Markdown";
import { toast } from "@/components/ui/primitives";
import { IntentBadge } from "./IntentBadge";
import type { AssistantMessage, Generation, Intent } from "@/lib/types";
import {
  acceptMemoryStaging,
  confirmMemory,
  createMemory,
  getMemorySettings,
  listMemoryScopes,
  markMemoryOnboardingSeen,
  patchMemory,
  patchMemoryScopeAssignment,
  patchMemoryStaging,
  rejectMemoryStaging,
  undoMemory,
  type MemoryType,
} from "@/lib/apiClient";
import { useChatStore } from "@/store/useChatStore";

export interface AssistantBubbleProps {
  msg: AssistantMessage;
  generations: Generation[];
  onEditImage: (imageId: string) => void;
  onRetry: (gen: Generation) => void;
  onRetryText: () => void;
  onRegenerate: (newIntent: Exclude<Intent, "auto">) => Promise<void>;
}

const GenerationView = dynamic(() => import("./GenerationView"), {
  ssr: false,
  loading: () => <GenerationViewFallback />,
});

export function AssistantBubble({
  msg,
  generations,
  onEditImage,
  onRetry,
  onRetryText,
  onRegenerate,
}: AssistantBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [confirmationDone, setConfirmationDone] = useState(false);
  const [selectionFab, setSelectionFab] = useState<{
    text: string;
    x: number;
    y: number;
  } | null>(null);
  const [savePrefill, setSavePrefill] = useState<string | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const isStreamingText = msg.status === "streaming";
  const isThinking = isStreamingText && !!msg.thinking && !msg.text;
  const isChatLike =
    msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
  // pending / streaming / canceled 期间不允许切换 intent
  const canSwitchIntent =
    msg.status === "succeeded" || msg.status === "failed";
  const isFailedText = msg.status === "failed" && isChatLike;
  const canCopy = Boolean(msg.text && msg.status !== "pending");
  const hasGenerations = generations.length > 0;

  const handleCopy = async () => {
    if (!msg.text) return;
    if (await copyText(msg.text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } else {
      toast.error("复制失败", { description: "浏览器拒绝了剪贴板写入" });
    }
  };

  // 选中助手回答里的一段文字 → 浮 FAB "记下这条". 用户单击 FAB 弹 modal,
  // 默认归类为 preference, 可改 type / scope / 编辑 content 后保存为 manual memory.
  const handleSelectionChange = () => {
    const sel = typeof window !== "undefined" ? window.getSelection() : null;
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setSelectionFab(null);
      return;
    }
    const text = sel.toString().trim();
    if (text.length < 2 || text.length > 200) {
      setSelectionFab(null);
      return;
    }
    const range = sel.getRangeAt(0);
    const bubble = bubbleRef.current;
    if (!bubble || !bubble.contains(range.commonAncestorContainer)) {
      setSelectionFab(null);
      return;
    }
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      setSelectionFab(null);
      return;
    }
    setSelectionFab({
      text,
      x: rect.left + rect.width / 2,
      y: rect.bottom,
    });
  };
  useEffect(() => {
    if (!selectionFab) return;
    const onScroll = () => setSelectionFab(null);
    window.addEventListener("scroll", onScroll, { passive: true, capture: true });
    return () => window.removeEventListener("scroll", onScroll, true);
  }, [selectionFab]);

  return (
    <motion.div
      layout="position"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ type: "spring", damping: 28, stiffness: 320 }}
      className="group flex justify-start"
    >
      <div className="max-w-[96%] md:max-w-[96%] w-full min-w-0 flex flex-col gap-2">
        {/* Thinking 折叠区：默认收起；点击后展开 */}
        {msg.thinking && (
          <div className="rounded-xl border border-white/[0.06] bg-white/[0.03] overflow-hidden">
            <button
              type="button"
              onClick={() => setThinkingOpen((v) => !v)}
              className={cn(
                "flex w-full items-center gap-2 px-4 py-2 text-xs text-neutral-400",
                "hover:text-neutral-300 transition-colors",
              )}
            >
              <span className={cn(
                "inline-block w-1.5 h-1.5 rounded-full",
                isThinking
                  ? "bg-[var(--color-lumen-amber)] animate-pulse"
                  : "bg-neutral-500",
              )} />
              <span>{isThinking ? "思考中…" : "思考过程"}</span>
              <ChevronDown
                className={cn(
                  "w-3 h-3 ml-auto transition-transform duration-200",
                  thinkingOpen && "rotate-180",
                )}
              />
            </button>
            <AnimatePresence initial={false}>
              {thinkingOpen && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="px-4 pb-3 text-xs leading-relaxed text-neutral-500 max-h-60 overflow-y-auto">
                    <Markdown>{msg.thinking}</Markdown>
                    {isThinking && (
                      <span
                        aria-hidden
                        className="inline-block w-[0.4ch] ml-0.5 animate-pulse text-neutral-500"
                      >
                        ▍
                      </span>
                    )}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* 文本气泡 */}
        {(msg.text || (isChatLike && !hasGenerations)) && (
          <div
            ref={bubbleRef}
            onMouseUp={handleSelectionChange}
            onTouchEnd={handleSelectionChange}
            className={cn(
              "relative px-4 py-3 md:px-5 md:py-3.5 rounded-2xl rounded-bl-md text-[0.9rem] md:text-[0.95rem] leading-relaxed",
              "bg-[var(--bg-1)]/70 border border-[var(--border)] text-[var(--fg-0)]",
              "backdrop-blur-sm shadow-sm min-w-0 break-words [overflow-wrap:anywhere]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "border-red-400/30 bg-red-500/5",
            )}
          >
            {msg.text ? (
              <Markdown>{msg.text}</Markdown>
            ) : (
              <span className="text-neutral-400">
                {isStreamingText ? "" : "…"}
              </span>
            )}
            {isStreamingText && (
              <span
                aria-hidden
                className="inline-block w-[0.5ch] ml-0.5 animate-pulse text-[var(--color-lumen-amber)]"
              >
                ▍
              </span>
            )}
            {msg.memory_writes && msg.memory_writes.length > 0 && (
              <div className="mt-3 flex flex-col gap-1.5 border-t border-white/8 pt-2">
                <MemoryWriteHints
                  conversationId={currentConvId}
                  writes={msg.memory_writes}
                />
              </div>
            )}
            {msg.used_memory_summary && msg.used_memory_summary.length > 0 && (
              <div className="mt-3 border-t border-white/8 pt-2">
                <button
                  type="button"
                  onClick={() => setMemoryOpen((v) => !v)}
                  className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.04] px-2 py-1 text-[11px] text-[var(--fg-1)] hover:bg-white/[0.07]"
                >
                  <Brain className="h-3 w-3" />
                  用了 {msg.used_memory_summary.length} 条记忆
                  <ChevronDown
                    className={cn("h-3 w-3 transition-transform", memoryOpen && "rotate-180")}
                  />
                </button>
                {memoryOpen && (
                  <div className="mt-2 space-y-1.5 rounded-md border border-white/10 bg-black/20 p-2">
                    {msg.used_memory_summary.map((memory) => (
                      <div
                        key={memory.id}
                        className="flex items-start justify-between gap-2 text-[11px] text-[var(--fg-1)]"
                      >
                        <span className="min-w-0 break-words">
                          {memory.content}
                          <span className="ml-1 text-[var(--fg-2)]">({memory.type})</span>
                        </span>
                        <button
                          type="button"
                          onClick={() => void disableMemory(memory.id)}
                          className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-[var(--fg-2)] hover:bg-white/10 hover:text-[var(--fg-0)]"
                        >
                          停用
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {msg.confirmation_candidate_id && !confirmationDone && (
                  <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--fg-2)]">
                    <Sparkles className="h-3 w-3 text-[var(--color-lumen-amber)]" />
                    <span title="基于一条高置信偏好">这条偏好还适用吗?</span>
                    <ConfirmButton
                      label="是"
                      onClick={() =>
                        confirmMemoryDecision(
                          msg.confirmation_candidate_id ?? "",
                          "yes",
                          setConfirmationDone,
                          currentConvId,
                        )
                      }
                    />
                    <ConfirmButton
                      label="不是"
                      onClick={() =>
                        confirmMemoryDecision(
                          msg.confirmation_candidate_id ?? "",
                          "no",
                          setConfirmationDone,
                          currentConvId,
                        )
                      }
                    />
                    <ConfirmButton
                      label="这次不用"
                      onClick={() =>
                        confirmMemoryDecision(
                          msg.confirmation_candidate_id ?? "",
                          "skip",
                          setConfirmationDone,
                          currentConvId,
                        )
                      }
                    />
                  </div>
                )}
                <MemoryOnboardingTip
                  flag={2}
                  text="我刚才参考了你之前告诉我的记忆。"
                />
              </div>
            )}
            <IntentBadge
              currentIntent={msg.intent_resolved}
              disabled={!canSwitchIntent}
              onSwitch={onRegenerate}
              className="absolute -top-2.5 right-2"
            />
            {canCopy && (
              <button
                type="button"
                onClick={() => void handleCopy()}
                aria-label={copied ? "已复制" : "复制"}
                title={copied ? "已复制" : "复制"}
                className={cn(
                  "absolute right-2 bottom-2 p-1 rounded-md",
                  "text-[var(--fg-2)] hover:text-[var(--fg-0)] hover:bg-white/10",
                  "transition-all duration-150 active:scale-[0.92]",
                )}
              >
                {copied ? (
                  <Check className="w-3.5 h-3.5 text-[var(--ok,#30A46C)]" />
                ) : (
                  <Copy className="w-3.5 h-3.5" />
                )}
              </button>
            )}
          </div>
        )}

        {/* 生成卡 */}
        {hasGenerations && (
          <div
            className={cn(
              generations.length === 1
                ? "flex flex-col gap-3"
                : "grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3",
            )}
          >
            {generations.map((gen, index) => (
              <GenerationView
                key={gen.id}
                gen={gen}
                currentIntent={msg.intent_resolved}
                canSwitchIntent={canSwitchIntent}
                onEditImage={onEditImage}
                onRetry={onRetry}
                onRegenerate={onRegenerate}
                compact={generations.length > 1}
                ordinal={generations.length > 1 ? index + 1 : undefined}
              />
            ))}
          </div>
        )}

        {/* 底部重试按钮 */}
        {msg.status === "failed" && isChatLike && (
          <div
            className={cn(
              "flex items-center gap-1 pl-2 -mt-1",
              "opacity-100 sm:opacity-0 sm:group-hover:opacity-100 focus-within:opacity-100",
              "transition-opacity duration-200",
            )}
          >
            <ToolbarButton onClick={onRetryText} label="重试">
              <RotateCw className="w-3.5 h-3.5" />
            </ToolbarButton>
          </div>
        )}
      </div>
      {selectionFab && savePrefill === null && (
        <button
          type="button"
          onMouseDown={(e) => {
            e.preventDefault(); // 别让 click 清掉 selection
          }}
          onClick={() => {
            setSavePrefill(selectionFab.text);
            setSelectionFab(null);
            window.getSelection()?.removeAllRanges();
          }}
          style={{
            position: "fixed",
            left: selectionFab.x,
            top: selectionFab.y + 8,
            transform: "translate(-50%, 0)",
            zIndex: 50,
          }}
          className="inline-flex items-center gap-1.5 rounded-xl border border-white/15 bg-[var(--bg-1)] px-2.5 py-1.5 text-xs text-[var(--fg-0)] shadow-lg backdrop-blur-sm hover:bg-white/[0.08]"
        >
          <BookmarkPlus className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
          记下这条
        </button>
      )}
      {savePrefill !== null && (
        <SaveSelectionModal
          defaultContent={savePrefill}
          onClose={() => setSavePrefill(null)}
        />
      )}
    </motion.div>
  );
}

const MEMORY_TYPE_OPTIONS: Array<{ value: MemoryType; label: string }> = [
  { value: "preference", label: "偏好" },
  { value: "profile", label: "身份" },
  { value: "avoid", label: "禁忌" },
  { value: "project", label: "项目" },
];

function SaveSelectionModal({
  defaultContent,
  onClose,
}: {
  defaultContent: string;
  onClose: () => void;
}) {
  const [type, setType] = useState<MemoryType>("preference");
  const [content, setContent] = useState(defaultContent.slice(0, 200));
  const [scopeId, setScopeId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const scopesQ = useQuery({
    queryKey: ["me", "memory", "scopes"],
    queryFn: listMemoryScopes,
    staleTime: 60_000,
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const handleSave = async () => {
    const trimmed = content.trim();
    if (!trimmed) {
      toast.error("内容不能为空");
      return;
    }
    setSaving(true);
    try {
      await createMemory({
        type,
        content: trimmed.slice(0, 200),
        scope_id: scopeId,
      });
      toast.success("已加入记忆");
      onClose();
    } catch {
      toast.error("保存失败");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 px-4 sm:items-center"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-sm font-medium text-[var(--fg-0)]">
            <BookmarkPlus className="h-4 w-4 text-[var(--color-lumen-amber)]" />
            记下这段
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-[var(--fg-2)] hover:bg-white/5"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="mb-3 flex flex-wrap gap-1.5">
          {MEMORY_TYPE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setType(option.value)}
              className={cn(
                "rounded-lg border px-2.5 py-1 text-xs transition-colors",
                type === option.value
                  ? "border-[var(--color-lumen-amber)]/40 bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)]"
                  : "border-white/10 text-[var(--fg-1)] hover:bg-white/5",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value.slice(0, 200))}
          rows={3}
          className="mb-2 w-full rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--color-lumen-amber)]/60"
          placeholder="例:偏好简洁回答"
        />
        <div className="mb-4 flex items-center justify-between gap-2 text-[11px] text-[var(--fg-2)]">
          <span>{content.length}/200</span>
          {scopesQ.data && scopesQ.data.length > 0 && (
            <select
              value={scopeId ?? ""}
              onChange={(e) => setScopeId(e.target.value || null)}
              className="h-7 rounded-md border border-white/10 bg-white/[0.03] px-1.5 text-[11px] text-[var(--fg-1)] outline-none"
            >
              <option value="">默认作用域</option>
              {scopesQ.data
                .filter((scope) => !scope.is_default)
                .map((scope) => (
                  <option key={scope.id} value={scope.id}>
                    {scope.emoji ? `${scope.emoji} ` : ""}
                    {scope.name}
                  </option>
                ))}
            </select>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="h-9 rounded-xl border border-white/10 px-4 text-sm text-[var(--fg-1)] hover:bg-white/5 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={saving || !content.trim()}
            className="inline-flex h-9 items-center justify-center rounded-xl bg-[var(--color-lumen-amber)] px-4 text-sm font-medium text-black disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

async function disableMemory(id: string) {
  try {
    await patchMemory(id, { disabled: true });
    toast.success("已停用这条记忆");
  } catch {
    toast.error("停用失败");
  }
}

async function confirmMemoryDecision(
  id: string,
  decision: "yes" | "no" | "skip",
  setDone: (done: boolean) => void,
  conversationId?: string | null,
) {
  if (!id) return;
  try {
    await confirmMemory(id, decision, conversationId);
    setDone(true);
    toast.success("已更新记忆反馈");
  } catch {
    toast.error("反馈失败");
  }
}

function ConfirmButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded px-1.5 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
    >
      {label}
    </button>
  );
}

function MemoryOnboardingTip({ flag, text }: { flag: number; text: string }) {
  const qc = useQueryClient();
  const settingsQ = useQuery({
    queryKey: ["me", "memory", "settings"],
    queryFn: getMemorySettings,
    staleTime: 60_000,
  });
  const mut = useMutation({
    mutationFn: markMemoryOnboardingSeen,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["me", "memory"] });
    },
  });
  const seen = ((settingsQ.data?.onboarding_seen ?? 0) & (1 << flag)) !== 0;
  if (seen || settingsQ.isPending) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 rounded-md border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/8 px-2 py-1 text-[11px] text-[var(--fg-1)]">
      <Brain className="h-3 w-3 text-[var(--color-lumen-amber)]" />
      <span>{text}</span>
      <button
        type="button"
        onClick={() => mut.mutate(flag)}
        className="rounded px-1 py-0.5 text-[var(--color-lumen-amber)] hover:bg-white/10"
      >
        知道了
      </button>
    </div>
  );
}

function MemoryWriteHints({
  writes,
  conversationId,
}: {
  writes: NonNullable<AssistantMessage["memory_writes"]>;
  conversationId?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const compactable = writes.filter(
    (write) => write.kind !== "staged" && write.kind !== "rejected_pii" && write.type,
  );
  if (compactable.length === writes.length && compactable.length >= 3 && !expanded) {
    const type = compactable[0]?.type ?? "preference";
    return (
      <>
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--fg-2)]">
          <Brain className="h-3 w-3 text-[var(--color-lumen-amber)]" />
          <span>已记下 {compactable.length} 条{memoryTypeLabel(type)}</span>
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
          >
            查看
          </button>
        </div>
        <MemoryOnboardingTip
          flag={1}
          text="我从这句话学到了长期偏好，5 分钟内可以撤销。"
        />
      </>
    );
  }
  return (
    <>
      {writes.map((write, idx) => (
        <MemoryWriteHint
          key={`${write.kind}-${write.id ?? idx}`}
          conversationId={conversationId}
          write={write}
        />
      ))}
      <MemoryOnboardingTip
        flag={1}
        text="我从这句话学到了长期偏好，5 分钟内可以撤销。"
      />
    </>
  );
}

function memoryTypeLabel(type: string | null | undefined): string {
  if (type === "profile") return "身份";
  if (type === "avoid") return "禁忌";
  if (type === "project") return "项目";
  return "偏好";
}

function MemoryWriteHint({
  write,
}: {
  write: NonNullable<AssistantMessage["memory_writes"]>[number];
  conversationId?: string | null;
}) {
  const [doneLabel, setDoneLabel] = useState<string | null>(null);
  const [scopeId, setScopeId] = useState(write.scope_id ?? "");
  const [detailOpen, setDetailOpen] = useState(false);
  const [editedContent, setEditedContent] = useState(write.content ?? "");
  const scopesQ = useQuery({
    queryKey: ["me", "memory", "scopes"],
    queryFn: listMemoryScopes,
    enabled: Boolean(write.id) && write.kind !== "rejected_pii",
    staleTime: 60_000,
  });
  const label =
    write.kind === "staged"
      ? `想让我记住「${write.content}」吗?`
      : write.kind === "rejected_pii"
        ? "检测到敏感信息,未记住"
        : write.kind === "merged"
          ? `已合并到现有偏好:${write.content}`
          : write.kind === "superseded"
            ? `已更新偏好:${write.content}`
            : `已记下:${write.content}`;
  const handleUndo = async () => {
    if (!write.undo_token) return;
    try {
      await undoMemory(write.undo_token);
      setDoneLabel("已撤销");
      toast.success("已撤销");
    } catch {
      toast.error("撤销失败");
    }
  };
  const handleAccept = async () => {
    if (!write.id) return;
    try {
      // 用户编辑过 content (点过"详细"改了文字), 先 patch staging 再 accept;
      // 否则跳过 patch 直接 accept.
      const trimmed = editedContent.trim();
      if (
        detailOpen &&
        trimmed &&
        trimmed !== (write.content ?? "").trim() &&
        trimmed.length <= 200
      ) {
        await patchMemoryStaging(write.id, { content: trimmed });
      }
      await acceptMemoryStaging(write.id);
      setDoneLabel("已加入记忆");
      toast.success("已加入记忆");
    } catch {
      toast.error("接受失败");
    }
  };
  const handleReject = async () => {
    if (!write.id) return;
    try {
      await rejectMemoryStaging(write.id);
      setDoneLabel("已忽略");
      toast.success("已忽略");
    } catch {
      toast.error("拒绝失败");
    }
  };
  const handleScopeChange = async (nextScopeId: string) => {
    if (!write.id) return;
    setScopeId(nextScopeId);
    try {
      if (write.kind === "staged") {
        await patchMemoryStaging(write.id, { scope_id: nextScopeId });
      } else {
        await patchMemoryScopeAssignment(write.id, nextScopeId);
      }
      toast.success("已更新作用域");
    } catch {
      setScopeId(write.scope_id ?? "");
      toast.error("作用域更新失败");
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--fg-2)]">
      <Brain className="h-3 w-3 text-[var(--color-lumen-amber)]" />
      <span>{doneLabel ?? label}</span>
      {write.id && write.kind !== "rejected_pii" && scopesQ.data && !doneLabel && (
        <select
          value={scopeId}
          onChange={(e) => void handleScopeChange(e.target.value)}
          className="h-6 rounded-md border border-white/10 bg-white/[0.03] px-1 text-[11px] text-[var(--fg-1)] outline-none"
          title={
            write.recommended_scope_id && write.recommended_scope_id === scopeId
              ? "推荐作用域"
              : "作用域"
          }
        >
          {scopesQ.data.map((scope) => (
            <option key={scope.id} value={scope.id}>
              {scope.is_default ? "默认" : scope.name}
            </option>
          ))}
        </select>
      )}
      {write.kind === "staged" && write.id && !doneLabel && (
        <>
          <button
            type="button"
            onClick={() => void handleAccept()}
            className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
          >
            是
          </button>
          <button
            type="button"
            onClick={() => void handleReject()}
            className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
          >
            否
          </button>
          <button
            type="button"
            onClick={() => setDetailOpen((v) => !v)}
            className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
          >
            {detailOpen ? "收起" : "详细"}
          </button>
        </>
      )}
      {write.undo_token && !doneLabel && (
        <button
          type="button"
          onClick={() => void handleUndo()}
          className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
        >
          撤销
        </button>
      )}
      <a
        href="/settings/memory"
        className="rounded px-1 py-0.5 text-[var(--fg-1)] hover:bg-white/10"
      >
        管理
      </a>
      {detailOpen && write.kind === "staged" && !doneLabel && (
        <div className="mt-1 w-full rounded-lg border border-white/10 bg-white/[0.03] p-2 text-[11px] text-[var(--fg-2)]">
          {write.source_excerpt && (
            <div className="mb-2 leading-5 text-[var(--fg-2)]/80">
              来源:{write.source_excerpt}
            </div>
          )}
          <input
            value={editedContent}
            onChange={(e) => setEditedContent(e.target.value.slice(0, 200))}
            className="h-7 w-full rounded-md border border-white/10 bg-white/[0.04] px-2 text-[11px] text-[var(--fg-0)] outline-none focus:border-[var(--color-lumen-amber)]/60"
            placeholder="编辑后再保存"
          />
          <div className="mt-1 text-right text-[10px] text-[var(--fg-2)]/70">
            改完点上面的"是"以编辑后的内容入库
          </div>
        </div>
      )}
    </div>
  );
}

export default AssistantBubble;

function GenerationViewFallback() {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="aspect-[4/3] w-full rounded-2xl border border-white/10 bg-white/[0.03]" />
      <div className="h-4 w-2/3 rounded bg-white/[0.04]" />
    </div>
  );
}

// BUG-036: 移除已弃用的 execCommand("copy") 回退。navigator.clipboard API 在所有现代浏览器中可用。
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// 通用小工具按钮：36px 触控目标在子元素 padding 内得到
function ToolbarButton({
  onClick,
  label,
  children,
}: {
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex items-center justify-center w-9 h-9 rounded-md",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-white/10",
        "active:scale-[0.92] transition-all duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
      )}
    >
      {children}
    </button>
  );
}
