"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Brain,
  Check,
  Download,
  Loader2,
  Pause,
  Pin,
  Plus,
  RefreshCw,
  Search,
  ShieldOff,
  Trash2,
  X,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import {
  acceptMemoryStaging,
  clearMemories,
  createMemory,
  createMemoryScope,
  deleteMemory,
  deleteMemoryScope,
  exportMemories,
  getMemorySettings,
  listMemories,
  listMemoryScopes,
  listMemoryStaging,
  listMemoryTimeline,
  markMemoryOnboardingSeen,
  patchMemory,
  patchMemoryScope,
  patchMemoryStaging,
  patchMemorySettings,
  rejectMemoryStaging,
  type MemoryItemOut,
  type MemoryScopeOut,
  type MemoryStagingOut,
  type MemoryType,
} from "@/lib/apiClient";

const TYPE_OPTIONS: Array<{ value: MemoryType; label: string }> = [
  { value: "profile", label: "身份" },
  { value: "preference", label: "偏好" },
  { value: "avoid", label: "禁忌" },
  { value: "project", label: "项目" },
];

function typeLabel(type: MemoryType | string): string {
  return TYPE_OPTIONS.find((option) => option.value === type)?.label ?? type;
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export default function MemorySettingsPage() {
  const qc = useQueryClient();
  const [selectedScope, setSelectedScope] = useState<string>("all");
  const [newScopeName, setNewScopeName] = useState("");
  const [newScopeEmoji, setNewScopeEmoji] = useState("");
  const [newMemoryType, setNewMemoryType] = useState<MemoryType>("preference");
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [stagingEdits, setStagingEdits] = useState<Record<string, string>>({});
  const [clearText, setClearText] = useState("");
  const [memorySearch, setMemorySearch] = useState("");
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<Set<string>>(() => new Set());
  const [showCapabilityModal, setShowCapabilityModal] = useState(false);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["me", "memory"] });
  };

  const settingsQ = useQuery({
    queryKey: ["me", "memory", "settings"],
    queryFn: getMemorySettings,
  });
  const scopesQ = useQuery({
    queryKey: ["me", "memory", "scopes"],
    queryFn: listMemoryScopes,
  });
  const memoriesQ = useQuery({
    queryKey: ["me", "memory", "items", selectedScope],
    queryFn: () =>
      listMemories(selectedScope === "all" ? {} : { scope_id: selectedScope }),
  });
  const stagingQ = useQuery({
    queryKey: ["me", "memory", "staging"],
    queryFn: listMemoryStaging,
  });
  const timelineQ = useQuery({
    queryKey: ["me", "memory", "timeline"],
    queryFn: () => listMemoryTimeline(),
  });

  const scopes = scopesQ.data ?? [];
  const defaultScope = scopes.find((scope) => scope.is_default) ?? scopes[0];
  // settings 还没加载时默认按 "可用" 处理, 避免首次渲染闪烁出 banner;
  // 加载完后以服务端真实值为准.
  const embeddingAvailable = settingsQ.data?.embedding_available ?? true;
  // 用户尝试启用记忆 (disabled=false), 但服务端没 embedding provider:
  // 不发 mutate, 弹窗提示去 admin 配置.
  const requestEnableMemory = (next: boolean) => {
    if (!embeddingAvailable && next === true) {
      // 用户想 "启用" (off → on, 即 disabled: true → false), 但不可用.
      setShowCapabilityModal(true);
      return;
    }
    settingsMut.mutate({ disabled: !next });
  };
  const memories = memoriesQ.data?.items ?? [];
  const filteredMemories = useMemo(() => {
    const query = memorySearch.trim().toLowerCase();
    if (!query) return memories;
    return memories.filter((memory) => {
      const scope = scopes.find((item) => item.id === memory.scope_id);
      return [
        memory.content,
        memory.source_excerpt ?? "",
        memory.type,
        scope?.name ?? "",
        scope?.emoji ?? "",
      ]
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
  }, [memories, memorySearch, scopes]);
  const staging = stagingQ.data?.items ?? [];
  const emptyFirstRun =
    !settingsQ.isPending &&
    !memoriesQ.isPending &&
    memories.length === 0 &&
    ((settingsQ.data?.onboarding_seen ?? 0) & 1) === 0;

  const typeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const memory of memories) counts[memory.type] = (counts[memory.type] ?? 0) + 1;
    return counts;
  }, [memories]);

  const settingsMut = useMutation({
    mutationFn: patchMemorySettings,
    onSuccess: invalidate,
  });
  const onboardingMut = useMutation({
    mutationFn: markMemoryOnboardingSeen,
    onSuccess: invalidate,
  });
  const createMemoryMut = useMutation({
    mutationFn: createMemory,
    onSuccess: () => {
      setNewMemoryContent("");
      invalidate();
    },
  });
  const patchMemoryMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof patchMemory>[1] }) =>
      patchMemory(id, body),
    onSuccess: invalidate,
  });
  const deleteMemoryMut = useMutation({
    mutationFn: deleteMemory,
    onSuccess: invalidate,
  });
  const createScopeMut = useMutation({
    mutationFn: createMemoryScope,
    onSuccess: () => {
      setNewScopeName("");
      setNewScopeEmoji("");
      invalidate();
    },
  });
  const patchScopeMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Parameters<typeof patchMemoryScope>[1] }) =>
      patchMemoryScope(id, body),
    onSuccess: invalidate,
  });
  const deleteScopeMut = useMutation({
    mutationFn: deleteMemoryScope,
    onSuccess: () => {
      setSelectedScope("all");
      invalidate();
    },
  });
  const acceptMut = useMutation({
    mutationFn: async (item: MemoryStagingOut) => {
      const content = stagingEdits[item.id]?.trim();
      if (content && content !== item.content) {
        await patchMemoryStaging(item.id, { content });
      }
      return acceptMemoryStaging(item.id);
    },
    onSuccess: invalidate,
  });
  const rejectMut = useMutation({
    mutationFn: rejectMemoryStaging,
    onSuccess: invalidate,
  });
  const patchStagingMut = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof patchMemoryStaging>[1];
    }) => patchMemoryStaging(id, body),
    onSuccess: invalidate,
  });
  const clearMut = useMutation({
    mutationFn: clearMemories,
    onSuccess: () => {
      setClearText("");
      invalidate();
    },
  });
  const bulkScopeMut = useMutation({
    mutationFn: async ({ ids, scopeId }: { ids: string[]; scopeId: string }) => {
      await Promise.all(ids.map((id) => patchMemory(id, { scope_id: scopeId })));
    },
    onSuccess: () => {
      setSelectedMemoryIds(new Set());
      invalidate();
    },
  });

  const exportJson = async () => {
    const data = await exportMemories();
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `lumen-memory-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <SettingsShell title="记忆" subtitle="MEMORY" maxWidth="max-w-6xl">
      <div className="space-y-6">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">记忆</h1>
            <p className="type-body mt-1.5">
              管理账号级长期记忆、候选建议和最近变化。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex items-center gap-1.5 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="h-4 w-4" />
            返回我的
          </Link>
        </header>

        {!embeddingAvailable && (
          <section className="flex flex-col gap-3 rounded-[var(--radius-card)] border border-warning-border bg-warning-soft p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
            <div className="flex gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-warning" />
              <div>
                <div className="font-medium text-warning">记忆未启用</div>
                <p className="mt-1 type-caption leading-5 text-warning/80">
                  需先在管理员后台为某个 provider 勾选 “embedding”；写入、检索、抽取均依赖向量。
                </p>
              </div>
            </div>
            <Link
              href="/admin"
              className="inline-flex h-9 flex-shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 type-caption font-medium text-warning transition-colors hover:bg-warning/20"
            >
              去管理员后台 →
            </Link>
          </section>
        )}

        <section className="grid gap-3 md:grid-cols-3">
          <SettingToggle
            icon={<Brain className="h-4 w-4" />}
            title="启用记忆"
            description="开启后 Lumen 会从对话中学习稳定偏好,并在新会话里复用。"
            checked={!Boolean(settingsQ.data?.disabled)}
            disabled={settingsMut.isPending}
            onChange={(checked) => requestEnableMemory(checked)}
          />
          <SettingToggle
            icon={<Pause className="h-4 w-4" />}
            title="暂停学习"
            description="不写入新记忆,已有记忆仍会参与回答。"
            checked={Boolean(settingsQ.data?.paused)}
            disabled={settingsMut.isPending || !embeddingAvailable || Boolean(settingsQ.data?.disabled)}
            onChange={(checked) => settingsMut.mutate({ paused: checked })}
          />
          <SettingToggle
            icon={<ShieldOff className="h-4 w-4" />}
            title="主动确认偏好"
            description="强偏好命中时,偶尔让模型先确认。"
            checked={Boolean(settingsQ.data?.confirmation_enabled)}
            disabled={settingsMut.isPending || !embeddingAvailable || Boolean(settingsQ.data?.disabled)}
            onChange={(checked) =>
              settingsMut.mutate({ confirmation_enabled: checked })
            }
          />
        </section>

        {emptyFirstRun && (
          <section className="rounded-[var(--radius-card)] border border-accent-border bg-accent-soft p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="type-body-sm font-medium text-[var(--fg-0)]">
                  Lumen 会从对话里学到稳定偏好
                </div>
                <p className="mt-1 type-body-sm text-[var(--fg-1)]">
                  也可以在这里手动添加，比如“偏好简洁回答”或“不要使用感叹号”。
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => settingsMut.mutate({ paused: true })}
                >
                  先暂停
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => onboardingMut.mutate(0)}
                >
                  {copy.action.confirm}
                </Button>
              </div>
            </div>
          </section>
        )}

        <section className="grid gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <aside className="space-y-3">
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-3">
              <button
                type="button"
                onClick={() => setSelectedScope("all")}
                className={scopeButtonClass(selectedScope === "all")}
              >
                <span>全部</span>
                <span>{scopes.reduce((sum, scope) => sum + scope.count, 0)}</span>
              </button>
              {scopes.map((scope) => (
                <ScopeButton
                  key={scope.id}
                  scope={scope}
                  active={selectedScope === scope.id}
                  onSelect={() => setSelectedScope(scope.id)}
                  onRename={(name) => patchScopeMut.mutate({ id: scope.id, body: { name } })}
                  onDelete={() => deleteScopeMut.mutate(scope.id)}
                />
              ))}
            </div>

            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-3">
              <div className="mb-2 type-caption font-medium text-[var(--fg-1)]">新作用域</div>
              <div className="flex gap-2">
                <input
                  value={newScopeEmoji}
                  onChange={(e) => setNewScopeEmoji(e.target.value.slice(0, 4))}
                  placeholder="图标"
                  className="h-9 w-14 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-2 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60"
                />
                <input
                  value={newScopeName}
                  onChange={(e) => setNewScopeName(e.target.value)}
                  placeholder="工作"
                  className="h-9 min-w-0 flex-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60"
                />
                <IconButton
                  variant="primary"
                  disabled={!newScopeName.trim() || createScopeMut.isPending}
                  onClick={() =>
                    createScopeMut.mutate({
                      name: newScopeName.trim(),
                      emoji: newScopeEmoji.trim() || null,
                    })
                  }
                  aria-label="创建作用域"
                >
                  <Plus className="h-4 w-4" />
                </IconButton>
              </div>
            </div>
          </aside>

          <div className="space-y-5">
            <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="type-card-title">手动添加</h2>
                  <p className="mt-1 type-caption text-[var(--fg-2)]">
                    手动记忆按 1.0 置信度写入。
                  </p>
                </div>
                <div className="flex flex-wrap gap-1.5 text-[11px] text-[var(--fg-2)]">
                  {TYPE_OPTIONS.map((option) => (
                    <span key={option.value}>
                      {option.label} {typeCounts[option.value] ?? 0}
                    </span>
                  ))}
                </div>
              </div>
              <div className="grid gap-2 sm:grid-cols-[150px_minmax(0,1fr)_auto]">
                <select
                  value={newMemoryType}
                  onChange={(e) => setNewMemoryType(e.target.value as MemoryType)}
                  className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60"
                >
                  {TYPE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <input
                  value={newMemoryContent}
                  onChange={(e) => setNewMemoryContent(e.target.value)}
                  placeholder="例如：偏好 200 字以内的回答"
                  maxLength={200}
                  className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60"
                />
                <Button
                  variant="primary"
                  size="md"
                  disabled={!newMemoryContent.trim() || createMemoryMut.isPending}
                  loading={createMemoryMut.isPending}
                  onClick={() =>
                    createMemoryMut.mutate({
                      type: newMemoryType,
                      content: newMemoryContent.trim(),
                      scope_id: selectedScope === "all" ? defaultScope?.id ?? null : selectedScope,
                    })
                  }
                  leftIcon={!createMemoryMut.isPending ? <Plus className="h-4 w-4" /> : undefined}
                >
                  添加
                </Button>
              </div>
            </section>

            <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
              <SectionHeader
                title="记忆库"
                suffix={`${filteredMemories.length}/${memories.length} 条`}
                actions={
                  <>
                    <IconButton
                      variant="outline"
                      size="sm"
                      onClick={() => void memoriesQ.refetch()}
                      aria-label="刷新记忆"
                      tooltip="刷新"
                    >
                      <RefreshCw className="h-4 w-4" />
                    </IconButton>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void exportJson()}
                      leftIcon={<Download className="h-3.5 w-3.5" />}
                    >
                      {copy.action.export}
                    </Button>
                  </>
                }
              />
              <div className="flex flex-col gap-2 border-t border-white/5 p-3 sm:flex-row sm:items-center sm:justify-between">
                <label className="relative min-w-0 flex-1">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
                  <input
                    value={memorySearch}
                    onChange={(event) => setMemorySearch(event.target.value)}
                    placeholder={selectedScope === "all" ? "跨作用域搜索" : "搜索当前作用域"}
                    className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] pl-9 pr-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60"
                  />
                </label>
                {selectedScope === "all" && selectedMemoryIds.size > 0 ? (
                  <div className="flex flex-wrap items-center gap-2 type-caption text-[var(--fg-1)]">
                    <span>已选 {selectedMemoryIds.size} 条</span>
                    <select
                      disabled={bulkScopeMut.isPending}
                      onChange={(event) => {
                        const scopeId = event.target.value;
                        if (!scopeId) return;
                        bulkScopeMut.mutate({
                          ids: Array.from(selectedMemoryIds),
                          scopeId,
                        });
                        event.currentTarget.value = "";
                      }}
                      className="h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-2 text-xs text-[var(--fg-0)] outline-none"
                      defaultValue=""
                    >
                      <option value="" disabled>
                        批量改作用域
                      </option>
                      {scopes.map((scope) => (
                        <option key={scope.id} value={scope.id}>
                          {scope.is_default ? "默认" : scope.name}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}
              </div>
              {memoriesQ.isPending ? (
                <LoadingBlock />
              ) : filteredMemories.length === 0 ? (
                <EmptyBlock text="当前作用域还没有记忆。" />
              ) : (
                <div className="divide-y divide-white/5">
                  {filteredMemories.map((memory) => (
                    <MemoryRow
                      key={memory.id}
                      memory={memory}
                      scopes={scopes}
                      selectable={selectedScope === "all"}
                      selected={selectedMemoryIds.has(memory.id)}
                      onToggleSelected={(checked) =>
                        setSelectedMemoryIds((prev) => {
                          const next = new Set(prev);
                          if (checked) next.add(memory.id);
                          else next.delete(memory.id);
                          return next;
                        })
                      }
                      editingValue={editing[memory.id]}
                      onEditValue={(value) =>
                        setEditing((prev) => ({ ...prev, [memory.id]: value }))
                      }
                      onSaveEdit={() => {
                        const content = editing[memory.id]?.trim();
                        if (content && content !== memory.content) {
                          patchMemoryMut.mutate({ id: memory.id, body: { content } });
                        }
                        setEditing((prev) => {
                          const next = { ...prev };
                          delete next[memory.id];
                          return next;
                        });
                      }}
                      onCancelEdit={() =>
                        setEditing((prev) => {
                          const next = { ...prev };
                          delete next[memory.id];
                          return next;
                        })
                      }
                      onPatch={(body) => patchMemoryMut.mutate({ id: memory.id, body })}
                      onDelete={() => deleteMemoryMut.mutate(memory.id)}
                    />
                  ))}
                </div>
              )}
            </section>

            <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
              <SectionHeader title="建议加入记忆" suffix={`${staging.length} 条`} />
              {stagingQ.isPending ? (
                <LoadingBlock />
              ) : staging.length === 0 ? (
                <EmptyBlock text="暂无待确认候选。" />
              ) : (
                <div className="divide-y divide-white/5">
                  {staging.map((item) => (
                    <div key={item.id} className="p-4">
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <TypeBadge type={item.type} />
                        <span className="type-caption text-[var(--fg-2)]">
                          置信度 {Math.round(item.confidence * 100)}%
                        </span>
                        <span className="type-caption text-[var(--fg-2)]">
                          {formatTime(item.created_at)}
                        </span>
                      </div>
                      <input
                        value={stagingEdits[item.id] ?? item.content}
                        onChange={(e) =>
                          setStagingEdits((prev) => ({
                            ...prev,
                            [item.id]: e.target.value,
                          }))
                        }
                        className="mb-3 h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60"
                      />
                      <div className="flex flex-wrap gap-2">
                        <select
                          value={item.scope_id}
                          onChange={(event) =>
                            patchStagingMut.mutate({
                              id: item.id,
                              body: { scope_id: event.target.value },
                            })
                          }
                          className="h-8 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-2 text-xs text-[var(--fg-1)] outline-none"
                        >
                          {scopes.map((scope) => (
                            <option key={scope.id} value={scope.id}>
                              {scope.is_default ? "默认" : scope.name}
                              {item.recommended_scope_id === scope.id ? " · 推荐" : ""}
                            </option>
                          ))}
                        </select>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => acceptMut.mutate(item)}
                          leftIcon={<Check className="h-3.5 w-3.5" />}
                          className="bg-success-soft text-success hover:bg-success/20"
                        >
                          接受
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => rejectMut.mutate(item.id)}
                          leftIcon={<X className="h-3.5 w-3.5" />}
                        >
                          拒绝
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
              <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
                <SectionHeader title="最近变化" />
                {timelineQ.isPending ? (
                  <LoadingBlock />
                ) : (timelineQ.data?.items.length ?? 0) === 0 ? (
                  <EmptyBlock text="还没有审计事件。" />
                ) : (
                  <div className="divide-y divide-white/5">
                    {timelineQ.data?.items.map((event) => (
                      <div key={event.id} className="grid gap-1 p-4 sm:grid-cols-[100px_minmax(0,1fr)]">
                        <span className="type-caption text-[var(--fg-2)]">
                          {formatTime(event.created_at)}
                        </span>
                        <div className="min-w-0">
                          <div className="type-caption font-mono text-[var(--fg-1)]">
                            {event.event_type}
                          </div>
                          <div className="mt-1 truncate type-body-sm text-[var(--fg-0)]">
                            {event.new_content ?? event.old_content ?? "设置变更"}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-4">
                <h2 className="type-card-title text-[var(--danger-fg)]">清空记忆</h2>
                <p className="mt-1 type-caption leading-5 text-[var(--danger-fg)]/70">
                  输入“清空”后软删全部，30 天后物理删除。
                </p>
                <input
                  value={clearText}
                  onChange={(e) => setClearText(e.target.value)}
                  placeholder="清空"
                  className="mt-3 h-10 w-full rounded-[var(--radius-control)] border border-danger-border bg-black/20 px-3 text-sm text-[var(--danger-fg)] outline-none placeholder:text-[var(--danger-fg)]/30 focus:border-danger"
                />
                <Button
                  variant="danger"
                  size="md"
                  disabled={clearText !== "清空" || clearMut.isPending}
                  loading={clearMut.isPending}
                  onClick={() => clearMut.mutate()}
                  leftIcon={!clearMut.isPending ? <Trash2 className="h-4 w-4" /> : undefined}
                  fullWidth
                  className="mt-3"
                >
                  清空全部
                </Button>
              </div>
            </section>
          </div>
        </section>
      </div>
      {showCapabilityModal && (
        <CapabilityModal onClose={() => setShowCapabilityModal(false)} />
      )}
    </SettingsShell>
  );
}

function CapabilityModal({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md surface-dialog rounded-[var(--radius-dialog)] p-5"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-2 flex items-center gap-2">
          <AlertTriangle className="h-5 w-5 text-warning" />
          <h3 className="type-card-title">需要 embedding provider</h3>
        </div>
        <p className="type-body-sm leading-6 text-[var(--fg-1)]">
          启用前需在管理员后台为某个 provider 勾选 “embedding” 用途；记忆的写入、检索、抽取均依赖向量。
        </p>
        <div className="mt-5 flex flex-wrap items-center justify-end gap-2">
          <Button variant="outline" size="md" onClick={onClose}>
            {copy.action.confirm}
          </Button>
          <Link
            href="/admin"
            onClick={onClose}
            className="inline-flex h-9 items-center justify-center rounded-[var(--radius-control)] bg-accent px-4 text-sm font-medium text-black"
          >
            去管理员后台
          </Link>
        </div>
      </div>
    </div>
  );
}

function SectionHeader({
  title,
  suffix,
  actions,
}: {
  title: string;
  suffix?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-white/5 p-4">
      <div className="flex items-baseline gap-2">
        <h2 className="type-card-title">{title}</h2>
        {suffix ? <span className="type-caption text-[var(--fg-2)]">{suffix}</span> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

function SettingToggle({
  icon,
  title,
  description,
  checked,
  disabled,
  onChange,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        "flex min-h-[112px] items-start gap-3 rounded-[var(--radius-card)] border p-4 text-left transition-colors disabled:opacity-60",
        checked
          ? "border-accent-border bg-accent-soft"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/60 hover:bg-white/[0.03]",
      ].join(" ")}
    >
      <span className="mt-0.5 text-accent">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block type-body-sm font-medium text-[var(--fg-0)]">{title}</span>
        <span className="mt-1 block type-caption leading-5 text-[var(--fg-2)]">
          {description}
        </span>
      </span>
      <span
        className={[
          "mt-1 inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors",
          checked
            ? "border-accent bg-accent"
            : "border-white/15 bg-white/5",
        ].join(" ")}
        aria-hidden
      >
        <span
          className={[
            "inline-block h-4 w-4 rounded-full bg-black/80 transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          ].join(" ")}
        />
      </span>
    </button>
  );
}

function ScopeButton({
  scope,
  active,
  onSelect,
  onRename,
  onDelete,
}: {
  scope: MemoryScopeOut;
  active: boolean;
  onSelect: () => void;
  onRename: (name: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(scope.name);
  if (editing) {
    return (
      <div className="mt-1 flex gap-1">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="h-8 min-w-0 flex-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-2 text-xs text-[var(--fg-0)] outline-none"
        />
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            onRename(name.trim() || scope.name);
            setEditing(false);
          }}
        >
          {copy.action.save}
        </Button>
      </div>
    );
  }
  return (
    <div className="group mt-1 flex items-center gap-1">
      <button type="button" onClick={onSelect} className={scopeButtonClass(active)}>
        <span className="truncate">
          {scope.emoji ? `${scope.emoji} ` : ""}
          {scope.is_default ? "默认" : scope.name}
        </span>
        <span>{scope.count}</span>
      </button>
      {!scope.is_default && (
        <div className="flex opacity-0 transition-opacity group-hover:opacity-100">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="h-7 rounded-[var(--radius-control)] px-1.5 text-[11px] text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            改
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="h-7 rounded-[var(--radius-control)] px-1.5 text-[11px] text-danger/70 hover:text-danger"
          >
            删
          </button>
        </div>
      )}
    </div>
  );
}

function scopeButtonClass(active: boolean): string {
  return [
    "flex h-9 min-w-0 flex-1 items-center justify-between gap-2 rounded-[var(--radius-control)] px-3 text-sm transition-colors",
    active
      ? "bg-accent-soft text-accent"
      : "text-[var(--fg-1)] hover:bg-white/[0.04] hover:text-[var(--fg-0)]",
  ].join(" ");
}

function MemoryRow({
  memory,
  scopes,
  selectable = false,
  selected = false,
  onToggleSelected,
  editingValue,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memory: MemoryItemOut;
  scopes: MemoryScopeOut[];
  selectable?: boolean;
  selected?: boolean;
  onToggleSelected?: (checked: boolean) => void;
  editingValue?: string;
  onEditValue: (value: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onPatch: (body: Parameters<typeof patchMemory>[1]) => void;
  onDelete: () => void;
}) {
  const isEditing = editingValue != null;
  return (
    <div className={["p-4", memory.disabled ? "opacity-55" : ""].join(" ")}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {selectable ? (
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onToggleSelected?.(event.target.checked)}
            className="h-4 w-4 rounded border-white/20 bg-white/[0.03]"
            aria-label="选择记忆"
          />
        ) : null}
        <TypeBadge type={memory.type} />
        <span className="type-caption text-[var(--fg-2)]">{memory.source}</span>
        <span className="type-caption text-[var(--fg-2)]">{formatTime(memory.updated_at)}</span>
        {memory.pinned ? (
          <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] bg-accent-soft px-1.5 py-0.5 text-[10px] text-accent">
            <Pin className="h-2.5 w-2.5" />
            pinned
          </span>
        ) : null}
      </div>
      {isEditing ? (
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <input
            value={editingValue}
            onChange={(e) => onEditValue(e.target.value)}
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60"
          />
          <div className="flex gap-2">
            <Button variant="primary" size="md" onClick={onSaveEdit}>
              {copy.action.save}
            </Button>
            <Button variant="outline" size="md" onClick={onCancelEdit}>
              {copy.action.cancel}
            </Button>
          </div>
        </div>
      ) : (
        <p className="type-body-sm leading-6 text-[var(--fg-0)]">{memory.content}</p>
      )}
      {memory.source_excerpt ? (
        <p className="mt-2 truncate type-caption text-[var(--fg-2)]">
          来源：{memory.source_excerpt}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => onEditValue(memory.content)}>
          {copy.action.edit}
        </Button>
        <Button variant="outline" size="sm" onClick={() => onPatch({ pinned: !memory.pinned })}>
          {memory.pinned ? "取消 Pin" : "Pin"}
        </Button>
        <Button variant="outline" size="sm" onClick={() => onPatch({ disabled: !memory.disabled })}>
          {memory.disabled ? "启用" : "停用"}
        </Button>
        <select
          value={memory.scope_id}
          onChange={(e) => onPatch({ scope_id: e.target.value })}
          className="h-8 rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.03] px-2 text-xs text-[var(--fg-1)] outline-none"
        >
          {scopes.map((scope) => (
            <option key={scope.id} value={scope.id}>
              {scope.is_default ? "默认" : scope.name}
            </option>
          ))}
        </select>
        <Button
          variant="outline"
          size="sm"
          onClick={onDelete}
          className="text-danger hover:text-danger"
        >
          {copy.action.delete}
        </Button>
      </div>
    </div>
  );
}

function TypeBadge({ type }: { type: MemoryType | string }) {
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-white/[0.04] px-1.5 py-0.5 text-[10px] text-[var(--fg-1)]">
      {typeLabel(type)}
    </span>
  );
}

function LoadingBlock() {
  return (
    <div className="flex items-center justify-center gap-2 p-8 type-body-sm text-[var(--fg-2)]">
      <Loader2 className="h-4 w-4 animate-spin" />
      {copy.state.loading}
    </div>
  );
}

function EmptyBlock({ text }: { text: string }) {
  return <div className="p-8 text-center type-body-sm text-[var(--fg-2)]">{text}</div>;
}
