import {
  Check,
  Gauge,
  KeyRound,
  Layers3,
  Plus,
  Server,
  Trash2,
  Zap,
} from "lucide-react";

import { Button, IconButton } from "@/components/ui/primitives";
import type { VideoProviderKind } from "@/lib/types";

import {
  ACTION_LABELS,
  KIND_LABELS,
  VIDEO_ACTIONS,
  VOLCANO_DEFAULT_PROJECT_NAME,
  VOLCANO_DEFAULT_REGION,
  actionCoverageLabel,
  issueTone,
  normalizeVideoProviderEnabled,
  videoProviderKindCanBeEnabled,
  type Draft,
  type ModelDraft,
  type ProviderSummary,
} from "./domain";
import {
  assetCredentialSummary,
  baseUrlDraftPatch,
  credentialPlaceholder,
  editorStatusLabel,
  formIssues,
  providerKeyStatus,
  providerKindPatch,
} from "./providerFormModel";
import { Field, IssueList, SectionTitle, StatusPill } from "./shared";

export type ProviderEditorProps = {
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
  onPatchModel: (index: number, patch: Partial<ModelDraft>) => void;
  onMirrorModel: (index: number) => void;
  onDeleteModel: (index: number) => void;
};

function ModelSummary({ models }: { models: ModelDraft[] }) {
  const names = models
    .map((model) => model.model.trim())
    .filter((model, index, candidates) => {
      return Boolean(model) && candidates.indexOf(model) === index;
    });
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

function ProviderEditorHeader({
  draft,
  summary,
  onDelete,
}: {
  draft: Draft;
  summary: ProviderSummary | undefined;
  onDelete: () => void;
}) {
  const issues = formIssues(summary?.issues);
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
  storedKeyHint,
  onPatch,
}: {
  draft: Draft;
  storedKeyHint: string;
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
          placeholder={storedKeyHint ? `留空保留 ${storedKeyHint}` : "必填"}
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
  storedKeyHint,
  onPatch,
}: {
  draft: Draft;
  summary: ProviderSummary | undefined;
  storedKeyHint: string;
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
          {providerKeyStatus(draft, storedKeyHint)}
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
        {draft.models.map((model, index) => (
          <div
            key={model._key}
            className="grid gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3 md:grid-cols-[minmax(130px,0.9fr)_1fr_1fr_1fr_auto]"
          >
            <Field
              label="业务模型"
              value={model.model}
              onChange={(value) => onPatchModel(index, { model: value })}
            />
            <Field
              label="文字生成"
              value={model.t2v}
              onChange={(value) => onPatchModel(index, { t2v: value })}
            />
            <Field
              label="首帧生成"
              value={model.i2v}
              onChange={(value) => onPatchModel(index, { i2v: value })}
            />
            <Field
              label="参考生成"
              value={model.reference}
              onChange={(value) => onPatchModel(index, { reference: value })}
            />
            <div className="flex items-end gap-1">
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="同步模型映射"
                tooltip="同步模型映射"
                onClick={() => onMirrorModel(index)}
              >
                <Zap className="h-4 w-4" />
              </IconButton>
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="删除模型映射"
                onClick={() => onDeleteModel(index)}
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

export function ProviderEditor({
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
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]">
      <ProviderEditorHeader
        draft={draft}
        summary={summary}
        onDelete={onDelete}
      />

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.55fr)]">
        <div className="space-y-4">
          <ProviderConnectionEditor
            draft={draft}
            storedKeyHint={storedKeyHint}
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
          storedKeyHint={storedKeyHint}
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
