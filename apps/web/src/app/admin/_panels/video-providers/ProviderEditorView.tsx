import type { ReactNode } from "react";
import {
  Clapperboard,
  Layers3,
  Save,
  Server,
  ShieldCheck,
  Zap,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import type { VideoProviderItemOut } from "@/lib/types";

import {
  emptyDashScopeDraft,
  emptyFakeDraft,
  emptyModelDraft,
  emptyOmniFlashDraft,
  emptyVeoDraft,
  emptyVolcanoDraft,
  emptyVolcanoNewApiDraft,
  emptyVolcanoThirdPartyDraft,
  mirroredModelPatch,
  presetPatchForKind,
  sourceLabel,
  storedDraftHints,
  type Draft,
  type ModelDraft,
  type ProviderSummary,
} from "./domain";
import type { DraftPanelMetrics } from "./metrics";
import { ProviderEditor } from "./ProviderForm";
import { StatusPill } from "./shared";

export type ProviderEditorViewProps = {
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
};

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
  updateDraft: ProviderEditorViewProps["updateDraft"];
  updateModel: ProviderEditorViewProps["updateModel"];
  deleteDraft: ProviderEditorViewProps["deleteDraft"];
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

export function ProviderEditorView({
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
}: ProviderEditorViewProps) {
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
      <div className="fixed bottom-0 left-0 right-0 z-40 max-w-full px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:bottom-4 sm:left-1/2 sm:right-auto sm:w-auto sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:px-0 sm:pb-4">
        <div className="grid grid-cols-2 items-stretch gap-2 rounded-[var(--radius-dialog)] border border-[var(--color-lumen-amber)]/40 bg-[var(--bg-1)]/95 px-3 py-2.5 shadow-[var(--shadow-3)] backdrop-blur-xl sm:flex sm:items-center sm:gap-3 sm:px-4">
          <span className="col-span-2 min-w-0 type-caption text-[var(--fg-1)] sm:col-span-1">
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
          <div className="hidden flex-1 sm:block sm:flex-none" />
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
  icon: ReactNode;
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
