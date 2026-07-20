import type { ReactNode } from "react";
import {
  AlertCircle,
  Check,
  Clapperboard,
  Pencil,
  Plus,
  Power,
  X,
} from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import type { VideoProviderItemOut } from "@/lib/types";

import {
  ACTION_LABELS,
  KIND_LABELS,
  VIDEO_ACTIONS,
  VOLCANO_DEFAULT_PROJECT_NAME,
  VOLCANO_DEFAULT_REGION,
  actionCoverageLabel,
  issueTone,
  modelNamesFromModels,
  sourceLabel,
  type Issue,
  type ProviderSummary,
  type VideoAction,
} from "./domain";
import type { ProviderPanelMetrics } from "./metrics";
import { IssueList, MetaSep, StatusPill } from "./shared";

export function ProviderPanelLoadingState() {
  return (
    <section className="space-y-4" aria-busy="true">
      <div className="h-28 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
      <div className="h-36 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
      <div className="h-56 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
    </section>
  );
}

export function ProviderPanelHeader({
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

export function ProviderPanelFeedback({
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

export function ProviderOverview({
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
  sub?: ReactNode;
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
