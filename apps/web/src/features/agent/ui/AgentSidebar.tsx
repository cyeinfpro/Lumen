"use client";

import { Bot, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Button,
  ConfirmDialog,
  Input,
  Spinner,
} from "@/components/ui/primitives";
import type { AgentSession } from "../model/contracts";
import { AgentSessionItem } from "./AgentSessionItem";

export function AgentSidebar({
  sessions,
  currentSessionId,
  loading,
  creating,
  hasMore,
  loadingMore,
  query,
  busySessionId,
  onCreate,
  onSelect,
  onRename,
  onArchive,
  onDelete,
  onNavigate,
  onLoadMore,
  onQueryChange,
}: {
  sessions: AgentSession[];
  currentSessionId: string | null;
  loading: boolean;
  creating: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  query: string;
  busySessionId: string | null;
  onCreate: () => void;
  onSelect: (sessionId: string) => void;
  onRename: (sessionId: string, title: string) => void;
  onArchive: (session: AgentSession) => void;
  onDelete: (sessionId: string) => Promise<void> | void;
  onNavigate?: () => void;
  onLoadMore: () => void;
  onQueryChange: (query: string) => void;
}) {
  const [showArchived, setShowArchived] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    return sessions.filter(
      (session) =>
        session.archived === showArchived &&
        (!normalized ||
          (session.title || "新会话").toLocaleLowerCase("zh-CN").includes(normalized)),
    );
  }, [query, sessions, showArchived]);

  return (
    <div className="flex min-h-0 w-full flex-1 flex-col bg-[var(--bg-1)]">
      <div className="flex items-center gap-2 px-4 pb-3 pt-4">
        <Bot className="h-5 w-5 text-accent" aria-hidden />
        <span className="type-card-title">Agent</span>
      </div>
      <div className="px-4 pb-3">
        <Button
          variant="primary"
          fullWidth
          loading={creating}
          onClick={onCreate}
          leftIcon={<Plus className="h-4 w-4" aria-hidden />}
          className="justify-start"
        >
          新建会话
        </Button>
      </div>
      <div className="flex gap-1 px-4 pb-2" role="group" aria-label="会话分类">
        <Button
          variant={showArchived ? "ghost" : "secondary"}
          size="sm"
          onClick={() => setShowArchived(false)}
          className="flex-1"
          aria-pressed={!showArchived}
        >
          会话
        </Button>
        <Button
          variant={showArchived ? "secondary" : "ghost"}
          size="sm"
          onClick={() => setShowArchived(true)}
          className="flex-1"
          aria-pressed={showArchived}
        >
          已归档
        </Button>
      </div>
      <div className="px-4 pb-3">
        <Input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="搜索会话"
          aria-label="搜索 Agent 会话"
          leftIcon={<Search className="h-4 w-4" aria-hidden />}
        />
      </div>
      <div
        role="region"
        aria-label="Agent 会话列表"
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-2 pb-4"
      >
        {loading && sessions.length === 0 ? (
          <div role="status" className="flex items-center gap-2 px-3 py-6 type-caption text-[var(--fg-2)]">
            <Spinner size={16} /> 加载中
          </div>
        ) : filtered.length === 0 ? (
          <p className="px-3 py-8 text-center type-caption text-[var(--fg-2)]">
            {query ? "无结果" : showArchived ? "归档为空" : "暂无会话"}
          </p>
        ) : (
          <ul className="space-y-0.5">
            {filtered.map((session) => (
              <AgentSessionItem
                key={session.id}
                session={session}
                active={session.id === currentSessionId}
                busy={busySessionId === session.id}
                onSelect={() => {
                  onSelect(session.id);
                  onNavigate?.();
                }}
                onRename={(title) => onRename(session.id, title)}
                onArchive={() => onArchive(session)}
                onDelete={() => setDeleteTarget(session)}
              />
            ))}
          </ul>
        )}
        {hasMore ? (
          <div className="px-3 pt-2">
            <Button
              variant="ghost"
              size="sm"
              fullWidth
              loading={loadingMore}
              onClick={onLoadMore}
            >
              加载更多
            </Button>
          </div>
        ) : null}
      </div>
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="删除 Agent 会话"
        description="消息和关联的 Agent 运行记录将一并删除。"
        confirmText="删除"
        tone="danger"
        confirming={Boolean(deleteTarget && busySessionId === deleteTarget.id)}
        onConfirm={async () => {
          if (!deleteTarget) return;
          await onDelete(deleteTarget.id);
          setDeleteTarget(null);
        }}
      />
    </div>
  );
}
