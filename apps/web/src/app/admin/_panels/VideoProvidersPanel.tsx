"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  Clapperboard,
  Gauge,
  KeyRound,
  Layers3,
  Pencil,
  Plus,
  Power,
  Save,
  Server,
  ShieldCheck,
  Trash2,
  X,
  Zap,
} from "lucide-react";

import {
  useUpdateVideoProvidersMutation,
  useVideoProvidersQuery,
} from "@/lib/queries";
import type { VideoProviderItemOut, VideoProviderKind } from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { ErrorBlock } from "../_components/AdminFeedback";
import {
  ACTION_LABELS,
  KIND_LABELS,
  VIDEO_ACTIONS,
  VOLCANO_DEFAULT_PROJECT_NAME,
  VOLCANO_DEFAULT_REGION,
  actionCoverageLabel,
  analyzeDrafts,
  analyzeProvider,
  draftSaveError,
  draftToInput,
  draftWasRenamed,
  emptyDashScopeDraft,
  emptyFakeDraft,
  emptyModelDraft,
  emptyOmniFlashDraft,
  emptyVeoDraft,
  emptyVolcanoDraft,
  emptyVolcanoNewApiDraft,
  emptyVolcanoThirdPartyDraft,
  inferVolcanoRegion,
  issueTone,
  mirroredModelPatch,
  modelNamesFromModels,
  normalizeVideoProviderEnabled,
  presetPatchForKind,
  saveError,
  sourceLabel,
  storedDraftHints,
  summaryIsUsable,
  toDraft,
  videoProviderKindCanBeEnabled,
  type Draft,
  type Issue,
  type ModelDraft,
  type ProviderSummary,
  type VideoAction,
} from "./videoProviderPanelDomain";

/*
 * Legacy source-contract markers for tests that inspect this facade directly.
 * Implementations live in videoProviderPanelDomain.ts.
 * function videoProviderKindCanBeEnabled(kind) { return kind !== "veo"; }
 * enabled: normalizeVideoProviderEnabled(item.kind, item.enabled)
 * function veoPresetPatch() { return { kind: "veo", enabled: false }; }
 * enabled: normalizeVideoProviderEnabled(draft.kind, draft.enabled)
 * function isOmniFlashPlaceholderBaseUrl() { return "api.example.com"; }
 * isOmniFlashPlaceholderBaseUrl(draft.kind, draft.base_url)
 */

function DraftProviderEditor({
  draft,
  index,
  summary,
  serverItems,
  proxies,
  updateDraft,
  updateModel,
  deleteDraft,
}: {
  draft: Draft;
  index: number;
  summary: ProviderSummary | undefined;
  serverItems: VideoProviderItemOut[];
  proxies: string[];
  updateDraft: (index: number, patch: Partial<Draft>) => void;
  updateModel: (
    providerIndex: number,
    modelIndex: number,
    patch: Partial<ModelDraft>,
  ) => void;
  deleteDraft: (index: number) => void;
}) {
  const stored = storedDraftHints(draft, serverItems);
  return (
    <ProviderEditor
      draft={draft}
      summary={summary}
      storedKeyHint={stored.key}
      storedAccessKeyIdHint={stored.accessKeyId}
      storedSecretAccessKeyHint={stored.secretAccessKey}
      storedAssetManagementReady={stored.assetManagementReady}
      storedAssetCredentialsRequireReplacement={
        stored.assetCredentialsRequireReplacement
      }
      proxies={proxies}
      onPatch={(patch) => updateDraft(index, patch)}
      onDelete={() => deleteDraft(index)}
      onAddModel={() =>
        updateDraft(index, {
          models: [...draft.models, emptyModelDraft()],
        })
      }
      onApplyPreset={() => updateDraft(index, presetPatchForKind(draft))}
      onPatchModel={(modelIndex, patch) =>
        updateModel(index, modelIndex, patch)
      }
      onMirrorModel={(modelIndex) => {
        const patch = mirroredModelPatch(draft.models[modelIndex]);
        if (patch) updateModel(index, modelIndex, patch);
      }}
      onDeleteModel={(modelIndex) =>
        updateDraft(index, {
          models: draft.models.filter(
            (_model, candidateIndex) => candidateIndex !== modelIndex,
          ),
        })
      }
    />
  );
}

function draftStatusLabel(
  globalIssue: string | null,
  errorCount: number,
  warningCount: number,
): string {
  if (globalIssue) return globalIssue;
  if (errorCount > 0) return `还有 ${errorCount} 个错误需要处理`;
  if (warningCount > 0) return `${warningCount} 个提示不会阻止保存`;
  return "配置可以保存";
}

type ProviderPanelMetrics = {
  enabledCount: number;
  usableCount: number;
  totalConcurrency: number;
  coveredActions: Set<VideoAction>;
  issues: Issue[];
};

function summarizeProviders(
  summaries: ProviderSummary[],
): ProviderPanelMetrics {
  const coveredActions = new Set<VideoAction>();
  const issues: Issue[] = [];
  let enabledCount = 0;
  let usableCount = 0;
  let totalConcurrency = 0;
  for (const summary of summaries) {
    if (summary.enabled) {
      enabledCount += 1;
      totalConcurrency += summary.concurrency;
    }
    if (summary.enabled && summary.hasKey && summary.modelNames.length > 0) {
      usableCount += 1;
    }
    if (summary.enabled && summary.hasKey) {
      summary.capabilities.forEach((action) => coveredActions.add(action));
    }
    issues.push(
      ...summary.issues.map((issue) => ({
        ...issue,
        message: `${summary.name}：${issue.message}`,
      })),
    );
  }
  return {
    enabledCount,
    usableCount,
    totalConcurrency,
    coveredActions,
    issues,
  };
}

type DraftPanelMetrics = {
  errorCount: number;
  warningCount: number;
  globalIssue: string | null;
  statusText: string;
};

function summarizeDrafts(
  summaries: ProviderSummary[],
  enabled: boolean,
): DraftPanelMetrics {
  let errorCount = 0;
  let warningCount = 0;
  for (const summary of summaries) {
    errorCount += summary.issues.filter(
      (issue) => issue.severity === "error",
    ).length;
    warningCount += summary.issues.filter(
      (issue) => issue.severity === "warning",
    ).length;
  }
  const globalIssue =
    enabled && !summaries.some(summaryIsUsable)
      ? "启用视频生成前至少需要一个启用且可用的供应商"
      : null;
  return {
    errorCount,
    warningCount,
    globalIssue,
    statusText: draftStatusLabel(globalIssue, errorCount, warningCount),
  };
}

function ProviderPanelHeader({
  editing,
  enabled,
  source,
  serverItems,
  metrics,
  onEdit,
}: {
  editing: boolean;
  enabled: boolean;
  source: string | undefined;
  serverItems: VideoProviderItemOut[];
  metrics: ProviderPanelMetrics;
  onEdit: () => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-panel)] border border-accent-border bg-accent-soft">
              <Clapperboard className="h-4 w-4 text-accent" />
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-[var(--fg-0)]">
                AI 视频供应商
              </h3>
              <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                Seedance / HappyHorse / Omni Flash · 模型映射与并发路由
              </p>
            </div>
          </div>
        </div>
        {!editing && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={onEdit}
              leftIcon={<Pencil className="h-3.5 w-3.5" />}
            >
              编辑
            </Button>
          </div>
        )}
      </div>
      {serverItems.length > 0 && !editing && (
        <VideoStatsRow
          enabled={enabled}
          source={source}
          providerCount={serverItems.length}
          enabledCount={metrics.enabledCount}
          usableCount={metrics.usableCount}
          totalConcurrency={metrics.totalConcurrency}
          coveredActions={metrics.coveredActions}
        />
      )}
    </div>
  );
}

function ProviderPanelFeedback({
  error,
  saved,
  onDismissError,
}: {
  error: string | null;
  saved: boolean;
  onDismissError: () => void;
}) {
  return (
    <>
      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="flex-1">{error}</span>
          <IconButton
            variant="ghost"
            size="sm"
            aria-label="关闭"
            onClick={onDismissError}
          >
            <X className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      )}
      {saved && (
        <div
          aria-live="polite"
          className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-4 py-3 type-body-sm text-success"
        >
          <Check className="h-4 w-4" />
          已保存
        </div>
      )}
    </>
  );
}

function ProviderOverview({
  enabled,
  serverItems,
  summaries,
  metrics,
  onCreate,
}: {
  enabled: boolean;
  serverItems: VideoProviderItemOut[];
  summaries: ProviderSummary[];
  metrics: ProviderPanelMetrics;
  onCreate: () => void;
}) {
  return (
    <>
      <ReadinessNotice
        enabled={enabled}
        usableCount={metrics.usableCount}
        coveredActions={metrics.coveredActions}
        providerIssues={metrics.issues}
      />
      <div className="space-y-5">
        {serverItems.map((item) => (
          <ProviderCard
            key={item.name}
            item={item}
            summary={summaries.find((summary) => summary.name === item.name)}
          />
        ))}
        {serverItems.length === 0 && <EmptyState onCreate={onCreate} />}
      </div>
    </>
  );
}

function ProviderEditorView({
  drafts,
  summaries,
  metrics,
  enabled,
  source,
  serverItems,
  proxyNames,
  saving,
  onToggle,
  onAddDraft,
  updateDraft,
  updateModel,
  deleteDraft,
  onDiscard,
  onSave,
}: {
  drafts: Draft[];
  summaries: ProviderSummary[];
  metrics: DraftPanelMetrics;
  enabled: boolean;
  source: string | undefined;
  serverItems: VideoProviderItemOut[];
  proxyNames: string[];
  saving: boolean;
  onToggle: (value: boolean) => void;
  onAddDraft: (draft: Draft) => void;
  updateDraft: (index: number, patch: Partial<Draft>) => void;
  updateModel: (
    providerIndex: number,
    modelIndex: number,
    patch: Partial<ModelDraft>,
  ) => void;
  deleteDraft: (index: number) => void;
  onDiscard: () => void;
  onSave: () => void;
}) {
  return (
    <div className="space-y-5">
      <EditCommandCenter
        enabled={enabled}
        source={source}
        draftCount={drafts.length}
        errorCount={metrics.errorCount + (metrics.globalIssue ? 1 : 0)}
        warningCount={metrics.warningCount}
        globalIssue={metrics.globalIssue}
        onToggle={onToggle}
        onAddVolcano={() => onAddDraft(emptyVolcanoDraft())}
        onAddVolcanoThirdParty={() => onAddDraft(emptyVolcanoThirdPartyDraft())}
        onAddVolcanoNewApi={() => onAddDraft(emptyVolcanoNewApiDraft())}
        onAddDashscope={() => onAddDraft(emptyDashScopeDraft())}
        onAddVeo={() => onAddDraft(emptyVeoDraft())}
        onAddOmniFlash={() => onAddDraft(emptyOmniFlashDraft())}
        onAddFake={() => onAddDraft(emptyFakeDraft())}
      />
      <div className="space-y-4">
        {drafts.map((draft, index) => (
          <DraftProviderEditor
            key={draft._key}
            draft={draft}
            index={index}
            summary={summaries[index]}
            serverItems={serverItems}
            proxies={proxyNames}
            updateDraft={updateDraft}
            updateModel={updateModel}
            deleteDraft={deleteDraft}
          />
        ))}
        {drafts.length === 0 && (
          <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-5 py-8 text-center">
            <p className="text-sm font-medium text-[var(--fg-0)]">
              暂无编辑中的供应商
            </p>
            <p className="mt-1 type-caption text-[var(--fg-2)]">
              使用上方预设添加 Seedance 或 HappyHorse。
            </p>
          </div>
        )}
      </div>
      <div className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[env(safe-area-inset-bottom)] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4">
        <div className="flex items-center gap-2 rounded-[var(--radius-dialog)] border border-[var(--color-lumen-amber)]/40 bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[var(--shadow-3)] backdrop-blur-xl sm:gap-3 sm:px-4">
          <span className="min-w-0 type-caption text-[var(--fg-1)]">
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-lumen-amber)] shadow-[var(--shadow-amber)]" />
              编辑中
              <span className="text-[var(--fg-2)]">·</span>
              <span className="font-mono tabular-nums">{drafts.length}</span>
              <span>个供应商</span>
            </span>
            <span className="ml-2 hidden text-[var(--fg-2)] sm:inline">
              {metrics.statusText}
            </span>
          </span>
          <div className="flex-1 sm:flex-none" />
          <Button
            variant="secondary"
            size="sm"
            onClick={onDiscard}
            disabled={saving}
          >
            放弃
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={onSave}
            disabled={saving}
            loading={saving}
            leftIcon={!saving ? <Save className="h-3.5 w-3.5" /> : undefined}
          >
            {saving ? "保存中" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function VideoProvidersPanel() {
  const query = useVideoProvidersQuery();
  const updateMut = useUpdateVideoProvidersMutation();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [enabledDraft, setEnabledDraft] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const serverItems = useMemo(
    () => query.data?.items ?? [],
    [query.data?.items],
  );
  const proxyOptions = useMemo(
    () => query.data?.proxies ?? [],
    [query.data?.proxies],
  );
  const editing = drafts !== null;

  const providerSummaries = useMemo(
    () => serverItems.map(analyzeProvider),
    [serverItems],
  );
  const draftSummaries = useMemo(
    () => (drafts ? analyzeDrafts(drafts, enabledDraft, serverItems) : []),
    [drafts, enabledDraft, serverItems],
  );
  const providerMetrics = useMemo(
    () => summarizeProviders(providerSummaries),
    [providerSummaries],
  );
  const draftMetrics = useMemo(
    () => summarizeDrafts(draftSummaries, enabledDraft),
    [draftSummaries, enabledDraft],
  );

  const startEdit = () => {
    setDrafts(serverItems.map(toDraft));
    setEnabledDraft(Boolean(query.data?.enabled));
    setError(null);
    setSaved(false);
  };

  const addDraft = (draft: Draft) => {
    setDrafts((prev) => [...(prev ?? []), draft]);
  };

  const updateDraft = (idx: number, patch: Partial<Draft>) => {
    setDrafts((prev) => {
      if (!prev || idx < 0 || idx >= prev.length) return prev;
      const next = [...prev];
      const patched = { ...next[idx], ...patch };
      next[idx] = {
        ...patched,
        enabled: normalizeVideoProviderEnabled(patched.kind, patched.enabled),
      };
      return next;
    });
  };

  const updateModel = (
    providerIdx: number,
    modelIdx: number,
    patch: Partial<ModelDraft>,
  ) => {
    setDrafts((prev) => {
      if (
        !prev ||
        providerIdx < 0 ||
        providerIdx >= prev.length ||
        modelIdx < 0 ||
        modelIdx >= prev[providerIdx].models.length
      ) {
        return prev;
      }
      const next = [...prev];
      const models = [...next[providerIdx].models];
      models[modelIdx] = { ...models[modelIdx], ...patch };
      next[providerIdx] = { ...next[providerIdx], models };
      return next;
    });
  };

  const deleteDraft = (index: number) => {
    setDrafts(
      (previous) =>
        previous?.filter(
          (_draft, candidateIndex) => candidateIndex !== index,
        ) ?? null,
    );
  };

  const save = () => {
    if (!drafts || updateMut.isPending) return;
    setError(null);
    const validationError = draftSaveError(drafts, enabledDraft, serverItems);
    if (validationError) {
      setError(validationError);
      return;
    }
    const items = drafts.map(draftToInput);
    updateMut.mutate(
      { enabled: enabledDraft, items },
      {
        onSuccess: () => {
          setDrafts(null);
          setSaved(true);
        },
        onError: (err) => setError(saveError(err)),
      },
    );
  };

  const discard = () => {
    setDrafts(null);
    setError(null);
  };

  if (query.isLoading) {
    return (
      <section className="space-y-4" aria-busy="true">
        <div className="h-28 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
        <div className="h-36 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
        <div className="h-56 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
      </section>
    );
  }

  if (query.isError) {
    return (
      <ErrorBlock
        message={query.error?.message ?? "加载失败"}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <section className="space-y-5 pb-28">
      <ProviderPanelHeader
        editing={editing}
        enabled={Boolean(query.data?.enabled)}
        source={query.data?.source}
        serverItems={serverItems}
        metrics={providerMetrics}
        onEdit={startEdit}
      />

      <ProviderPanelFeedback
        error={error}
        saved={saved}
        onDismissError={() => setError(null)}
      />

      {!editing ? (
        <ProviderOverview
          enabled={Boolean(query.data?.enabled)}
          serverItems={serverItems}
          summaries={providerSummaries}
          metrics={providerMetrics}
          onCreate={startEdit}
        />
      ) : (
        <ProviderEditorView
          drafts={drafts}
          summaries={draftSummaries}
          metrics={draftMetrics}
          enabled={enabledDraft}
          source={query.data?.source}
          serverItems={serverItems}
          proxyNames={proxyOptions.map((item) => item.name)}
          saving={updateMut.isPending}
          onToggle={setEnabledDraft}
          onAddDraft={addDraft}
          updateDraft={updateDraft}
          updateModel={updateModel}
          deleteDraft={deleteDraft}
          onDiscard={discard}
          onSave={save}
        />
      )}
    </section>
  );
}

function VideoStatsRow({
  enabled,
  source,
  providerCount,
  enabledCount,
  usableCount,
  totalConcurrency,
  coveredActions,
}: {
  enabled: boolean;
  source: string | undefined;
  providerCount: number;
  enabledCount: number;
  usableCount: number;
  totalConcurrency: number;
  coveredActions: Set<VideoAction>;
}) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <VideoStatCard
        label="上线状态"
        value={enabled ? "已开启" : "已关闭"}
        sub={
          <span className="inline-flex items-center gap-1 text-[var(--fg-2)]">
            <Power className="h-3 w-3" />
            {sourceLabel(source)}
          </span>
        }
        accent={enabled ? "green" : undefined}
      />
      <VideoStatCard
        label="供应商"
        value={`${usableCount} / ${providerCount}`}
        sub={<span className="text-[var(--fg-2)]">{enabledCount} 个启用</span>}
        accent={usableCount > 0 ? "green" : "amber"}
      />
      <VideoStatCard
        label="动作覆盖"
        value={`${coveredActions.size} / ${VIDEO_ACTIONS.length}`}
        sub={
          <span className="text-[var(--fg-2)]">
            {actionCoverageLabel(coveredActions)} · 并发 {totalConcurrency}
          </span>
        }
        accent={
          coveredActions.size === VIDEO_ACTIONS.length ? "green" : "amber"
        }
      />
    </div>
  );
}

function VideoStatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: React.ReactNode;
  accent?: "green" | "amber";
}) {
  const ring =
    accent === "green"
      ? "border-success-border"
      : accent === "amber"
        ? "border-[var(--color-lumen-amber)]/20"
        : "border-[var(--border)]";

  return (
    <div
      className={`rounded-[var(--radius-panel)] border bg-[var(--bg-1)]/60 px-4 py-3 backdrop-blur-sm ${ring}`}
    >
      <div className="mb-1 text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
        {label}
      </div>
      <div className="text-lg font-semibold leading-tight text-[var(--fg-0)] tabular-nums">
        {value}
      </div>
      {sub && <div className="mt-1 truncate text-[11px]">{sub}</div>}
    </div>
  );
}

function ReadinessNotice({
  enabled,
  usableCount,
  coveredActions,
  providerIssues,
}: {
  enabled: boolean;
  usableCount: number;
  coveredActions: Set<VideoAction>;
  providerIssues: Issue[];
}) {
  const topIssues = providerIssues.slice(0, 3);
  const ready =
    enabled &&
    usableCount > 0 &&
    coveredActions.size === VIDEO_ACTIONS.length &&
    providerIssues.length === 0;
  if (ready) return null;

  const title = enabled ? "视频供应商需要处理" : "视频生成未开启";
  const detail = !enabled
    ? "打开编辑后启用总开关，再确认至少一个供应商可用。"
    : usableCount === 0
      ? "至少需要一个启用、已保存 Key 且有模型映射的供应商。"
      : coveredActions.size < VIDEO_ACTIONS.length
        ? `当前只覆盖 ${actionCoverageLabel(coveredActions)}。`
        : "部分供应商存在配置提示。";

  return (
    <div className="rounded-[var(--radius-panel)] border border-warning-border bg-warning-soft px-4 py-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-2">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div className="min-w-0">
            <p className="text-sm font-medium text-[var(--fg-0)]">{title}</p>
            <p className="mt-1 type-caption text-warning">{detail}</p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <StatusPill
            tone={enabled ? "success" : "warning"}
            label={enabled ? "总开关已开" : "总开关关闭"}
          />
          <StatusPill
            tone={usableCount > 0 ? "success" : "warning"}
            label={`${usableCount} 个可用`}
          />
          <StatusPill
            tone={
              coveredActions.size === VIDEO_ACTIONS.length
                ? "success"
                : "warning"
            }
            label={`${coveredActions.size}/${VIDEO_ACTIONS.length} 动作`}
          />
        </div>
      </div>
      {topIssues.length > 0 && (
        <IssueList className="mt-3" issues={topIssues} />
      )}
    </div>
  );
}

function ProviderCard({
  item,
  summary,
}: {
  item: VideoProviderItemOut;
  summary: ProviderSummary | undefined;
}) {
  const issues = summary?.issues ?? [];
  const models = summary?.modelNames ?? modelNamesFromModels(item.models);
  return (
    <article
      className={
        "rounded-[var(--radius-dialog)] border p-5 shadow-[var(--shadow-1)] backdrop-blur-sm transition-colors " +
        (item.enabled
          ? "border-[var(--border)] bg-[var(--bg-1)]/60"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/30")
      }
    >
      <ProviderCardHeader item={item} issues={issues} />
      <ProviderCardTags models={models} summary={summary} />

      {issues.length > 0 && <IssueList className="mt-4" issues={issues} />}
      <ProviderCardMeta item={item} />
    </article>
  );
}

function ProviderCardHeader({
  item,
  issues,
}: {
  item: VideoProviderItemOut;
  issues: Issue[];
}) {
  const assetReady = item.asset_management_ready;
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`text-sm font-medium ${
              item.enabled ? "text-[var(--fg-0)]" : "text-[var(--fg-1)]"
            }`}
          >
            {item.name}
          </span>
          <StatusPill
            tone={issueTone(issues)}
            label={item.enabled ? "启用" : "停用"}
          />
          <StatusPill tone="neutral" label={KIND_LABELS[item.kind]} />
          {item.kind === "volcano" && (
            <StatusPill
              tone={assetReady ? "success" : "warning"}
              label={assetReady ? "资产管理已就绪" : "资产管理未就绪"}
            />
          )}
        </div>
        <code className="mt-1 block break-all text-xs text-[var(--fg-2)]">
          {item.base_url}
        </code>
      </div>
    </div>
  );
}

function ProviderCardTags({
  models,
  summary,
}: {
  models: string[];
  summary: ProviderSummary | undefined;
}) {
  const visibleModels = models.slice(0, 6);
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5">
      {VIDEO_ACTIONS.map((action) => (
        <StatusPill
          key={action}
          tone={summary?.capabilities.has(action) ? "success" : "neutral"}
          label={ACTION_LABELS[action]}
        />
      ))}
      {visibleModels.map((model) => (
        <span
          key={model}
          className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-2 py-1 font-mono text-[11px] text-[var(--fg-1)]"
        >
          {model}
        </span>
      ))}
      {models.length > visibleModels.length && (
        <span className="rounded-[var(--radius-card)] border border-[var(--border)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
          +{models.length - visibleModels.length}
        </span>
      )}
      {models.length === 0 && (
        <span className="rounded-[var(--radius-card)] border border-warning-border bg-warning-soft px-2 py-1 text-[11px] text-warning">
          未配置模型
        </span>
      )}
    </div>
  );
}

function VolcanoProviderMeta({ item }: { item: VideoProviderItemOut }) {
  return (
    <>
      <MetaSep />
      <ProviderMetaItem
        label="Access Key ID"
        value={item.access_key_id_hint || "未保存"}
        mono
        color={item.access_key_id_hint ? undefined : "text-warning"}
      />
      <MetaSep />
      <ProviderMetaItem
        label="Secret Access Key"
        value={item.secret_access_key_hint || "未保存"}
        mono
        color={item.secret_access_key_hint ? undefined : "text-warning"}
      />
      <MetaSep />
      <ProviderMetaItem
        label="ProjectName"
        value={item.project_name || VOLCANO_DEFAULT_PROJECT_NAME}
        mono
      />
      <MetaSep />
      <ProviderMetaItem
        label="Region"
        value={item.region || VOLCANO_DEFAULT_REGION}
        mono
      />
    </>
  );
}

function ProviderCardMeta({ item }: { item: VideoProviderItemOut }) {
  return (
    <div
      className={`mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs ${
        item.enabled ? "text-[var(--fg-1)]" : "text-[var(--fg-2)]"
      }`}
    >
      <ProviderMetaItem
        label="密钥"
        value={item.api_key_hint || "未保存"}
        mono
        color={item.api_key_hint ? undefined : "text-danger"}
      />
      {item.kind === "volcano" && <VolcanoProviderMeta item={item} />}
      <MetaSep />
      <ProviderMetaItem label="优先级" value={String(item.priority)} mono />
      <MetaSep />
      <ProviderMetaItem label="权重" value={String(item.weight)} mono />
      <MetaSep />
      <ProviderMetaItem label="并发" value={String(item.concurrency)} mono />
      <ProviderMetaItem
        label="幂等提交"
        value={item.supports_idempotency ? "已确认" : "未确认"}
      />
      <MetaSep />
      <ProviderMetaItem label="代理" value={item.proxy || "直连"} mono />
    </div>
  );
}

function ProviderMetaItem({
  label,
  value,
  mono,
  color,
}: {
  label: string;
  value: string;
  mono?: boolean;
  color?: string;
}) {
  return (
    <span>
      {label}:{" "}
      <code
        className={`${mono ? "tabular-nums" : ""} ${color ?? "text-[var(--fg-1)]"}`}
      >
        {value}
      </code>
    </span>
  );
}

function MetaSep() {
  return <span className="text-[var(--fg-3)]">·</span>;
}

function ModelSummary({ models }: { models: ModelDraft[] }) {
  const names = models
    .map((model) => model.model.trim())
    .filter((model, idx, arr) => model && arr.indexOf(model) === idx);
  const visible = names.slice(0, 4);

  if (visible.length === 0) {
    return <span className="text-[var(--fg-2)]">暂无模型</span>;
  }

  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5">
      {visible.map((model) => (
        <span
          key={model}
          className="rounded-[var(--radius-card)] border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--fg-1)]"
        >
          {model}
        </span>
      ))}
      {names.length > visible.length && (
        <span className="text-[10px] text-[var(--fg-2)]">
          +{names.length - visible.length}
        </span>
      )}
    </span>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-6 py-10 text-center">
      <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)]">
        <Clapperboard className="h-4 w-4 text-[var(--fg-1)]" />
      </div>
      <p className="mt-3 text-sm font-medium text-[var(--fg-0)]">
        还没有 AI 视频供应商
      </p>
      <p className="mx-auto mt-1 max-w-md type-caption text-[var(--fg-2)]">
        添加供应商后，视频页才能创建可用模型对应的视频任务。
      </p>
      <Button
        className="mt-4"
        variant="primary"
        size="sm"
        onClick={onCreate}
        leftIcon={<Plus className="h-3.5 w-3.5" />}
      >
        添加供应商
      </Button>
    </div>
  );
}

function EditCommandCenter({
  enabled,
  source,
  draftCount,
  errorCount,
  warningCount,
  globalIssue,
  onToggle,
  onAddVolcano,
  onAddVolcanoThirdParty,
  onAddVolcanoNewApi,
  onAddDashscope,
  onAddVeo,
  onAddOmniFlash,
  onAddFake,
}: {
  enabled: boolean;
  source: string | undefined;
  draftCount: number;
  errorCount: number;
  warningCount: number;
  globalIssue: string | null;
  onToggle: (value: boolean) => void;
  onAddVolcano: () => void;
  onAddVolcanoThirdParty: () => void;
  onAddVolcanoNewApi: () => void;
  onAddDashscope: () => void;
  onAddVeo: () => void;
  onAddOmniFlash: () => void;
  onAddFake: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 backdrop-blur-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-[var(--fg-0)]">
              编辑视频供应商
            </p>
            <StatusPill tone="neutral" label={`${draftCount} 个供应商`} />
            <StatusPill
              tone={errorCount > 0 ? "danger" : "neutral"}
              label={`${errorCount} 错误`}
            />
            <StatusPill
              tone={warningCount > 0 ? "warning" : "neutral"}
              label={`${warningCount} 提示`}
            />
          </div>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            当前来源：{sourceLabel(source)}
          </p>
        </div>
        <label className="flex min-h-9 items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-sm text-[var(--fg-0)] lg:min-w-[220px]">
          <span>启用视频生成</span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => onToggle(event.target.checked)}
          />
        </label>
      </div>
      {source === "env" && (
        <div className="mt-3 rounded-[var(--radius-card)] border border-warning-border bg-warning-soft px-3 py-2 type-caption text-warning">
          保存后将写入数据库配置，后续优先读取数据库。
        </div>
      )}
      {globalIssue && (
        <div
          role="alert"
          className="mt-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger"
        >
          {globalIssue}
        </div>
      )}
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-7">
        <PresetButton
          icon={<Zap className="h-4 w-4" />}
          title="火山 Seedance"
          detail="Seedance 2.0 / fast"
          onClick={onAddVolcano}
        />
        <PresetButton
          icon={<Server className="h-4 w-4" />}
          title="火山第三方"
          detail="MOYU / 中转网关"
          onClick={onAddVolcanoThirdParty}
        />
        <PresetButton
          icon={<Server className="h-4 w-4" />}
          title="New API"
          detail="/v1/videos"
          onClick={onAddVolcanoNewApi}
        />
        <PresetButton
          icon={<Clapperboard className="h-4 w-4" />}
          title="HappyHorse"
          detail="DashScope 国际站"
          onClick={onAddDashscope}
        />
        <PresetButton
          icon={<Layers3 className="h-4 w-4" />}
          title="Google Veo"
          detail="Veo 3.1 / fast / lite"
          onClick={onAddVeo}
        />
        <PresetButton
          icon={<Server className="h-4 w-4" />}
          title="Omni Flash"
          detail="/v1/video/create"
          onClick={onAddOmniFlash}
        />
        <PresetButton
          icon={<ShieldCheck className="h-4 w-4" />}
          title="测试供应商"
          detail="本地假任务"
          onClick={onAddFake}
        />
      </div>
    </div>
  );
}

function PresetButton({
  icon,
  title,
  detail,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-14 items-center gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-left transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-accent-border bg-accent-soft text-accent">
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium text-[var(--fg-0)]">
          {title}
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-[var(--fg-2)]">
          {detail}
        </span>
      </span>
    </button>
  );
}

type ProviderEditorProps = {
  draft: Draft;
  summary: ProviderSummary | undefined;
  storedKeyHint: string;
  storedAccessKeyIdHint: string;
  storedSecretAccessKeyHint: string;
  storedAssetManagementReady: boolean;
  storedAssetCredentialsRequireReplacement: boolean;
  proxies: string[];
  onPatch: (patch: Partial<Draft>) => void;
  onDelete: () => void;
  onAddModel: () => void;
  onApplyPreset: () => void;
  onPatchModel: (idx: number, patch: Partial<ModelDraft>) => void;
  onMirrorModel: (idx: number) => void;
  onDeleteModel: (idx: number) => void;
};

function editorStatusLabel(tone: ReturnType<typeof issueTone>): string {
  if (tone === "success") return "可保存";
  if (tone === "danger") return "需修复";
  return "有提示";
}

function providerKindPatch(
  draft: Draft,
  kind: VideoProviderKind,
): Partial<Draft> {
  return presetPatchForKind({ ...draft, kind });
}

function baseUrlDraftPatch(draft: Draft, baseUrl: string): Partial<Draft> {
  const previousInferredRegion = inferVolcanoRegion(draft.base_url);
  const nextInferredRegion = inferVolcanoRegion(baseUrl);
  const followsBaseUrl =
    draft.kind === "volcano" &&
    Boolean(nextInferredRegion) &&
    (!draft.region.trim() ||
      draft.region === previousInferredRegion ||
      (!previousInferredRegion && draft.region === VOLCANO_DEFAULT_REGION));
  return {
    base_url: baseUrl,
    ...(followsBaseUrl && nextInferredRegion
      ? { region: nextInferredRegion }
      : {}),
  };
}

function credentialPlaceholder(
  replacementRequired: boolean,
  storedHint: string,
): string {
  if (replacementRequired) return "重命名后需重填";
  if (storedHint) return `留空保留 ${storedHint}`;
  return "未配置";
}

function assetCredentialSummary({
  replacementRequired,
  hasNew,
  hasCompleteNew,
  hasStored,
  storedReady,
  storedAccessKeyIdHint,
  storedSecretAccessKeyHint,
}: {
  replacementRequired: boolean;
  hasNew: boolean;
  hasCompleteNew: boolean;
  hasStored: boolean;
  storedReady: boolean;
  storedAccessKeyIdHint: string;
  storedSecretAccessKeyHint: string;
}): {
  text: string;
  tone: "success" | "warning" | "danger" | "neutral";
  label: string;
} {
  if (hasNew && !hasCompleteNew) {
    return {
      text: "只填写了一项火山资产凭证，请同时填写 Access Key ID 与 Secret Access Key",
      tone: "danger",
      label: "凭证不完整",
    };
  }
  if (replacementRequired && !hasCompleteNew) {
    return {
      text: "供应商重命名后需重新填写 Access Key ID 与 Secret Access Key",
      tone: "danger",
      label: "保存前需重填",
    };
  }
  if (hasNew) {
    return {
      text: "将成对更新火山资产 Access Key ID 与 Secret Access Key",
      tone: storedReady ? "success" : "warning",
      label: "保存后校验",
    };
  }
  if (hasStored) {
    return {
      text: `留空将保留已保存凭证：${storedAccessKeyIdHint} / ${storedSecretAccessKeyHint}`,
      tone: storedReady ? "success" : "warning",
      label: storedReady ? "已保存配置可用" : "已保存配置未就绪",
    };
  }
  return {
    text: "尚未保存火山资产凭证",
    tone: "neutral",
    label: "未保存资产配置",
  };
}

function providerKeyStatus(draft: Draft, storedKeyHintValue: string): string {
  if (draft.kind === "fake") return "测试供应商不需要 Key";
  if (draft.api_key.trim()) return "将更新为新 Key";
  if (draftWasRenamed(draft)) return "重命名后需重新填写 Key";
  if (storedKeyHintValue) return `保留已保存 Key：${storedKeyHintValue}`;
  return "未保存 Key";
}

function ProviderEditorHeader({
  draft,
  summary,
  issues,
  onDelete,
}: {
  draft: Draft;
  summary: ProviderSummary | undefined;
  issues: Issue[];
  onDelete: () => void;
}) {
  const tone = issueTone(issues);
  return (
    <>
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-[var(--fg-0)]">
              {draft.name.trim() || "未命名供应商"}
            </p>
            <StatusPill tone={tone} label={editorStatusLabel(tone)} />
            <StatusPill tone="neutral" label={KIND_LABELS[draft.kind]} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-[var(--fg-2)]">
            <span>
              {summary
                ? actionCoverageLabel(summary.capabilities)
                : "未配置动作"}
            </span>
            <span className="text-[var(--fg-3)]">·</span>
            <ModelSummary models={draft.models} />
          </div>
        </div>
        <IconButton
          variant="ghost"
          size="sm"
          aria-label="删除供应商"
          onClick={onDelete}
        >
          <Trash2 className="h-4 w-4" />
        </IconButton>
      </div>
      {issues.length > 0 && <IssueList className="mt-4" issues={issues} />}
    </>
  );
}

function ProviderConnectionEditor({
  draft,
  storedKeyHintValue,
  onPatch,
}: {
  draft: Draft;
  storedKeyHintValue: string;
  onPatch: ProviderEditorProps["onPatch"];
}) {
  return (
    <>
      <SectionTitle icon={<Server className="h-4 w-4" />} title="基础连接" />
      <div className="grid gap-3 md:grid-cols-2">
        <Field
          label="名称"
          value={draft.name}
          onChange={(name) => onPatch({ name })}
        />
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">类型</span>
          <select
            value={draft.kind}
            onChange={(event) =>
              onPatch(
                providerKindPatch(
                  draft,
                  event.target.value as VideoProviderKind,
                ),
              )
            }
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          >
            <option value="volcano">火山方舟</option>
            <option value="volcano_third_party">火山第三方 / MOYU</option>
            <option value="volcano_newapi">火山 New API / /v1/videos</option>
            <option value="dashscope">DashScope / HappyHorse</option>
            <option value="veo">Google Veo</option>
            <option value="omni_flash">Google Omni Flash / 第三方</option>
            <option value="fake">测试</option>
          </select>
        </label>
        <Field
          label="Base URL"
          value={draft.base_url}
          onChange={(baseUrl) => onPatch(baseUrlDraftPatch(draft, baseUrl))}
        />
        <Field
          label="API Key"
          value={draft.api_key}
          onChange={(api_key) => onPatch({ api_key })}
          name={`video-provider-${draft._key}-api-key`}
          autoComplete="new-password"
          placeholder={
            storedKeyHintValue ? `留空保留 ${storedKeyHintValue}` : "必填"
          }
          type="password"
        />
      </div>
    </>
  );
}

function VolcanoAssetConfigEditor({
  draft,
  storedAccessKeyIdHint,
  storedSecretAccessKeyHint,
  storedAssetManagementReady,
  storedAssetCredentialsRequireReplacement,
  onPatch,
}: Pick<
  ProviderEditorProps,
  | "draft"
  | "storedAccessKeyIdHint"
  | "storedSecretAccessKeyHint"
  | "storedAssetManagementReady"
  | "storedAssetCredentialsRequireReplacement"
  | "onPatch"
>) {
  const hasNew = Boolean(
    draft.access_key_id.trim() || draft.secret_access_key.trim(),
  );
  const hasCompleteNew = Boolean(
    draft.access_key_id.trim() && draft.secret_access_key.trim(),
  );
  const hasStored = Boolean(storedAccessKeyIdHint && storedSecretAccessKeyHint);
  const summary = assetCredentialSummary({
    replacementRequired: storedAssetCredentialsRequireReplacement,
    hasNew,
    hasCompleteNew,
    hasStored,
    storedReady: storedAssetManagementReady,
    storedAccessKeyIdHint,
    storedSecretAccessKeyHint,
  });
  return (
    <div className="space-y-3 border-t border-[var(--border-subtle)] pt-4">
      <SectionTitle
        icon={<KeyRound className="h-4 w-4" />}
        title="火山资产管理"
      />
      <div className="grid gap-3 md:grid-cols-2">
        <Field
          label="Access Key ID"
          value={draft.access_key_id}
          onChange={(access_key_id) => onPatch({ access_key_id })}
          name={`video-provider-${draft._key}-access-key-id`}
          autoComplete="new-password"
          placeholder={credentialPlaceholder(
            storedAssetCredentialsRequireReplacement,
            storedAccessKeyIdHint,
          )}
          type="password"
        />
        <Field
          label="Secret Access Key"
          value={draft.secret_access_key}
          onChange={(secret_access_key) => onPatch({ secret_access_key })}
          name={`video-provider-${draft._key}-secret-access-key`}
          autoComplete="new-password"
          placeholder={credentialPlaceholder(
            storedAssetCredentialsRequireReplacement,
            storedSecretAccessKeyHint,
          )}
          type="password"
        />
        <Field
          label="ProjectName"
          value={draft.project_name}
          onChange={(project_name) => onPatch({ project_name })}
          placeholder={VOLCANO_DEFAULT_PROJECT_NAME}
        />
        <Field
          label="Region"
          value={draft.region}
          onChange={(region) => onPatch({ region })}
          placeholder={VOLCANO_DEFAULT_REGION}
        />
      </div>
      <div
        className={`flex flex-col gap-2 rounded-[var(--radius-card)] border px-3 py-2.5 type-caption sm:flex-row sm:items-center sm:justify-between ${
          summary.tone === "danger" || summary.tone === "warning"
            ? "border-warning-border bg-warning-soft text-warning"
            : "border-[var(--border-subtle)] bg-[var(--bg-0)] text-[var(--fg-2)]"
        }`}
      >
        <span>{summary.text}</span>
        <StatusPill tone={summary.tone} label={summary.label} />
      </div>
    </div>
  );
}

function ProviderRoutingEditor({
  draft,
  proxies,
  onPatch,
}: Pick<ProviderEditorProps, "draft" | "proxies" | "onPatch">) {
  return (
    <>
      <SectionTitle icon={<Gauge className="h-4 w-4" />} title="路由容量" />
      <div className="grid gap-3 md:grid-cols-4">
        <Field
          label="优先级"
          value={String(draft.priority)}
          onChange={(value) => onPatch({ priority: Number(value) || 0 })}
          type="number"
        />
        <Field
          label="权重"
          value={String(draft.weight)}
          onChange={(value) => onPatch({ weight: Number(value) || 1 })}
          type="number"
        />
        <Field
          label="并发"
          value={String(draft.concurrency)}
          onChange={(value) => onPatch({ concurrency: Number(value) || 1 })}
          type="number"
        />
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">代理</span>
          <select
            value={draft.proxy}
            onChange={(event) => onPatch({ proxy: event.target.value })}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          >
            <option value="">直连</option>
            {proxies.map((proxy) => (
              <option key={proxy} value={proxy}>
                {proxy}
              </option>
            ))}
          </select>
        </label>
      </div>
    </>
  );
}

function ProviderStateEditor({
  draft,
  summary,
  storedKeyHintValue,
  onPatch,
}: {
  draft: Draft;
  summary: ProviderSummary | undefined;
  storedKeyHintValue: string;
  onPatch: ProviderEditorProps["onPatch"];
}) {
  return (
    <div className="space-y-3">
      <label className="flex items-center justify-between gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-3 text-sm text-[var(--fg-0)]">
        <span>
          <span className="block">启用此供应商</span>
          {draft.kind === "veo" && (
            <span className="mt-0.5 block text-[11px] text-warning">
              Veo 适配器尚未接入 Worker
            </span>
          )}
        </span>
        <input
          type="checkbox"
          checked={normalizeVideoProviderEnabled(draft.kind, draft.enabled)}
          disabled={!videoProviderKindCanBeEnabled(draft.kind)}
          onChange={(event) =>
            onPatch({
              enabled: normalizeVideoProviderEnabled(
                draft.kind,
                event.target.checked,
              ),
            })
          }
        />
      </label>
      <label className="flex items-center justify-between gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-3 text-sm text-[var(--fg-0)]">
        <span>确认支持幂等提交</span>
        <input
          type="checkbox"
          checked={draft.supports_idempotency}
          onChange={(event) =>
            onPatch({ supports_idempotency: event.target.checked })
          }
        />
      </label>
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-3">
        <div className="flex items-center gap-2 text-xs font-medium text-[var(--fg-0)]">
          <KeyRound className="h-4 w-4 text-[var(--fg-2)]" />
          Key 状态
        </div>
        <p className="mt-2 type-caption text-[var(--fg-2)]">
          {providerKeyStatus(draft, storedKeyHintValue)}
        </p>
      </div>
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-3">
        <div className="flex items-center gap-2 text-xs font-medium text-[var(--fg-0)]">
          <Layers3 className="h-4 w-4 text-[var(--fg-2)]" />
          动作覆盖
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {VIDEO_ACTIONS.map((action) => (
            <StatusPill
              key={action}
              tone={summary?.capabilities.has(action) ? "success" : "neutral"}
              label={ACTION_LABELS[action]}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function ProviderModelsEditor({
  draft,
  onAddModel,
  onApplyPreset,
  onPatchModel,
  onMirrorModel,
  onDeleteModel,
}: Pick<
  ProviderEditorProps,
  | "draft"
  | "onAddModel"
  | "onApplyPreset"
  | "onPatchModel"
  | "onMirrorModel"
  | "onDeleteModel"
>) {
  return (
    <div className="mt-5 space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <SectionTitle icon={<Layers3 className="h-4 w-4" />} title="模型能力" />
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onApplyPreset}
            leftIcon={<Check className="h-3.5 w-3.5" />}
          >
            套用当前类型预设
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onAddModel}
            leftIcon={<Plus className="h-3.5 w-3.5" />}
          >
            添加模型
          </Button>
        </div>
      </div>
      <div className="space-y-2">
        {draft.models.map((model, idx) => (
          <div
            key={model._key}
            className="grid gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3 md:grid-cols-[minmax(130px,0.9fr)_1fr_1fr_1fr_auto]"
          >
            <Field
              label="业务模型"
              value={model.model}
              onChange={(value) => onPatchModel(idx, { model: value })}
            />
            <Field
              label="文字生成"
              value={model.t2v}
              onChange={(value) => onPatchModel(idx, { t2v: value })}
            />
            <Field
              label="首帧生成"
              value={model.i2v}
              onChange={(value) => onPatchModel(idx, { i2v: value })}
            />
            <Field
              label="参考生成"
              value={model.reference}
              onChange={(value) => onPatchModel(idx, { reference: value })}
            />
            <div className="flex items-end gap-1">
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="同步模型映射"
                tooltip="同步模型映射"
                onClick={() => onMirrorModel(idx)}
              >
                <Zap className="h-4 w-4" />
              </IconButton>
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="删除模型映射"
                onClick={() => onDeleteModel(idx)}
              >
                <Trash2 className="h-4 w-4" />
              </IconButton>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProviderEditor({
  draft,
  summary,
  storedKeyHint,
  storedAccessKeyIdHint,
  storedSecretAccessKeyHint,
  storedAssetManagementReady,
  storedAssetCredentialsRequireReplacement,
  proxies,
  onPatch,
  onDelete,
  onAddModel,
  onApplyPreset,
  onPatchModel,
  onMirrorModel,
  onDeleteModel,
}: ProviderEditorProps) {
  const issues = summary?.issues ?? [];
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]">
      <ProviderEditorHeader
        draft={draft}
        summary={summary}
        issues={issues}
        onDelete={onDelete}
      />

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.55fr)]">
        <div className="space-y-4">
          <ProviderConnectionEditor
            draft={draft}
            storedKeyHintValue={storedKeyHint}
            onPatch={onPatch}
          />

          {draft.kind === "volcano" && (
            <VolcanoAssetConfigEditor
              draft={draft}
              storedAccessKeyIdHint={storedAccessKeyIdHint}
              storedSecretAccessKeyHint={storedSecretAccessKeyHint}
              storedAssetManagementReady={storedAssetManagementReady}
              storedAssetCredentialsRequireReplacement={
                storedAssetCredentialsRequireReplacement
              }
              onPatch={onPatch}
            />
          )}

          <ProviderRoutingEditor
            draft={draft}
            proxies={proxies}
            onPatch={onPatch}
          />
        </div>

        <ProviderStateEditor
          draft={draft}
          summary={summary}
          storedKeyHintValue={storedKeyHint}
          onPatch={onPatch}
        />
      </div>

      <ProviderModelsEditor
        draft={draft}
        onAddModel={onAddModel}
        onApplyPreset={onApplyPreset}
        onPatchModel={onPatchModel}
        onMirrorModel={onMirrorModel}
        onDeleteModel={onDeleteModel}
      />
    </div>
  );
}

function SectionTitle({
  icon,
  title,
}: {
  icon: React.ReactNode;
  title: string;
}) {
  return (
    <div className="flex items-center gap-2 text-xs font-medium text-[var(--fg-0)]">
      <span className="text-[var(--fg-2)]">{icon}</span>
      {title}
    </div>
  );
}

function IssueList({
  issues,
  className = "",
}: {
  issues: Issue[];
  className?: string;
}) {
  return (
    <div className={`space-y-1.5 ${className}`}>
      {issues.map((issue, idx) => (
        <div
          key={`${issue.message}-${idx}`}
          className={`flex items-start gap-2 rounded-[var(--radius-card)] border px-3 py-2 type-caption ${
            issue.severity === "error"
              ? "border-danger-border bg-danger-soft text-danger"
              : "border-warning-border bg-warning-soft text-warning"
          }`}
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{issue.message}</span>
        </div>
      ))}
    </div>
  );
}

function StatusPill({
  tone,
  label,
}: {
  tone: "success" | "warning" | "danger" | "neutral";
  label: string;
}) {
  const className =
    tone === "success"
      ? "border-success-border bg-success-soft text-success"
      : tone === "warning"
        ? "border-warning-border bg-warning-soft text-warning"
        : tone === "danger"
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]";
  return (
    <span
      className={`inline-flex items-center rounded-[var(--radius-control)] border px-2 py-1 text-[11px] font-medium ${className}`}
    >
      {label}
    </span>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  name,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
  name?: string;
  autoComplete?: string;
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <input
        type={type}
        value={value}
        name={name}
        autoComplete={autoComplete}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--accent)]/50"
      />
    </label>
  );
}
