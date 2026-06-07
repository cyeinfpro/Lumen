"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
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
import { ApiError } from "@/lib/apiClient";
import type {
  VideoProviderItemIn,
  VideoProviderItemOut,
  VideoProviderKind,
} from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { ErrorBlock } from "../page";

type VideoAction = "t2v" | "i2v" | "reference";

type ModelDraft = {
  _key: number;
  model: string;
  t2v: string;
  i2v: string;
  reference: string;
};

type Draft = {
  _key: number;
  original_name?: string;
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  proxy: string;
  models: ModelDraft[];
};

type IssueSeverity = "error" | "warning";

type Issue = {
  severity: IssueSeverity;
  message: string;
};

type ProviderSummary = {
  name: string;
  kind: VideoProviderKind;
  enabled: boolean;
  hasKey: boolean;
  capabilities: Set<VideoAction>;
  modelNames: string[];
  concurrency: number;
  issues: Issue[];
};

type CapabilityRow = {
  model: string;
  t2v: string[];
  i2v: string[];
  reference: string[];
};

const VOLCANO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3";
const VOLCANO_THIRD_PARTY_BASE_URL = "https://www.moyu.info";
const DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com";
const VEO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta";
const VOLCANO_MODEL_PRESETS = [
  {
    model: "seedance-2.0",
    upstream: "doubao-seedance-2-0-260128",
  },
  {
    model: "seedance-2.0-fast",
    upstream: "doubao-seedance-2-0-fast-260128",
  },
] as const;
const VEO_MODEL_PRESETS = [
  {
    model: "veo-3.1",
    upstream: "veo-3.1-generate-preview",
    reference: true,
  },
  {
    model: "veo-3.1-fast",
    upstream: "veo-3.1-fast-generate-preview",
    reference: true,
  },
  {
    model: "veo-3.1-lite",
    upstream: "veo-3.1-lite-generate-preview",
    reference: false,
  },
] as const;
const HAPPYHORSE_MODEL = "happyhorse-1.0";
const TEST_VIDEO_MODEL = "test-video";
const VIDEO_ACTIONS: VideoAction[] = ["t2v", "i2v", "reference"];

const ACTION_LABELS: Record<VideoAction, string> = {
  t2v: "文字生成",
  i2v: "首帧生成",
  reference: "参考生成",
};

const KIND_LABELS: Record<VideoProviderKind, string> = {
  volcano: "火山方舟",
  volcano_third_party: "火山第三方",
  dashscope: "DashScope",
  veo: "Google Veo",
  fake: "测试",
};

let seq = 0;
function nextKey() {
  seq += 1;
  return seq;
}

function modelDraft(
  model = "",
  t2v = "",
  i2v = "",
  reference = "",
): ModelDraft {
  return {
    _key: nextKey(),
    model,
    t2v,
    i2v,
    reference,
  };
}

function volcanoModelDrafts(): ModelDraft[] {
  return VOLCANO_MODEL_PRESETS.map((preset) =>
    modelDraft(preset.model, preset.upstream, preset.upstream, preset.upstream),
  );
}

function happyHorseModelDrafts(): ModelDraft[] {
  return [
    modelDraft(
      HAPPYHORSE_MODEL,
      "happyhorse-1.0-t2v",
      "happyhorse-1.0-i2v",
      "happyhorse-1.0-r2v",
    ),
  ];
}

function veoModelDrafts(): ModelDraft[] {
  return VEO_MODEL_PRESETS.map((preset) =>
    modelDraft(
      preset.model,
      preset.upstream,
      preset.upstream,
      preset.reference ? preset.upstream : "",
    ),
  );
}

function fakeModelDrafts(): ModelDraft[] {
  return [modelDraft(TEST_VIDEO_MODEL, TEST_VIDEO_MODEL, TEST_VIDEO_MODEL, TEST_VIDEO_MODEL)];
}

function emptyModelDraft(): ModelDraft {
  return modelDraft();
}

function actionFromModelKey(key: string): VideoAction | null {
  const trimmed = key.trim();
  if (!trimmed.includes(":")) return null;
  const action = trimmed.split(/:(?=[^:]+$)/)[1];
  return VIDEO_ACTIONS.includes(action as VideoAction)
    ? (action as VideoAction)
    : null;
}

function baseModelName(key: string): string {
  const trimmed = key.trim();
  if (!trimmed.includes(":")) return trimmed;
  return trimmed.split(/:(?=[^:]+$)/)[0]?.trim() || trimmed;
}

function modelsToRows(models: Record<string, string>): ModelDraft[] {
  const rows = new Map<string, ModelDraft>();
  const rowFor = (model: string) => {
    const existing = rows.get(model);
    if (existing) return existing;
    const next = {
      _key: nextKey(),
      model,
      t2v: "",
      i2v: "",
      reference: "",
    };
    rows.set(model, next);
    return next;
  };
  for (const [key, value] of Object.entries(models)) {
    const trimmedKey = key.trim();
    const trimmedValue = value.trim();
    if (!trimmedKey || !trimmedValue) continue;
    const action = actionFromModelKey(trimmedKey);
    const model = action ? baseModelName(trimmedKey) : trimmedKey;
    const row = rowFor(model);
    if (action === "t2v") row.t2v = trimmedValue;
    else if (action === "i2v") row.i2v = trimmedValue;
    else if (action === "reference") row.reference = trimmedValue;
    else {
      row.t2v = trimmedValue;
      row.i2v = trimmedValue;
      row.reference = trimmedValue;
    }
  }
  const out = Array.from(rows.values());
  return out.length > 0 ? out : [emptyModelDraft()];
}

function rowsToModels(rows: ModelDraft[]): Record<string, string> {
  const models: Record<string, string> = {};
  for (const row of rows) {
    const model = row.model.trim();
    if (!model) continue;
    if (row.t2v.trim()) models[`${model}:t2v`] = row.t2v.trim();
    if (row.i2v.trim()) models[`${model}:i2v`] = row.i2v.trim();
    if (row.reference.trim()) models[`${model}:reference`] = row.reference.trim();
  }
  return models;
}

function modelNamesFromModels(models: Record<string, string>): string[] {
  const names = new Set<string>();
  for (const [key, value] of Object.entries(models)) {
    if (!value.trim()) continue;
    const name = baseModelName(key);
    if (name) names.add(name);
  }
  return Array.from(names).sort((a, b) => a.localeCompare(b));
}

function capabilitiesFromModels(models: Record<string, string>): Set<VideoAction> {
  const capabilities = new Set<VideoAction>();
  for (const [key, value] of Object.entries(models)) {
    if (!value.trim()) continue;
    const action = actionFromModelKey(key);
    if (action) {
      capabilities.add(action);
    } else {
      VIDEO_ACTIONS.forEach((item) => capabilities.add(item));
    }
  }
  return capabilities;
}

function toDraft(item: VideoProviderItemOut): Draft {
  return {
    _key: nextKey(),
    original_name: item.name,
    name: item.name,
    kind: item.kind,
    base_url: item.base_url,
    api_key: "",
    enabled: item.enabled,
    priority: item.priority,
    weight: item.weight,
    concurrency: item.concurrency,
    proxy: item.proxy ?? "",
    models: modelsToRows(item.models),
  };
}

function emptyVolcanoDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-main",
    kind: "volcano",
    base_url: VOLCANO_BASE_URL,
    api_key: "",
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 10,
    proxy: "",
    models: volcanoModelDrafts(),
  };
}

function emptyVolcanoThirdPartyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-third-party",
    kind: "volcano_third_party",
    base_url: VOLCANO_THIRD_PARTY_BASE_URL,
    api_key: "",
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 10,
    proxy: "",
    models: volcanoModelDrafts(),
  };
}

function emptyDashScopeDraft(): Draft {
  return {
    _key: nextKey(),
    name: "dashscope-happyhorse",
    kind: "dashscope",
    base_url: DASHSCOPE_BASE_URL,
    api_key: "",
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 2,
    proxy: "",
    models: happyHorseModelDrafts(),
  };
}

function emptyVeoDraft(): Draft {
  return {
    _key: nextKey(),
    name: "google-veo",
    kind: "veo",
    base_url: VEO_BASE_URL,
    api_key: "",
    enabled: false,
    priority: 80,
    weight: 1,
    concurrency: 2,
    proxy: "",
    models: veoModelDrafts(),
  };
}

function emptyFakeDraft(): Draft {
  return {
    _key: nextKey(),
    name: "video-test",
    kind: "fake",
    base_url: "http://localhost",
    api_key: "",
    enabled: false,
    priority: 10,
    weight: 1,
    concurrency: 1,
    proxy: "",
    models: fakeModelDrafts(),
  };
}

function presetName(draft: Draft, fallback: string): string {
  const name = draft.name.trim();
  if (
    !name ||
    name === "volcano-main" ||
    name === "volcano-third-party" ||
    name === "dashscope-happyhorse" ||
    name === "google-veo" ||
    name === "video-test"
  ) {
    return fallback;
  }
  return name;
}

function volcanoPresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "volcano-main"),
    kind: "volcano",
    base_url: VOLCANO_BASE_URL,
    enabled: draft.enabled,
    priority: draft.priority || 100,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: 10,
    models: volcanoModelDrafts(),
  };
}

function volcanoThirdPartyPresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "volcano-third-party"),
    kind: "volcano_third_party",
    base_url: VOLCANO_THIRD_PARTY_BASE_URL,
    enabled: draft.enabled,
    priority: draft.priority || 100,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: 10,
    models: volcanoModelDrafts(),
  };
}

function dashscopePresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "dashscope-happyhorse"),
    kind: "dashscope",
    base_url: DASHSCOPE_BASE_URL,
    enabled: draft.enabled,
    priority: draft.priority || 100,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: Math.max(1, Number(draft.concurrency) || 2),
    models: happyHorseModelDrafts(),
  };
}

function veoPresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "google-veo"),
    kind: "veo",
    base_url: VEO_BASE_URL,
    enabled: draft.enabled,
    priority: draft.priority || 80,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: Math.max(1, Number(draft.concurrency) || 2),
    models: veoModelDrafts(),
  };
}

function fakePresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "video-test"),
    kind: "fake",
    base_url: "http://localhost",
    enabled: false,
    priority: draft.priority || 10,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: 1,
    models: fakeModelDrafts(),
  };
}

function presetPatchForKind(draft: Draft): Partial<Draft> {
  if (draft.kind === "volcano_third_party") return volcanoThirdPartyPresetPatch(draft);
  if (draft.kind === "dashscope") return dashscopePresetPatch(draft);
  if (draft.kind === "veo") return veoPresetPatch(draft);
  if (draft.kind === "fake") return fakePresetPatch(draft);
  return volcanoPresetPatch(draft);
}

function storedKeyHint(
  serverItems: VideoProviderItemOut[],
  providerName: string,
): string {
  return serverItems.find((item) => item.name === providerName)?.api_key_hint?.trim() ?? "";
}

function hasStoredKey(
  serverItems: VideoProviderItemOut[],
  providerName: string,
): boolean {
  return Boolean(storedKeyHint(serverItems, providerName));
}

function hasDraftKey(draft: Draft, serverItems: VideoProviderItemOut[]): boolean {
  if (draft.kind === "fake") return true;
  if (draft.api_key.trim()) return true;
  const name = draft.name.trim();
  return Boolean(name && draft.original_name === name && hasStoredKey(serverItems, name));
}

function saveError(err: Error): string {
  if (err instanceof ApiError) {
    return err.message || `保存失败 (HTTP ${err.status})`;
  }
  return err.message || "保存失败";
}

function sourceLabel(source: string | undefined): string {
  if (source === "db") return "数据库";
  if (source === "env") return "环境变量";
  if (source === "desktop") return "本机配置";
  return "未配置";
}

function issueTone(issues: Issue[]): "danger" | "warning" | "success" {
  if (issues.some((item) => item.severity === "error")) return "danger";
  if (issues.length > 0) return "warning";
  return "success";
}

function analyzeProvider(
  item: VideoProviderItemOut,
): ProviderSummary {
  const capabilities = capabilitiesFromModels(item.models);
  const modelNames = modelNamesFromModels(item.models);
  const hasKey = item.kind === "fake" || Boolean(item.api_key_hint.trim());
  const issues: Issue[] = [];
  if (item.enabled && !hasKey) {
    issues.push({ severity: "error", message: "启用状态下缺少 API Key" });
  }
  if (item.enabled && modelNames.length === 0) {
    issues.push({ severity: "error", message: "启用状态下缺少模型映射" });
  }
  if (item.enabled && capabilities.size === 0) {
    issues.push({ severity: "error", message: "没有可用动作" });
  }
  if (item.enabled && item.kind === "veo") {
    issues.push({ severity: "warning", message: "Veo 适配器尚未接入 Worker" });
  }
  if (!item.enabled) {
    issues.push({ severity: "warning", message: "供应商已停用" });
  }
  return {
    name: item.name,
    kind: item.kind,
    enabled: item.enabled,
    hasKey,
    capabilities,
    modelNames,
    concurrency: item.concurrency,
    issues,
  };
}

function analyzeDraft(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
  duplicate: boolean,
): ProviderSummary {
  const name = draft.name.trim();
  const models = rowsToModels(draft.models);
  const capabilities = capabilitiesFromModels(models);
  const modelNames = modelNamesFromModels(models);
  const issues: Issue[] = [];
  if (!name) {
    issues.push({ severity: "error", message: "供应商名称不能为空" });
  }
  if (duplicate) {
    issues.push({ severity: "error", message: "供应商名称重复" });
  }
  if (!draft.base_url.trim()) {
    issues.push({ severity: "error", message: "缺少 Base URL" });
  } else {
    try {
      const url = new URL(draft.base_url.trim());
      if (!["http:", "https:"].includes(url.protocol)) {
        issues.push({ severity: "error", message: "Base URL 只能使用 HTTP 或 HTTPS" });
      }
      if (!url.hostname) {
        issues.push({ severity: "error", message: "Base URL 必须包含主机名" });
      }
      if (url.username || url.password) {
        issues.push({ severity: "error", message: "Base URL 不能包含用户名或密码" });
      }
    } catch {
      issues.push({ severity: "error", message: "Base URL 格式不合法" });
    }
  }
  if (draft.enabled && !hasDraftKey(draft, serverItems)) {
    issues.push({ severity: "error", message: "启用状态下必须填写 API Key" });
  }
  if (draft.enabled && modelNames.length === 0) {
    issues.push({ severity: "error", message: "至少需要一个模型映射" });
  }
  if (draft.enabled && capabilities.size === 0) {
    issues.push({ severity: "error", message: "至少需要一个可用动作" });
  }
  if (draft.kind === "veo" && draft.enabled) {
    issues.push({ severity: "warning", message: "Veo 适配器尚未接入 Worker" });
  }
  if (!draft.enabled) {
    issues.push({ severity: "warning", message: "保存后不会参与视频任务路由" });
  }
  return {
    name: name || "未命名",
    kind: draft.kind,
    enabled: draft.enabled,
    hasKey: hasDraftKey(draft, serverItems),
    capabilities,
    modelNames,
    concurrency: Math.max(1, Math.min(32, Number(draft.concurrency) || 1)),
    issues,
  };
}

function analyzeDrafts(
  drafts: Draft[],
  enabled: boolean,
  serverItems: VideoProviderItemOut[],
): ProviderSummary[] {
  const nameCounts = new Map<string, number>();
  for (const draft of drafts) {
    const name = draft.name.trim();
    if (name) nameCounts.set(name, (nameCounts.get(name) ?? 0) + 1);
  }
  return drafts.map((draft) =>
    analyzeDraft(draft, serverItems, (nameCounts.get(draft.name.trim()) ?? 0) > 1),
  ).map((summary) => {
    if (enabled || summary.enabled) return summary;
    return {
      ...summary,
      issues: summary.issues.filter((issue) => issue.severity !== "warning"),
    };
  });
}

function buildCapabilityRows(items: VideoProviderItemOut[]): CapabilityRow[] {
  const rows = new Map<string, CapabilityRow>();
  const rowFor = (model: string) => {
    const existing = rows.get(model);
    if (existing) return existing;
    const next: CapabilityRow = { model, t2v: [], i2v: [], reference: [] };
    rows.set(model, next);
    return next;
  };
  for (const provider of items) {
    if (!provider.enabled) continue;
    for (const [key, value] of Object.entries(provider.models)) {
      if (!value.trim()) continue;
      const model = baseModelName(key);
      const row = rowFor(model);
      const action = actionFromModelKey(key);
      if (action) {
        row[action].push(provider.name);
      } else {
        VIDEO_ACTIONS.forEach((item) => row[item].push(provider.name));
      }
    }
  }
  return Array.from(rows.values()).sort((a, b) => a.model.localeCompare(b.model));
}

function actionCoverageLabel(capabilities: Set<VideoAction>): string {
  if (capabilities.size === 0) return "无动作";
  return VIDEO_ACTIONS.filter((action) => capabilities.has(action))
    .map((action) => ACTION_LABELS[action])
    .join(" / ");
}

export function VideoProvidersPanel() {
  const query = useVideoProvidersQuery();
  const updateMut = useUpdateVideoProvidersMutation();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [enabledDraft, setEnabledDraft] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const serverItems = useMemo(() => query.data?.items ?? [], [query.data?.items]);
  const proxyOptions = useMemo(
    () => query.data?.proxies ?? [],
    [query.data?.proxies],
  );
  const editing = drafts !== null;

  const providerSummaries = useMemo(
    () => serverItems.map(analyzeProvider),
    [serverItems],
  );
  const capabilityRows = useMemo(
    () => buildCapabilityRows(serverItems),
    [serverItems],
  );
  const draftSummaries = useMemo(
    () =>
      drafts
        ? analyzeDrafts(drafts, enabledDraft, serverItems)
        : [],
    [drafts, enabledDraft, serverItems],
  );

  const enabledCount = providerSummaries.filter((item) => item.enabled).length;
  const usableCount = providerSummaries.filter(
    (item) => item.enabled && item.hasKey && item.modelNames.length > 0,
  ).length;
  const totalConcurrency = providerSummaries
    .filter((item) => item.enabled)
    .reduce((sum, item) => sum + item.concurrency, 0);
  const coveredActions = new Set<VideoAction>();
  providerSummaries
    .filter((item) => item.enabled && item.hasKey)
    .forEach((item) => item.capabilities.forEach((action) => coveredActions.add(action)));

  const draftErrorCount = draftSummaries.reduce(
    (sum, summary) =>
      sum + summary.issues.filter((issue) => issue.severity === "error").length,
    0,
  );
  const draftWarningCount = draftSummaries.reduce(
    (sum, summary) =>
      sum + summary.issues.filter((issue) => issue.severity === "warning").length,
    0,
  );
  const draftUsableCount = draftSummaries.filter(
    (summary) =>
      summary.enabled &&
      summary.hasKey &&
      summary.modelNames.length > 0 &&
      !summary.issues.some((issue) => issue.severity === "error"),
  ).length;
  const globalDraftIssue =
    enabledDraft && draftUsableCount === 0
      ? "启用视频生成前至少需要一个启用且可用的供应商"
      : null;

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
      if (!prev) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  };

  const updateModel = (
    providerIdx: number,
    modelIdx: number,
    patch: Partial<ModelDraft>,
  ) => {
    setDrafts((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      const models = [...next[providerIdx].models];
      models[modelIdx] = { ...models[modelIdx], ...patch };
      next[providerIdx] = { ...next[providerIdx], models };
      return next;
    });
  };

  const save = () => {
    if (!drafts) return;
    setError(null);
    if (enabledDraft && drafts.length === 0) {
      setError("开启视频生成前至少添加一个视频供应商");
      return;
    }
    const currentSummaries = analyzeDrafts(drafts, enabledDraft, serverItems);
    const firstError = currentSummaries
      .flatMap((summary) => summary.issues)
      .find((issue) => issue.severity === "error");
    if (firstError) {
      setError(firstError.message);
      return;
    }
    const currentUsableCount = currentSummaries.filter(
      (summary) =>
        summary.enabled &&
        summary.hasKey &&
        summary.modelNames.length > 0 &&
        !summary.issues.some((issue) => issue.severity === "error"),
    ).length;
    if (enabledDraft && currentUsableCount === 0) {
      setError("启用视频生成前至少需要一个启用且可用的供应商");
      return;
    }
    const items: VideoProviderItemIn[] = [];
    for (const draft of drafts) {
      const name = draft.name.trim();
      const models = rowsToModels(draft.models);
      items.push({
        name,
        kind: draft.kind,
        base_url: draft.base_url.trim(),
        ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
        enabled: draft.enabled,
        priority: Number(draft.priority) || 0,
        weight: Math.max(1, Number(draft.weight) || 1),
        concurrency: Math.max(1, Math.min(32, Number(draft.concurrency) || 1)),
        proxy: draft.proxy.trim() || null,
        models,
      });
    }
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
    <section className="space-y-5 pb-24">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-panel)] border border-accent-border bg-accent-soft">
            <Clapperboard className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-[var(--fg-0)]">AI 视频供应商</h3>
            <p className="type-caption text-[var(--fg-2)]">
              Seedance / HappyHorse 任务 API · 文字 / 首帧 / 参考生成
            </p>
          </div>
        </div>
        {!editing && (
          <Button
            variant="primary"
            size="sm"
            onClick={startEdit}
            leftIcon={<Pencil className="h-3.5 w-3.5" />}
          >
            编辑
          </Button>
        )}
      </div>

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
            onClick={() => setError(null)}
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

      {!editing ? (
        <>
          <OverviewGrid
            enabled={Boolean(query.data?.enabled)}
            source={query.data?.source}
            providerCount={serverItems.length}
            enabledCount={enabledCount}
            usableCount={usableCount}
            totalConcurrency={totalConcurrency}
            coveredActions={coveredActions}
          />

          <ReadinessPanel
            enabled={Boolean(query.data?.enabled)}
            usableCount={usableCount}
            coveredActions={coveredActions}
            totalConcurrency={totalConcurrency}
            providerIssues={providerSummaries.flatMap((summary) =>
              summary.issues.map((issue) => ({
                ...issue,
                message: `${summary.name}：${issue.message}`,
              })),
            )}
          />

          <CapabilityMatrix rows={capabilityRows} />

          <div className="space-y-3">
            {serverItems.map((item) => (
              <ProviderCard
                key={item.name}
                item={item}
                summary={providerSummaries.find((summary) => summary.name === item.name)}
              />
            ))}
            {serverItems.length === 0 && (
              <EmptyState onCreate={startEdit} />
            )}
          </div>
        </>
      ) : (
        <div className="space-y-5">
          <EditCommandCenter
            enabled={enabledDraft}
            source={query.data?.source}
            draftCount={drafts.length}
            errorCount={draftErrorCount + (globalDraftIssue ? 1 : 0)}
            warningCount={draftWarningCount}
            globalIssue={globalDraftIssue}
            onToggle={setEnabledDraft}
            onAddVolcano={() => addDraft(emptyVolcanoDraft())}
            onAddVolcanoThirdParty={() => addDraft(emptyVolcanoThirdPartyDraft())}
            onAddDashscope={() => addDraft(emptyDashScopeDraft())}
            onAddVeo={() => addDraft(emptyVeoDraft())}
            onAddFake={() => addDraft(emptyFakeDraft())}
          />

          <div className="space-y-4">
            {drafts.map((draft, idx) => (
              <ProviderEditor
                key={draft._key}
                draft={draft}
                summary={draftSummaries[idx]}
                storedKeyHint={
                  draft.original_name && draft.original_name === draft.name.trim()
                    ? storedKeyHint(serverItems, draft.original_name)
                    : ""
                }
                proxies={proxyOptions.map((item) => item.name)}
                onPatch={(patch) => updateDraft(idx, patch)}
                onDelete={() =>
                  setDrafts((prev) => prev?.filter((_, i) => i !== idx) ?? null)
                }
                onAddModel={() =>
                  updateDraft(idx, { models: [...draft.models, emptyModelDraft()] })
                }
                onApplyPreset={() =>
                  updateDraft(idx, presetPatchForKind(draft))
                }
                onPatchModel={(modelIdx, patch) => updateModel(idx, modelIdx, patch)}
                onMirrorModel={(modelIdx) => {
                  const row = draft.models[modelIdx];
                  const value = row.t2v.trim() || row.i2v.trim() || row.reference.trim();
                  if (!value) return;
                  updateModel(idx, modelIdx, {
                    t2v: row.t2v.trim() || value,
                    i2v: row.i2v.trim() || value,
                    reference: row.reference.trim() || value,
                  });
                }}
                onDeleteModel={(modelIdx) =>
                  updateDraft(idx, {
                    models: draft.models.filter((_, i) => i !== modelIdx),
                  })
                }
              />
            ))}
            {drafts.length === 0 && (
              <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-5 py-8 text-center">
                <p className="text-sm font-medium text-[var(--fg-0)]">暂无编辑中的供应商</p>
                <p className="mt-1 type-caption text-[var(--fg-2)]">
                  使用上方预设添加 Seedance 或 HappyHorse。
                </p>
              </div>
            )}
          </div>

          <div className="sticky bottom-4 z-10 flex flex-col gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/95 p-3 shadow-[var(--shadow-3)] backdrop-blur-xl sm:flex-row sm:items-center sm:justify-between">
            <div className="type-caption text-[var(--fg-2)]">
              {globalDraftIssue
                ? globalDraftIssue
                : draftErrorCount > 0
                  ? `还有 ${draftErrorCount} 个错误需要处理`
                : draftWarningCount > 0
                  ? `${draftWarningCount} 个提示不会阻止保存`
                  : "配置可以保存"}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="primary"
                size="sm"
                onClick={save}
                loading={updateMut.isPending}
                leftIcon={<Save className="h-3.5 w-3.5" />}
              >
                保存配置
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setDrafts(null);
                  setError(null);
                }}
              >
                取消
              </Button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function OverviewGrid({
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
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        icon={<Power className="h-4 w-4" />}
        label="上线状态"
        value={enabled ? "已开启" : "已关闭"}
        detail={`来源：${sourceLabel(source)}`}
        tone={enabled ? "success" : "muted"}
      />
      <MetricCard
        icon={<Server className="h-4 w-4" />}
        label="可用供应商"
        value={`${usableCount} / ${providerCount}`}
        detail={`${enabledCount} 个启用`}
        tone={usableCount > 0 ? "success" : "warning"}
      />
      <MetricCard
        icon={<Layers3 className="h-4 w-4" />}
        label="动作覆盖"
        value={`${coveredActions.size} / ${VIDEO_ACTIONS.length}`}
        detail={actionCoverageLabel(coveredActions)}
        tone={coveredActions.size === VIDEO_ACTIONS.length ? "success" : "warning"}
      />
      <MetricCard
        icon={<Gauge className="h-4 w-4" />}
        label="并发容量"
        value={`${totalConcurrency}`}
        detail="启用供应商合计"
        tone={totalConcurrency > 0 ? "success" : "muted"}
      />
    </div>
  );
}

function MetricCard({
  icon,
  label,
  value,
  detail,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: string;
  tone: "success" | "warning" | "muted";
}) {
  const toneClass =
    tone === "success"
      ? "border-success-border bg-success-soft text-success"
      : tone === "warning"
        ? "border-warning-border bg-warning-soft text-warning"
        : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]";
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="type-caption text-[var(--fg-2)]">{label}</p>
        <span className={`inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] border ${toneClass}`}>
          {icon}
        </span>
      </div>
      <p className="mt-3 text-xl font-semibold text-[var(--fg-0)]">{value}</p>
      <p className="mt-1 type-caption text-[var(--fg-2)]">{detail}</p>
    </div>
  );
}

function ReadinessPanel({
  enabled,
  usableCount,
  coveredActions,
  totalConcurrency,
  providerIssues,
}: {
  enabled: boolean;
  usableCount: number;
  coveredActions: Set<VideoAction>;
  totalConcurrency: number;
  providerIssues: Issue[];
}) {
  const checks = [
    {
      label: "视频生成总开关",
      ok: enabled,
      detail: enabled ? "已开启" : "未开启",
    },
    {
      label: "可用供应商",
      ok: usableCount > 0,
      detail: usableCount > 0 ? `${usableCount} 个可用` : "没有可用供应商",
    },
    {
      label: "动作覆盖",
      ok: coveredActions.size === VIDEO_ACTIONS.length,
      detail: actionCoverageLabel(coveredActions),
    },
    {
      label: "并发容量",
      ok: totalConcurrency > 0,
      detail: `${totalConcurrency} 路`,
    },
  ];
  const topIssues = providerIssues.slice(0, 4);
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <p className="text-sm font-medium text-[var(--fg-0)]">上线检查</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            当前配置保存后，任务创建会按这些条件决定是否可提交。
          </p>
        </div>
        <StatusPill
          tone={checks.every((item) => item.ok) && providerIssues.length === 0 ? "success" : "warning"}
          label={checks.every((item) => item.ok) ? "基本就绪" : "需要处理"}
        />
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-4">
        {checks.map((check) => (
          <div
            key={check.label}
            className="flex min-w-0 items-start gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-2"
          >
            {check.ok ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
            ) : (
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            )}
            <div className="min-w-0">
              <p className="text-xs font-medium text-[var(--fg-0)]">{check.label}</p>
              <p className="type-caption text-[var(--fg-2)]">{check.detail}</p>
            </div>
          </div>
        ))}
      </div>
      {topIssues.length > 0 && (
        <IssueList className="mt-4" issues={topIssues} />
      )}
    </div>
  );
}

function CapabilityMatrix({ rows }: { rows: CapabilityRow[] }) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-[var(--fg-0)]">模型能力覆盖</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            只统计已启用供应商。
          </p>
        </div>
        <Layers3 className="h-4 w-4 text-[var(--fg-2)]" />
      </div>
      {rows.length === 0 ? (
        <p className="mt-4 rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-2 type-caption text-[var(--fg-2)]">
          暂无启用模型映射。
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-[640px] w-full border-separate border-spacing-0 text-left text-xs">
            <thead>
              <tr className="text-[var(--fg-2)]">
                <th className="border-b border-[var(--border-subtle)] pb-2 pr-3 font-medium">业务模型</th>
                {VIDEO_ACTIONS.map((action) => (
                  <th key={action} className="border-b border-[var(--border-subtle)] px-3 pb-2 font-medium">
                    {ACTION_LABELS[action]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.model}>
                  <td className="border-b border-[var(--border-subtle)] py-3 pr-3 font-mono text-[var(--fg-0)]">
                    {row.model}
                  </td>
                  {VIDEO_ACTIONS.map((action) => (
                    <td key={action} className="border-b border-[var(--border-subtle)] px-3 py-3">
                      <ProviderNames names={row[action]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ProviderNames({ names }: { names: string[] }) {
  if (names.length === 0) {
    return <span className="text-[var(--fg-3)]">未覆盖</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {names.map((name) => (
        <span
          key={name}
          className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 font-mono text-[11px] text-[var(--fg-1)]"
        >
          {name}
        </span>
      ))}
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
  const models = Object.entries(item.models);
  return (
    <article className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-[var(--fg-0)]">{item.name}</p>
            <StatusPill tone={issueTone(issues)} label={item.enabled ? "启用" : "停用"} />
            <StatusPill tone="neutral" label={KIND_LABELS[item.kind]} />
          </div>
          <p className="mt-1 font-mono text-xs text-[var(--fg-2)] [overflow-wrap:anywhere]">
            {item.base_url}
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs text-[var(--fg-2)] sm:grid-cols-4 md:min-w-[360px]">
          <MiniStat label="Key" value={item.api_key_hint || "未保存"} />
          <MiniStat label="代理" value={item.proxy || "直连"} />
          <MiniStat label="优先级" value={String(item.priority)} />
          <MiniStat label="并发" value={String(item.concurrency)} />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {VIDEO_ACTIONS.map((action) => (
          <StatusPill
            key={action}
            tone={summary?.capabilities.has(action) ? "success" : "neutral"}
            label={ACTION_LABELS[action]}
          />
        ))}
      </div>

      {issues.length > 0 && <IssueList className="mt-4" issues={issues} />}

      <div className="mt-4 overflow-x-auto">
        <table className="min-w-[560px] w-full border-separate border-spacing-0 text-left text-xs">
          <thead>
            <tr className="text-[var(--fg-2)]">
              <th className="border-b border-[var(--border-subtle)] pb-2 pr-3 font-medium">模型键</th>
              <th className="border-b border-[var(--border-subtle)] px-3 pb-2 font-medium">上游模型</th>
            </tr>
          </thead>
          <tbody>
            {models.map(([key, value]) => (
              <tr key={key}>
                <td className="border-b border-[var(--border-subtle)] py-2 pr-3 font-mono text-[11px] text-[var(--fg-1)] [overflow-wrap:anywhere]">
                  {key}
                </td>
                <td className="border-b border-[var(--border-subtle)] px-3 py-2 font-mono text-[11px] text-[var(--fg-2)] [overflow-wrap:anywhere]">
                  {value}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-2">
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="mt-0.5 truncate text-xs font-medium text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-6 py-10 text-center">
      <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)]">
        <Clapperboard className="h-4 w-4 text-[var(--fg-1)]" />
      </div>
      <p className="mt-3 text-sm font-medium text-[var(--fg-0)]">还没有 AI 视频供应商</p>
      <p className="mx-auto mt-1 max-w-md type-caption text-[var(--fg-2)]">
        添加供应商后，视频页才能创建 Seedance 或 HappyHorse 任务。
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
  onAddDashscope,
  onAddVeo,
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
  onAddDashscope: () => void;
  onAddVeo: () => void;
  onAddFake: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(340px,0.75fr)]">
        <div className="space-y-3">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="text-sm font-medium text-[var(--fg-0)]">配置中心</p>
              <p className="mt-1 type-caption text-[var(--fg-2)]">
                当前来源：{sourceLabel(source)} · {draftCount} 个供应商
              </p>
            </div>
            <label className="flex items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-sm text-[var(--fg-0)] md:min-w-[220px]">
              <span>启用视频生成</span>
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => onToggle(event.target.checked)}
              />
            </label>
          </div>
          {source === "env" && (
            <div className="rounded-[var(--radius-card)] border border-warning-border bg-warning-soft px-3 py-2 type-caption text-warning">
              保存后将写入数据库配置，后续优先读取数据库。
            </div>
          )}
          {globalIssue && (
            <div role="alert" className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger">
              {globalIssue}
            </div>
          )}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
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
              icon={<ShieldCheck className="h-4 w-4" />}
              title="测试供应商"
              detail="本地假任务"
              onClick={onAddFake}
            />
          </div>
        </div>
        <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-1">
          <CompactStatus icon={<Server className="h-4 w-4" />} label="供应商" value={String(draftCount)} />
          <CompactStatus icon={<AlertCircle className="h-4 w-4" />} label="错误" value={String(errorCount)} tone={errorCount > 0 ? "danger" : "neutral"} />
          <CompactStatus icon={<CheckCircle2 className="h-4 w-4" />} label="提示" value={String(warningCount)} tone={warningCount > 0 ? "warning" : "neutral"} />
        </div>
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
      className="flex min-h-20 items-start gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-3 text-left transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
    >
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-accent-border bg-accent-soft text-accent">
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-[var(--fg-0)]">{title}</span>
        <span className="mt-1 block type-caption text-[var(--fg-2)]">{detail}</span>
      </span>
    </button>
  );
}

function CompactStatus({
  icon,
  label,
  value,
  tone = "neutral",
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone?: "neutral" | "danger" | "warning";
}) {
  const toneClass =
    tone === "danger"
      ? "text-danger"
      : tone === "warning"
        ? "text-warning"
        : "text-[var(--fg-1)]";
  return (
    <div className="flex items-center justify-between gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-2">
      <span className={`inline-flex items-center gap-2 text-xs ${toneClass}`}>
        {icon}
        {label}
      </span>
      <span className="text-sm font-semibold text-[var(--fg-0)]">{value}</span>
    </div>
  );
}

function ProviderEditor({
  draft,
  summary,
  storedKeyHint,
  proxies,
  onPatch,
  onDelete,
  onAddModel,
  onApplyPreset,
  onPatchModel,
  onMirrorModel,
  onDeleteModel,
}: {
  draft: Draft;
  summary: ProviderSummary | undefined;
  storedKeyHint: string;
  proxies: string[];
  onPatch: (patch: Partial<Draft>) => void;
  onDelete: () => void;
  onAddModel: () => void;
  onApplyPreset: () => void;
  onPatchModel: (idx: number, patch: Partial<ModelDraft>) => void;
  onMirrorModel: (idx: number) => void;
  onDeleteModel: (idx: number) => void;
}) {
  const issues = summary?.issues ?? [];
  const tone = issueTone(issues);
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-[var(--fg-0)]">
              {draft.name.trim() || "未命名供应商"}
            </p>
            <StatusPill tone={tone} label={tone === "success" ? "可保存" : tone === "danger" ? "需修复" : "有提示"} />
            <StatusPill tone="neutral" label={KIND_LABELS[draft.kind]} />
          </div>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            {summary ? actionCoverageLabel(summary.capabilities) : "未配置动作"}
          </p>
        </div>
        <IconButton variant="ghost" size="sm" aria-label="删除供应商" onClick={onDelete}>
          <Trash2 className="h-4 w-4" />
        </IconButton>
      </div>

      {issues.length > 0 && <IssueList className="mt-4" issues={issues} />}

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.55fr)]">
        <div className="space-y-4">
          <SectionTitle icon={<Server className="h-4 w-4" />} title="基础连接" />
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="名称" value={draft.name} onChange={(name) => onPatch({ name })} />
            <label className="space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">类型</span>
              <select
                value={draft.kind}
                onChange={(event) => {
                  const kind = event.target.value as VideoProviderKind;
                  if (kind === "volcano") {
                    onPatch(volcanoPresetPatch(draft));
                  } else if (kind === "volcano_third_party") {
                    onPatch(volcanoThirdPartyPresetPatch(draft));
                  } else if (kind === "dashscope") {
                    onPatch(dashscopePresetPatch(draft));
                  } else if (kind === "veo") {
                    onPatch(veoPresetPatch(draft));
                  } else if (kind === "fake") {
                    onPatch(fakePresetPatch(draft));
                  } else {
                    onPatch({ kind });
                  }
                }}
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
              >
                <option value="volcano">火山方舟</option>
                <option value="volcano_third_party">火山第三方 / MOYU</option>
                <option value="dashscope">DashScope / HappyHorse</option>
                <option value="veo">Google Veo</option>
                <option value="fake">测试</option>
              </select>
            </label>
            <Field
              label="Base URL"
              value={draft.base_url}
              onChange={(base_url) => onPatch({ base_url })}
            />
            <Field
              label="API Key"
              value={draft.api_key}
              onChange={(api_key) => onPatch({ api_key })}
              placeholder={storedKeyHint ? `留空保留 ${storedKeyHint}` : "必填"}
              type="password"
            />
          </div>

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
        </div>

        <div className="space-y-3">
          <label className="flex items-center justify-between gap-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-3 text-sm text-[var(--fg-0)]">
            <span>启用此供应商</span>
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => onPatch({ enabled: event.target.checked })}
            />
          </label>
          <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] px-3 py-3">
            <div className="flex items-center gap-2 text-xs font-medium text-[var(--fg-0)]">
              <KeyRound className="h-4 w-4 text-[var(--fg-2)]" />
              Key 状态
            </div>
            <p className="mt-2 type-caption text-[var(--fg-2)]">
              {draft.kind === "fake"
                ? "测试供应商不需要 Key"
                : draft.api_key.trim()
                  ? "将更新为新 Key"
                  : draft.original_name && draft.original_name !== draft.name.trim()
                    ? "重命名后需重新填写 Key"
                  : storedKeyHint
                    ? `保留已保存 Key：${storedKeyHint}`
                    : "未保存 Key"}
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
      </div>

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
    </div>
  );
}

function SectionTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
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
    <span className={`inline-flex items-center rounded-[var(--radius-control)] border px-2 py-1 text-[11px] font-medium ${className}`}>
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
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--accent)]/50"
      />
    </label>
  );
}
