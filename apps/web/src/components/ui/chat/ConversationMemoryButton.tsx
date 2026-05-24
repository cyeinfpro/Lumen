"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, ChevronDown, Power, SlidersHorizontal } from "lucide-react";

import {
  getConversation,
  getConversationUsedMemories,
  listMemoryScopes,
  patchConversationActiveScope,
  patchConversationMemoryDisabled,
} from "@/lib/apiClient";
import { Button } from "@/components/ui/primitives";
import { useChatStore } from "@/store/useChatStore";

export function ConversationMemoryButton({ compact = false }: { compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const qc = useQueryClient();

  const convQ = useQuery({
    queryKey: ["conversation", currentConvId],
    queryFn: () => getConversation(currentConvId ?? ""),
    enabled: Boolean(currentConvId),
    staleTime: 10_000,
  });
  const scopesQ = useQuery({
    queryKey: ["me", "memory", "scopes"],
    queryFn: listMemoryScopes,
    enabled: open,
    staleTime: 30_000,
  });
  const usedQ = useQuery({
    queryKey: ["conversation", currentConvId, "used-memories"],
    queryFn: () => getConversationUsedMemories(currentConvId ?? ""),
    enabled: open && Boolean(currentConvId),
    staleTime: 10_000,
  });

  const invalidateConversation = () => {
    void qc.invalidateQueries({ queryKey: ["conversation", currentConvId] });
    void qc.invalidateQueries({ queryKey: ["conversation", currentConvId, "used-memories"] });
  };

  const toggleMut = useMutation({
    mutationFn: (disabled: boolean) =>
      patchConversationMemoryDisabled(currentConvId ?? "", disabled),
    onSuccess: invalidateConversation,
  });
  const scopeMut = useMutation({
    mutationFn: (scopeId: string | null) =>
      patchConversationActiveScope(currentConvId ?? "", scopeId),
    onSuccess: invalidateConversation,
  });

  const disabled = Boolean(convQ.data?.memory_disabled);
  const activeScopeId = convQ.data?.active_scope_id ?? null;
  const scopes = scopesQ.data ?? [];
  const activeScope = scopes.find((scope) => scope.id === activeScopeId);
  const used = usedQ.data?.used_memory_summary ?? [];

  return (
    <div className="relative">
      {/* 紧凑顶栏触发按钮：自带 Brain icon + chevron + 可选 label，需要紧凑节奏 */}
      <button
        type="button"
        disabled={!currentConvId}
        onClick={() => setOpen((value) => !value)}
        className={[
          "inline-flex items-center justify-center gap-1 rounded-full transition-colors disabled:opacity-40",
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
            <span className="hidden type-caption lg:inline">
              {disabled ? "记忆关" : activeScope ? activeScope.name : "记忆"}
            </span>
            <ChevronDown className="h-3 w-3" />
          </>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-2 w-[310px] overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/95 shadow-[var(--shadow-3)] backdrop-blur-xl">
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
                disabled={toggleMut.isPending || !currentConvId}
                onClick={() => toggleMut.mutate(!disabled)}
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

          <div className="space-y-3 p-3">
            <div>
              <div className="mb-2 flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
                <SlidersHorizontal className="h-3.5 w-3.5" />
                作用域
              </div>
              <select
                value={activeScopeId ?? ""}
                disabled={scopeMut.isPending || scopes.length === 0 || !currentConvId}
                onChange={(e) => scopeMut.mutate(e.target.value || null)}
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

            <div>
              <div className="mb-2 type-caption text-[var(--fg-2)]">
                最近参考
              </div>
              {used.length === 0 ? (
                <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-white/[0.02] p-3 type-caption">
                  最近一轮没有使用记忆。
                </div>
              ) : (
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
              )}
            </div>

            <Link
              href="/settings/memory"
              onClick={() => setOpen(false)}
              className="block rounded-[var(--radius-control)] border border-[var(--border)] px-3 py-2 text-center type-body-sm text-[var(--fg-1)] transition-colors hover:bg-white/[0.04] hover:text-[var(--fg-0)]"
            >
              管理全部记忆
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
