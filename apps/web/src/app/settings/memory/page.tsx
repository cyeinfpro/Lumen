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
import {
  userMemoryQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import {
  formatTime,
  isEmptyFirstRun,
  removeEditValue,
  TYPE_OPTIONS,
  typeLabel,
} from "./memoryPageUtils";

type MemorySettingsData = Awaited<ReturnType<typeof getMemorySettings>>;
type MemoryTimelineEvent = Awaited<
  ReturnType<typeof listMemoryTimeline>
>["items"][number];
type MemoryPatchBody = Parameters<typeof patchMemory>[1];

export default function MemorySettingsPage() {
  const qc = useQueryClient();
  const userScope = useUserQueryScope();
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
    if (!userScope.enabled) return;
    void qc.invalidateQueries({
      queryKey: userMemoryQueryKeys.all(userScope.userId),
    });
  };

  const settingsQ = useQuery({
    queryKey: userMemoryQueryKeys.settings(userScope.userId),
    queryFn: getMemorySettings,
    enabled: userScope.enabled,
  });
  const scopesQ = useQuery({
    queryKey: userMemoryQueryKeys.scopes(userScope.userId),
    queryFn: listMemoryScopes,
    enabled: userScope.enabled,
  });
  const memoriesQ = useQuery({
    queryKey: userMemoryQueryKeys.items(userScope.userId, selectedScope),
    queryFn: () =>
      listMemories(selectedScope === "all" ? {} : { scope_id: selectedScope }),
    enabled: userScope.enabled,
  });
  const stagingQ = useQuery({
    queryKey: userMemoryQueryKeys.staging(userScope.userId),
    queryFn: listMemoryStaging,
    enabled: userScope.enabled,
  });
  const timelineQ = useQuery({
    queryKey: userMemoryQueryKeys.timeline(userScope.userId),
    queryFn: () => listMemoryTimeline(),
    enabled: userScope.enabled,
  });

  const scopes = useMemo(() => scopesQ.data ?? [], [scopesQ.data]);
  const defaultScope = scopes.find((scope) => scope.is_default) ?? scopes[0];
  // settings 还没加载时默认按 "可用" 处理, 避免首次渲染闪烁出 banner;
  // 加载完后以服务端真实值为准.
  const embeddingAvailable = settingsQ.data?.embedding_available ?? true;
  // 用户尝试启用记忆 (disabled=false), 但服务端没 embedding provider:
  // 不发 mutate, 弹窗提示去 admin 配置.
  const requestEnableMemory = (next: boolean) => {
    if (!userScope.enabled) return;
    if (!embeddingAvailable && next === true) {
      // 用户想 "启用" (off → on, 即 disabled: true → false), 但不可用.
      setShowCapabilityModal(true);
      return;
    }
    settingsMut.mutate({ disabled: !next });
  };
  const memories = useMemo(() => memoriesQ.data?.items ?? [], [memoriesQ.data]);
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
  const emptyFirstRun = isEmptyFirstRun({
    settingsPending: settingsQ.isPending,
    memoriesPending: memoriesQ.isPending,
    memoryCount: memories.length,
    onboardingSeen: settingsQ.data?.onboarding_seen ?? 0,
  });

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
    if (!userScope.enabled) return;
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
      <div className="space-y-5 pb-4 sm:space-y-6">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">记忆</h1>
            <p className="type-body mt-1.5">
              管理账号级长期记忆、候选建议和最近变化。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="h-4 w-4" />
            返回我的
          </Link>
        </header>

        <MemoryCapabilityBanner available={embeddingAvailable} />
        <MemorySettingsToggles
          settings={settingsQ.data}
          embeddingAvailable={embeddingAvailable}
          pending={settingsMut.isPending}
          onEnableChange={requestEnableMemory}
          onPausedChange={(paused) => settingsMut.mutate({ paused })}
          onConfirmationChange={(confirmation_enabled) =>
            settingsMut.mutate({ confirmation_enabled })
          }
        />
        <MemoryFirstRunCard
          visible={emptyFirstRun}
          onPause={() => settingsMut.mutate({ paused: true })}
          onConfirm={() => onboardingMut.mutate(0)}
        />

        <section className="grid min-w-0 gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <MemoryScopeSidebar
            scopes={scopes}
            selectedScope={selectedScope}
            newScopeName={newScopeName}
            newScopeEmoji={newScopeEmoji}
            creating={createScopeMut.isPending}
            onSelectScope={setSelectedScope}
            onRenameScope={(id, name) =>
              patchScopeMut.mutate({ id, body: { name } })
            }
            onDeleteScope={(id) => deleteScopeMut.mutate(id)}
            onNewScopeNameChange={setNewScopeName}
            onNewScopeEmojiChange={setNewScopeEmoji}
            onCreateScope={() =>
              createScopeMut.mutate({
                name: newScopeName.trim(),
                emoji: newScopeEmoji.trim() || null,
              })
            }
          />

          <div className="min-w-0 space-y-5">
            <ManualMemorySection
              typeCounts={typeCounts}
              memoryType={newMemoryType}
              content={newMemoryContent}
              creating={createMemoryMut.isPending}
              onTypeChange={setNewMemoryType}
              onContentChange={setNewMemoryContent}
              onCreate={() =>
                createMemoryMut.mutate({
                  type: newMemoryType,
                  content: newMemoryContent.trim(),
                  scope_id:
                    selectedScope === "all"
                      ? (defaultScope?.id ?? null)
                      : selectedScope,
                })
              }
            />

            <MemoryLibrarySection
              memories={memories}
              filteredMemories={filteredMemories}
              scopes={scopes}
              selectedScope={selectedScope}
              selectedMemoryIds={selectedMemoryIds}
              editing={editing}
              search={memorySearch}
              pending={memoriesQ.isPending}
              bulkMoving={bulkScopeMut.isPending}
              onRefresh={() => void memoriesQ.refetch()}
              onExport={() => void exportJson()}
              onSearchChange={setMemorySearch}
              onBulkMove={(scopeId) =>
                bulkScopeMut.mutate({
                  ids: Array.from(selectedMemoryIds),
                  scopeId,
                })
              }
              onToggleSelected={(id, checked) =>
                setSelectedMemoryIds((prev) => {
                  const next = new Set(prev);
                  if (checked) next.add(id);
                  else next.delete(id);
                  return next;
                })
              }
              onEditValue={(id, value) =>
                setEditing((prev) => ({ ...prev, [id]: value }))
              }
              onSaveEdit={(memory) => {
                const content = editing[memory.id]?.trim();
                if (content && content !== memory.content) {
                  patchMemoryMut.mutate({
                    id: memory.id,
                    body: { content },
                  });
                }
                setEditing((prev) => removeEditValue(prev, memory.id));
              }}
              onCancelEdit={(id) =>
                setEditing((prev) => removeEditValue(prev, id))
              }
              onPatch={(id, body) => patchMemoryMut.mutate({ id, body })}
              onDelete={(id) => deleteMemoryMut.mutate(id)}
            />

            <MemoryStagingSection
              staging={staging}
              scopes={scopes}
              edits={stagingEdits}
              pending={stagingQ.isPending}
              onEdit={(id, value) =>
                setStagingEdits((prev) => ({ ...prev, [id]: value }))
              }
              onScopeChange={(id, scopeId) =>
                patchStagingMut.mutate({
                  id,
                  body: { scope_id: scopeId },
                })
              }
              onAccept={(item) => acceptMut.mutate(item)}
              onReject={(id) => rejectMut.mutate(id)}
            />

            <MemoryTimelineAndClear
              events={timelineQ.data?.items ?? []}
              timelinePending={timelineQ.isPending}
              clearText={clearText}
              clearing={clearMut.isPending}
              onClearTextChange={setClearText}
              onClear={() => clearMut.mutate()}
            />
          </div>
        </section>
      </div>
      <MemoryCapabilityModal
        open={showCapabilityModal}
        onClose={() => setShowCapabilityModal(false)}
      />
    </SettingsShell>
  );
}

function MemoryCapabilityBanner({ available }: { available: boolean }) {
  if (available) return null;
  return (
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
        className="inline-flex min-h-11 flex-shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 type-caption font-medium text-warning transition-colors hover:bg-warning/20"
      >
        去管理员后台 →
      </Link>
    </section>
  );
}

function MemorySettingsToggles({
  settings,
  embeddingAvailable,
  pending,
  onEnableChange,
  onPausedChange,
  onConfirmationChange,
}: {
  settings: MemorySettingsData | undefined;
  embeddingAvailable: boolean;
  pending: boolean;
  onEnableChange: (checked: boolean) => void;
  onPausedChange: (checked: boolean) => void;
  onConfirmationChange: (checked: boolean) => void;
}) {
  const memoryDisabled = Boolean(settings?.disabled);
  const dependentDisabled =
    pending || !embeddingAvailable || memoryDisabled;
  return (
    <section className="grid gap-3 md:grid-cols-3">
      <SettingToggle
        icon={<Brain className="h-4 w-4" />}
        title="启用记忆"
        description="开启后 Lumen 会从对话中学习稳定偏好,并在新会话里复用。"
        checked={!memoryDisabled}
        disabled={pending}
        onChange={onEnableChange}
      />
      <SettingToggle
        icon={<Pause className="h-4 w-4" />}
        title="暂停学习"
        description="不写入新记忆,已有记忆仍会参与回答。"
        checked={Boolean(settings?.paused)}
        disabled={dependentDisabled}
        onChange={onPausedChange}
      />
      <SettingToggle
        icon={<ShieldOff className="h-4 w-4" />}
        title="主动确认偏好"
        description="强偏好命中时,偶尔让模型先确认。"
        checked={Boolean(settings?.confirmation_enabled)}
        disabled={dependentDisabled}
        onChange={onConfirmationChange}
      />
    </section>
  );
}

function MemoryFirstRunCard({
  visible,
  onPause,
  onConfirm,
}: {
  visible: boolean;
  onPause: () => void;
  onConfirm: () => void;
}) {
  if (!visible) return null;
  return (
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
          <Button variant="outline" size="sm" onClick={onPause}>
            先暂停
          </Button>
          <Button variant="primary" size="sm" onClick={onConfirm}>
            {copy.action.confirm}
          </Button>
        </div>
      </div>
    </section>
  );
}

function MemoryScopeSidebar({
  scopes,
  selectedScope,
  newScopeName,
  newScopeEmoji,
  creating,
  onSelectScope,
  onRenameScope,
  onDeleteScope,
  onNewScopeNameChange,
  onNewScopeEmojiChange,
  onCreateScope,
}: {
  scopes: MemoryScopeOut[];
  selectedScope: string;
  newScopeName: string;
  newScopeEmoji: string;
  creating: boolean;
  onSelectScope: (scopeId: string) => void;
  onRenameScope: (scopeId: string, name: string) => void;
  onDeleteScope: (scopeId: string) => void;
  onNewScopeNameChange: (name: string) => void;
  onNewScopeEmojiChange: (emoji: string) => void;
  onCreateScope: () => void;
}) {
  const totalCount = scopes.reduce((sum, scope) => sum + scope.count, 0);
  return (
    <aside className="min-w-0 space-y-3">
      <div className="flex min-w-0 gap-1 overflow-x-auto rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-2 [scrollbar-width:none] lg:block lg:overflow-visible lg:p-3 [&::-webkit-scrollbar]:hidden">
        <button
          type="button"
          onClick={() => onSelectScope("all")}
          className={scopeButtonClass(selectedScope === "all")}
        >
          <span>全部</span>
          <span>{totalCount}</span>
        </button>
        {scopes.map((scope) => (
          <ScopeButton
            key={scope.id}
            scope={scope}
            active={selectedScope === scope.id}
            onSelect={() => onSelectScope(scope.id)}
            onRename={(name) => onRenameScope(scope.id, name)}
            onDelete={() => onDeleteScope(scope.id)}
          />
        ))}
      </div>

      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-3">
        <div className="mb-2 type-caption font-medium text-[var(--fg-1)]">
          新作用域
        </div>
        <div className="flex gap-2">
          <input
            value={newScopeEmoji}
            onChange={(event) =>
              onNewScopeEmojiChange(event.target.value.slice(0, 4))
            }
            placeholder="图标"
            className="h-11 w-14 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 md:h-9"
          />
          <input
            value={newScopeName}
            onChange={(event) => onNewScopeNameChange(event.target.value)}
            placeholder="工作"
            className="h-11 min-w-0 flex-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 md:h-9"
          />
          <IconButton
            variant="primary"
            disabled={!newScopeName.trim() || creating}
            onClick={onCreateScope}
            aria-label="创建作用域"
          >
            <Plus className="h-4 w-4" />
          </IconButton>
        </div>
      </div>
    </aside>
  );
}

function ManualMemorySection({
  typeCounts,
  memoryType,
  content,
  creating,
  onTypeChange,
  onContentChange,
  onCreate,
}: {
  typeCounts: Record<string, number>;
  memoryType: MemoryType;
  content: string;
  creating: boolean;
  onTypeChange: (type: MemoryType) => void;
  onContentChange: (content: string) => void;
  onCreate: () => void;
}) {
  return (
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
          value={memoryType}
          onChange={(event) =>
            onTypeChange(event.target.value as MemoryType)
          }
          className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 sm:h-10"
        >
          {TYPE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <input
          value={content}
          onChange={(event) => onContentChange(event.target.value)}
          placeholder="例如：偏好 200 字以内的回答"
          maxLength={200}
          className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60 sm:h-10"
        />
        <Button
          variant="primary"
          size="md"
          disabled={!content.trim() || creating}
          loading={creating}
          onClick={onCreate}
          leftIcon={!creating ? <Plus className="h-4 w-4" /> : undefined}
        >
          添加
        </Button>
      </div>
    </section>
  );
}

function MemoryLibrarySection({
  memories,
  filteredMemories,
  scopes,
  selectedScope,
  selectedMemoryIds,
  editing,
  search,
  pending,
  bulkMoving,
  onRefresh,
  onExport,
  onSearchChange,
  onBulkMove,
  onToggleSelected,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memories: MemoryItemOut[];
  filteredMemories: MemoryItemOut[];
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  editing: Record<string, string>;
  search: string;
  pending: boolean;
  bulkMoving: boolean;
  onRefresh: () => void;
  onExport: () => void;
  onSearchChange: (value: string) => void;
  onBulkMove: (scopeId: string) => void;
  onToggleSelected: (id: string, checked: boolean) => void;
  onEditValue: (id: string, value: string) => void;
  onSaveEdit: (memory: MemoryItemOut) => void;
  onCancelEdit: (id: string) => void;
  onPatch: (id: string, body: MemoryPatchBody) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader
        title="记忆库"
        suffix={`${filteredMemories.length}/${memories.length} 条`}
        actions={
          <>
            <IconButton
              variant="outline"
              size="md"
              onClick={onRefresh}
              aria-label="刷新记忆"
              tooltip="刷新"
            >
              <RefreshCw className="h-4 w-4" />
            </IconButton>
            <Button
              variant="outline"
              size="sm"
              onClick={onExport}
              leftIcon={<Download className="h-3.5 w-3.5" />}
            >
              {copy.action.export}
            </Button>
          </>
        }
      />
      <MemoryLibraryToolbar
        scopes={scopes}
        selectedScope={selectedScope}
        selectedMemoryIds={selectedMemoryIds}
        search={search}
        bulkMoving={bulkMoving}
        onSearchChange={onSearchChange}
        onBulkMove={onBulkMove}
      />
      <MemoryLibraryList
        memories={filteredMemories}
        scopes={scopes}
        selectedScope={selectedScope}
        selectedMemoryIds={selectedMemoryIds}
        editing={editing}
        pending={pending}
        onToggleSelected={onToggleSelected}
        onEditValue={onEditValue}
        onSaveEdit={onSaveEdit}
        onCancelEdit={onCancelEdit}
        onPatch={onPatch}
        onDelete={onDelete}
      />
    </section>
  );
}

function MemoryLibraryToolbar({
  scopes,
  selectedScope,
  selectedMemoryIds,
  search,
  bulkMoving,
  onSearchChange,
  onBulkMove,
}: {
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  search: string;
  bulkMoving: boolean;
  onSearchChange: (value: string) => void;
  onBulkMove: (scopeId: string) => void;
}) {
  const showBulkActions =
    selectedScope === "all" && selectedMemoryIds.size > 0;
  const searchPlaceholder =
    selectedScope === "all" ? "跨作用域搜索" : "搜索当前作用域";
  return (
    <div className="flex flex-col gap-2 border-t border-[var(--border-subtle)] p-3 sm:flex-row sm:items-center sm:justify-between">
      <label className="relative min-w-0 flex-1">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={searchPlaceholder}
          className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] pl-9 pr-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-[var(--accent)]/60 sm:h-9"
        />
      </label>
      {showBulkActions ? (
        <div className="flex flex-wrap items-center gap-2 type-caption text-[var(--fg-1)]">
          <span>已选 {selectedMemoryIds.size} 条</span>
          <select
            disabled={bulkMoving}
            onChange={(event) => {
              const scopeId = event.target.value;
              if (!scopeId) return;
              onBulkMove(scopeId);
              event.currentTarget.value = "";
            }}
            className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-xs text-[var(--fg-0)] outline-none sm:h-9"
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
  );
}

function MemoryLibraryList({
  memories,
  scopes,
  selectedScope,
  selectedMemoryIds,
  editing,
  pending,
  onToggleSelected,
  onEditValue,
  onSaveEdit,
  onCancelEdit,
  onPatch,
  onDelete,
}: {
  memories: MemoryItemOut[];
  scopes: MemoryScopeOut[];
  selectedScope: string;
  selectedMemoryIds: Set<string>;
  editing: Record<string, string>;
  pending: boolean;
  onToggleSelected: (id: string, checked: boolean) => void;
  onEditValue: (id: string, value: string) => void;
  onSaveEdit: (memory: MemoryItemOut) => void;
  onCancelEdit: (id: string) => void;
  onPatch: (id: string, body: MemoryPatchBody) => void;
  onDelete: (id: string) => void;
}) {
  if (pending) return <LoadingBlock />;
  if (memories.length === 0) {
    return <EmptyBlock text="当前作用域还没有记忆。" />;
  }
  const selectable = selectedScope === "all";
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {memories.map((memory) => (
        <MemoryRow
          key={memory.id}
          memory={memory}
          scopes={scopes}
          selectable={selectable}
          selected={selectedMemoryIds.has(memory.id)}
          onToggleSelected={(checked) =>
            onToggleSelected(memory.id, checked)
          }
          editingValue={editing[memory.id]}
          onEditValue={(value) => onEditValue(memory.id, value)}
          onSaveEdit={() => onSaveEdit(memory)}
          onCancelEdit={() => onCancelEdit(memory.id)}
          onPatch={(body) => onPatch(memory.id, body)}
          onDelete={() => onDelete(memory.id)}
        />
      ))}
    </div>
  );
}

function MemoryStagingSection({
  staging,
  scopes,
  edits,
  pending,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  staging: MemoryStagingOut[];
  scopes: MemoryScopeOut[];
  edits: Record<string, string>;
  pending: boolean;
  onEdit: (id: string, value: string) => void;
  onScopeChange: (id: string, scopeId: string) => void;
  onAccept: (item: MemoryStagingOut) => void;
  onReject: (id: string) => void;
}) {
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader
        title="建议加入记忆"
        suffix={`${staging.length} 条`}
      />
      <MemoryStagingList
        staging={staging}
        scopes={scopes}
        edits={edits}
        pending={pending}
        onEdit={onEdit}
        onScopeChange={onScopeChange}
        onAccept={onAccept}
        onReject={onReject}
      />
    </section>
  );
}

function MemoryStagingList({
  staging,
  scopes,
  edits,
  pending,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  staging: MemoryStagingOut[];
  scopes: MemoryScopeOut[];
  edits: Record<string, string>;
  pending: boolean;
  onEdit: (id: string, value: string) => void;
  onScopeChange: (id: string, scopeId: string) => void;
  onAccept: (item: MemoryStagingOut) => void;
  onReject: (id: string) => void;
}) {
  if (pending) return <LoadingBlock />;
  if (staging.length === 0) {
    return <EmptyBlock text="暂无待确认候选。" />;
  }
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {staging.map((item) => (
        <MemoryStagingRow
          key={item.id}
          item={item}
          scopes={scopes}
          value={edits[item.id] ?? item.content}
          onEdit={(value) => onEdit(item.id, value)}
          onScopeChange={(scopeId) => onScopeChange(item.id, scopeId)}
          onAccept={() => onAccept(item)}
          onReject={() => onReject(item.id)}
        />
      ))}
    </div>
  );
}

function MemoryStagingRow({
  item,
  scopes,
  value,
  onEdit,
  onScopeChange,
  onAccept,
  onReject,
}: {
  item: MemoryStagingOut;
  scopes: MemoryScopeOut[];
  value: string;
  onEdit: (value: string) => void;
  onScopeChange: (scopeId: string) => void;
  onAccept: () => void;
  onReject: () => void;
}) {
  return (
    <div className="p-4">
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
        value={value}
        onChange={(event) => onEdit(event.target.value)}
        className="mb-3 h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 sm:h-10"
      />
      <div className="flex flex-wrap gap-2">
        <select
          value={item.scope_id}
          onChange={(event) => onScopeChange(event.target.value)}
          className="h-11 min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-2 text-base text-[var(--fg-1)] outline-none sm:h-8 sm:text-xs"
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
          onClick={onAccept}
          leftIcon={<Check className="h-3.5 w-3.5" />}
          className="bg-success-soft text-success hover:bg-success/20"
        >
          接受
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onReject}
          leftIcon={<X className="h-3.5 w-3.5" />}
        >
          拒绝
        </Button>
      </div>
    </div>
  );
}

function MemoryTimelineAndClear({
  events,
  timelinePending,
  clearText,
  clearing,
  onClearTextChange,
  onClear,
}: {
  events: MemoryTimelineEvent[];
  timelinePending: boolean;
  clearText: string;
  clearing: boolean;
  onClearTextChange: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
      <MemoryTimelinePanel events={events} pending={timelinePending} />
      <MemoryClearPanel
        clearText={clearText}
        clearing={clearing}
        onClearTextChange={onClearTextChange}
        onClear={onClear}
      />
    </section>
  );
}

function MemoryTimelinePanel({
  events,
  pending,
}: {
  events: MemoryTimelineEvent[];
  pending: boolean;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60">
      <SectionHeader title="最近变化" />
      <MemoryTimelineList events={events} pending={pending} />
    </div>
  );
}

function MemoryTimelineList({
  events,
  pending,
}: {
  events: MemoryTimelineEvent[];
  pending: boolean;
}) {
  if (pending) return <LoadingBlock />;
  if (events.length === 0) {
    return <EmptyBlock text="还没有审计事件。" />;
  }
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      {events.map((event) => (
        <div
          key={event.id}
          className="grid gap-1 p-4 sm:grid-cols-[100px_minmax(0,1fr)]"
        >
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
  );
}

function MemoryClearPanel({
  clearText,
  clearing,
  onClearTextChange,
  onClear,
}: {
  clearText: string;
  clearing: boolean;
  onClearTextChange: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-4">
      <h2 className="type-card-title text-[var(--danger-fg)]">清空记忆</h2>
      <p className="mt-1 type-caption leading-5 text-[var(--danger-fg)]/70">
        输入“清空”后软删全部，30 天后物理删除。
      </p>
      <input
        value={clearText}
        onChange={(event) => onClearTextChange(event.target.value)}
        placeholder="清空"
        className="mt-3 h-11 w-full rounded-[var(--radius-control)] border border-danger-border bg-[var(--bg-0)]/70 px-3 text-base text-[var(--danger-fg)] outline-none placeholder:text-[var(--danger-fg)]/50 focus:border-danger sm:h-10 sm:text-sm"
      />
      <Button
        variant="danger"
        size="md"
        disabled={clearText !== "清空" || clearing}
        loading={clearing}
        onClick={onClear}
        leftIcon={!clearing ? <Trash2 className="h-4 w-4" /> : undefined}
        fullWidth
        className="mt-3"
      >
        清空全部
      </Button>
    </div>
  );
}

function MemoryCapabilityModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return <CapabilityModal onClose={onClose} />;
}

function CapabilityModal({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-sm mobile-dialog-shell sm:items-center"
      onClick={onClose}
    >
      <div
        className="mobile-dialog-panel flex w-full max-w-md flex-col overflow-hidden rounded-t-[var(--radius-dialog)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] p-5 shadow-[var(--shadow-3)] sm:rounded-[var(--radius-dialog)] sm:border-b"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-capability-title"
      >
        <div className="mobile-dialog-scroll min-h-0 overflow-y-auto pr-0.5">
          <div className="mb-2 flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 shrink-0 text-warning" />
            <h3 id="memory-capability-title" className="type-card-title">
              需要 embedding provider
            </h3>
          </div>
          <p className="type-body-sm leading-6 text-[var(--fg-1)]">
            启用前需在管理员后台为某个 provider 勾选 “embedding” 用途；记忆的写入、检索、抽取均依赖向量。
          </p>
        </div>
        <div className="mobile-dialog-footer -mx-5 mt-5 flex shrink-0 flex-col gap-2 border-t border-[var(--border)] px-5 pt-3 sm:mx-0 sm:flex-row sm:justify-end sm:border-t-0 sm:px-0 sm:pt-0">
          <Button variant="outline" size="md" onClick={onClose}>
            {copy.action.confirm}
          </Button>
          <Link
            href="/admin"
            onClick={onClose}
            className="inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] bg-accent px-4 text-sm font-medium text-black"
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
    <div className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] p-4">
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
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/60 hover:bg-[var(--bg-3)]",
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
            : "border-[var(--border)] bg-[var(--bg-2)]",
        ].join(" ")}
        aria-hidden
      >
        <span
          className={[
            "inline-block h-4 w-4 rounded-full bg-[var(--accent-on)] transition-transform",
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
          className="h-11 min-w-0 flex-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-xs text-[var(--fg-0)] outline-none md:h-8"
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
            className="min-h-11 min-w-11 rounded-[var(--radius-control)] px-2 text-[11px] text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] lg:min-h-8 lg:min-w-8"
          >
            改
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="min-h-11 min-w-11 rounded-[var(--radius-control)] px-2 text-[11px] text-danger/70 hover:bg-danger-soft hover:text-danger lg:min-h-8 lg:min-w-8"
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
    "flex min-h-11 min-w-max flex-1 items-center justify-between gap-2 rounded-[var(--radius-control)] px-3 text-sm transition-colors lg:h-9 lg:min-h-0 lg:min-w-0",
    active
      ? "bg-accent-soft text-accent"
      : "text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
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
            className="h-4 w-4 rounded border-[var(--border-strong)] bg-[var(--bg-2)]"
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
            className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-3 text-base text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/60 sm:h-10 sm:text-sm"
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
          className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-2 text-base text-[var(--fg-1)] outline-none sm:h-8 sm:text-xs"
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
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 text-[10px] text-[var(--fg-1)]">
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
