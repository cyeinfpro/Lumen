import { ApiError } from "@/lib/apiClient";
import type {
  VideoProviderItemIn,
  VideoProviderItemOut,
  VideoProviderKind,
} from "@/lib/types";
import { evaluateVolcanoAssetCredentials } from "./videoProviderAssetRules";

export type VideoAction = "t2v" | "i2v" | "reference";

export type ModelDraft = {
  _key: number;
  model: string;
  t2v: string;
  i2v: string;
  reference: string;
};

export type Draft = {
  _key: number;
  original_name?: string;
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key: string;
  access_key_id: string;
  secret_access_key: string;
  project_name: string;
  region: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  supports_idempotency: boolean;
  proxy: string;
  models: ModelDraft[];
};

type IssueSeverity = "error" | "warning";

export type Issue = {
  severity: IssueSeverity;
  message: string;
};

export type ProviderSummary = {
  name: string;
  kind: VideoProviderKind;
  enabled: boolean;
  hasKey: boolean;
  capabilities: Set<VideoAction>;
  modelNames: string[];
  concurrency: number;
  issues: Issue[];
};

const VOLCANO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3";
export const VOLCANO_DEFAULT_PROJECT_NAME = "default";
export const VOLCANO_DEFAULT_REGION = "cn-beijing";
const VOLCANO_THIRD_PARTY_BASE_URL = "https://www.moyu.info";
const VOLCANO_NEWAPI_BASE_URL = "https://zz1cc.cc.cd";
const DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com";
const VEO_BASE_URL = "https://generativelanguage.googleapis.com/v1beta";
const OMNI_FLASH_BASE_URL = "https://api.example.com";
const VOLCANO_MODEL_PRESETS = [
  {
    model: "seedance-2.0",
    upstream: "doubao-seedance-2-0-260128",
  },
  {
    model: "seedance-2.0-fast",
    upstream: "doubao-seedance-2-0-fast-260128",
  },
  {
    model: "seedance-2.0-mini",
    upstream: "doubao-seedance-2-0-mini-260615",
  },
] as const;
const VOLCANO_NEWAPI_MODEL_PRESETS = [
  {
    model: "video-ds-2.0",
    upstream: "video-ds-2.0",
  },
  {
    model: "video-ds-2.0-fast",
    upstream: "video-ds-2.0-fast",
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
const OMNI_FLASH_MODEL = "omni-flash";
const TEST_VIDEO_MODEL = "test-video";
export const VIDEO_ACTIONS: VideoAction[] = ["t2v", "i2v", "reference"];

export function inferVolcanoRegion(baseUrl: string): string | null {
  try {
    const hostname = new URL(baseUrl.trim()).hostname.toLowerCase();
    return (
      hostname.match(/^ark\.([a-z0-9]+(?:-[a-z0-9]+)*)\.volces\.com$/)?.[1] ??
      null
    );
  } catch {
    return null;
  }
}

function resolvedVolcanoRegion(
  baseUrl: string,
  region?: string | null,
): string {
  return (
    region?.trim() || inferVolcanoRegion(baseUrl) || VOLCANO_DEFAULT_REGION
  );
}

export function videoProviderKindCanBeEnabled(
  kind: VideoProviderKind,
): boolean {
  return kind !== "veo";
}

export function normalizeVideoProviderEnabled(
  kind: VideoProviderKind,
  enabled: boolean,
): boolean {
  return videoProviderKindCanBeEnabled(kind) && enabled;
}

function isOmniFlashPlaceholderBaseUrl(
  kind: VideoProviderKind,
  baseUrl: string,
): boolean {
  if (kind !== "omni_flash") return false;
  try {
    return new URL(baseUrl.trim()).hostname.toLowerCase() === "api.example.com";
  } catch {
    return false;
  }
}

export const ACTION_LABELS: Record<VideoAction, string> = {
  t2v: "文字生成",
  i2v: "首帧生成",
  reference: "参考生成",
};

export const KIND_LABELS: Record<VideoProviderKind, string> = {
  volcano: "火山方舟",
  volcano_third_party: "火山第三方",
  volcano_newapi: "火山 New API",
  dashscope: "DashScope",
  veo: "Google Veo",
  omni_flash: "Google Omni Flash",
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

function volcanoNewApiModelDrafts(): ModelDraft[] {
  return VOLCANO_NEWAPI_MODEL_PRESETS.map((preset) =>
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

function omniFlashModelDrafts(): ModelDraft[] {
  return [
    modelDraft(
      OMNI_FLASH_MODEL,
      "gemini_omni_flash",
      "gemini_omni_flash",
      "gemini_omni_flash",
    ),
  ];
}

function fakeModelDrafts(): ModelDraft[] {
  return [
    modelDraft(
      TEST_VIDEO_MODEL,
      TEST_VIDEO_MODEL,
      TEST_VIDEO_MODEL,
      TEST_VIDEO_MODEL,
    ),
  ];
}

function defaultVolcanoAssetDraftFields(): Pick<
  Draft,
  "access_key_id" | "secret_access_key" | "project_name" | "region"
> {
  return {
    access_key_id: "",
    secret_access_key: "",
    project_name: VOLCANO_DEFAULT_PROJECT_NAME,
    region: resolvedVolcanoRegion(VOLCANO_BASE_URL),
  };
}

export function emptyModelDraft(): ModelDraft {
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
    if (row.reference.trim()) {
      models[`${model}:reference`] = row.reference.trim();
    }
  }
  return models;
}

export function modelNamesFromModels(
  models: Record<string, string>,
): string[] {
  const names = new Set<string>();
  for (const [key, value] of Object.entries(models)) {
    if (!value.trim()) continue;
    const name = baseModelName(key);
    if (name) names.add(name);
  }
  return Array.from(names).sort((a, b) => a.localeCompare(b));
}

function capabilitiesFromModels(
  models: Record<string, string>,
): Set<VideoAction> {
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

export function toDraft(item: VideoProviderItemOut): Draft {
  return {
    _key: nextKey(),
    original_name: item.name,
    name: item.name,
    kind: item.kind,
    base_url: item.base_url,
    api_key: "",
    access_key_id: "",
    secret_access_key: "",
    project_name: item.project_name?.trim() || VOLCANO_DEFAULT_PROJECT_NAME,
    region: resolvedVolcanoRegion(item.base_url, item.region),
    enabled: normalizeVideoProviderEnabled(item.kind, item.enabled),
    priority: item.priority,
    weight: item.weight,
    concurrency: item.concurrency,
    supports_idempotency: item.supports_idempotency,
    proxy: item.proxy ?? "",
    models: modelsToRows(item.models),
  };
}

export function emptyVolcanoDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-main",
    kind: "volcano",
    base_url: VOLCANO_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 10,
    supports_idempotency: false,
    proxy: "",
    models: volcanoModelDrafts(),
  };
}

export function emptyVolcanoThirdPartyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-third-party",
    kind: "volcano_third_party",
    base_url: VOLCANO_THIRD_PARTY_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 10,
    supports_idempotency: false,
    proxy: "",
    models: volcanoModelDrafts(),
  };
}

export function emptyVolcanoNewApiDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-newapi",
    kind: "volcano_newapi",
    base_url: VOLCANO_NEWAPI_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 10,
    supports_idempotency: false,
    proxy: "",
    models: volcanoNewApiModelDrafts(),
  };
}

export function emptyDashScopeDraft(): Draft {
  return {
    _key: nextKey(),
    name: "dashscope-happyhorse",
    kind: "dashscope",
    base_url: DASHSCOPE_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 2,
    supports_idempotency: false,
    proxy: "",
    models: happyHorseModelDrafts(),
  };
}

export function emptyVeoDraft(): Draft {
  return {
    _key: nextKey(),
    name: "google-veo",
    kind: "veo",
    base_url: VEO_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: false,
    priority: 80,
    weight: 1,
    concurrency: 2,
    supports_idempotency: false,
    proxy: "",
    models: veoModelDrafts(),
  };
}

export function emptyOmniFlashDraft(): Draft {
  return {
    _key: nextKey(),
    name: "google-omni-flash",
    kind: "omni_flash",
    base_url: OMNI_FLASH_BASE_URL,
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: false,
    priority: 90,
    weight: 1,
    concurrency: 2,
    supports_idempotency: false,
    proxy: "",
    models: omniFlashModelDrafts(),
  };
}

export function emptyFakeDraft(): Draft {
  return {
    _key: nextKey(),
    name: "video-test",
    kind: "fake",
    base_url: "http://localhost",
    api_key: "",
    ...defaultVolcanoAssetDraftFields(),
    enabled: false,
    priority: 10,
    weight: 1,
    concurrency: 1,
    supports_idempotency: true,
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
    name === "volcano-newapi" ||
    name === "dashscope-happyhorse" ||
    name === "google-veo" ||
    name === "google-omni-flash" ||
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
    project_name: draft.project_name.trim() || VOLCANO_DEFAULT_PROJECT_NAME,
    region: resolvedVolcanoRegion(VOLCANO_BASE_URL, draft.region),
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

function volcanoNewApiPresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "volcano-newapi"),
    kind: "volcano_newapi",
    base_url: VOLCANO_NEWAPI_BASE_URL,
    enabled: draft.enabled,
    priority: draft.priority || 100,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: 10,
    models: volcanoNewApiModelDrafts(),
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
    enabled: false,
    priority: draft.priority || 80,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: Math.max(1, Number(draft.concurrency) || 2),
    models: veoModelDrafts(),
  };
}

function omniFlashPresetPatch(draft: Draft): Partial<Draft> {
  return {
    name: presetName(draft, "google-omni-flash"),
    kind: "omni_flash",
    base_url: OMNI_FLASH_BASE_URL,
    enabled: false,
    priority: draft.priority || 90,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: Math.max(1, Number(draft.concurrency) || 2),
    models: omniFlashModelDrafts(),
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

export function presetPatchForKind(draft: Draft): Partial<Draft> {
  if (draft.kind === "volcano_third_party") {
    return volcanoThirdPartyPresetPatch(draft);
  }
  if (draft.kind === "volcano_newapi") return volcanoNewApiPresetPatch(draft);
  if (draft.kind === "dashscope") return dashscopePresetPatch(draft);
  if (draft.kind === "veo") return veoPresetPatch(draft);
  if (draft.kind === "omni_flash") return omniFlashPresetPatch(draft);
  if (draft.kind === "fake") return fakePresetPatch(draft);
  return volcanoPresetPatch(draft);
}

function storedKeyHint(
  serverItems: VideoProviderItemOut[],
  providerName: string,
): string {
  return (
    serverItems
      .find((item) => item.name === providerName)
      ?.api_key_hint?.trim() ?? ""
  );
}

function hasStoredKey(
  serverItems: VideoProviderItemOut[],
  providerName: string,
): boolean {
  return Boolean(storedKeyHint(serverItems, providerName));
}

function hasDraftKey(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
): boolean {
  if (draft.kind === "fake") return true;
  if (draft.api_key.trim()) return true;
  const name = draft.name.trim();
  return Boolean(
    name && draft.original_name === name && hasStoredKey(serverItems, name),
  );
}

export function draftWasRenamed(draft: Draft): boolean {
  return Boolean(
    draft.original_name && draft.original_name !== draft.name.trim(),
  );
}

function originalProvider(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
): VideoProviderItemOut | undefined {
  if (!draft.original_name) return undefined;
  return serverItems.find((item) => item.name === draft.original_name);
}

export function saveError(err: Error): string {
  if (err instanceof ApiError) {
    return err.message || `保存失败 (HTTP ${err.status})`;
  }
  return err.message || "保存失败";
}

export function sourceLabel(source: string | undefined): string {
  if (source === "db") return "数据库";
  if (source === "env") return "环境变量";
  return "未配置";
}

export function issueTone(
  issues: Issue[],
): "danger" | "warning" | "success" {
  if (issues.some((item) => item.severity === "error")) return "danger";
  if (issues.length > 0) return "warning";
  return "success";
}

export function analyzeProvider(
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
  if (item.kind === "veo") {
    issues.push({
      severity: item.enabled ? "error" : "warning",
      message: item.enabled
        ? "Veo 适配器尚未接入 Worker，必须停用"
        : "Veo 适配器尚未接入 Worker，暂不可启用",
    });
  }
  if (isOmniFlashPlaceholderBaseUrl(item.kind, item.base_url)) {
    issues.push({
      severity: "error",
      message: "Omni Flash 仍使用占位 Base URL",
    });
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

function draftBaseUrlIssues(draft: Draft): Issue[] {
  const baseUrl = draft.base_url.trim();
  if (!baseUrl) {
    return [{ severity: "error", message: "缺少 Base URL" }];
  }

  const issues: Issue[] = [];
  try {
    const url = new URL(baseUrl);
    if (!["http:", "https:"].includes(url.protocol)) {
      issues.push({
        severity: "error",
        message: "Base URL 只能使用 HTTP 或 HTTPS",
      });
    }
    if (!url.hostname) {
      issues.push({ severity: "error", message: "Base URL 必须包含主机名" });
    }
    if (url.username || url.password) {
      issues.push({
        severity: "error",
        message: "Base URL 不能包含用户名或密码",
      });
    }
    if (isOmniFlashPlaceholderBaseUrl(draft.kind, baseUrl)) {
      issues.push({
        severity: "error",
        message: "Omni Flash 的 Base URL 仍是占位地址，请替换为真实网关",
      });
    }
  } catch {
    issues.push({ severity: "error", message: "Base URL 格式不合法" });
  }
  return issues;
}

function draftIdentityIssues(name: string, duplicate: boolean): Issue[] {
  const issues: Issue[] = [];
  if (!name) {
    issues.push({ severity: "error", message: "供应商名称不能为空" });
  }
  if (duplicate) {
    issues.push({ severity: "error", message: "供应商名称重复" });
  }
  return issues;
}

function draftEnabledIssues(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
  modelNames: string[],
  capabilities: Set<VideoAction>,
): Issue[] {
  if (!draft.enabled) {
    return [{ severity: "warning", message: "保存后不会参与视频任务路由" }];
  }
  const issues: Issue[] = [];
  if (!hasDraftKey(draft, serverItems)) {
    issues.push({ severity: "error", message: "启用状态下必须填写 API Key" });
  }
  if (modelNames.length === 0) {
    issues.push({ severity: "error", message: "至少需要一个模型映射" });
  }
  if (capabilities.size === 0) {
    issues.push({ severity: "error", message: "至少需要一个可用动作" });
  }
  return issues;
}

function draftKindIssues(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
): Issue[] {
  const issues: Issue[] = [];
  if (
    draft.kind !== "fake" &&
    draftWasRenamed(draft) &&
    !draft.api_key.trim()
  ) {
    issues.push({
      severity: "error",
      message: "供应商重命名后必须重新填写 API Key",
    });
  }
  if (draft.kind === "veo" && draft.enabled) {
    issues.push({
      severity: "error",
      message: "Veo 适配器尚未接入 Worker，暂不可启用",
    });
  }
  if (draft.kind !== "volcano") return issues;

  const stored = originalProvider(draft, serverItems);
  const assetRule = evaluateVolcanoAssetCredentials({
    renamed: draftWasRenamed(draft),
    accessKeyId: draft.access_key_id,
    secretAccessKey: draft.secret_access_key,
    storedAccessKeyIdHint: stored?.access_key_id_hint,
    storedSecretAccessKeyHint: stored?.secret_access_key_hint,
    assetManagementReady: stored?.asset_management_ready,
  });
  if (assetRule.error) {
    issues.push({
      severity: "error",
      message: assetRule.error === "rename_replacement"
        ? "供应商重命名后需重新填写火山资产 Access Key ID 与 Secret Access Key"
        : "火山资产 Access Key ID 与 Secret Access Key 必须同时填写",
    });
  }
  return issues;
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
  const issues = [
    ...draftIdentityIssues(name, duplicate),
    ...draftBaseUrlIssues(draft),
    ...draftEnabledIssues(draft, serverItems, modelNames, capabilities),
    ...draftKindIssues(draft, serverItems),
  ];
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

export function summaryIsUsable(summary: ProviderSummary): boolean {
  return (
    summary.enabled &&
    summary.hasKey &&
    summary.modelNames.length > 0 &&
    !summary.issues.some((issue) => issue.severity === "error")
  );
}

function volcanoDraftInput(
  draft: Draft,
): Pick<
  VideoProviderItemIn,
  "access_key_id" | "secret_access_key" | "project_name" | "region"
> {
  return {
    access_key_id: draft.access_key_id.trim(),
    secret_access_key: draft.secret_access_key.trim(),
    project_name: draft.project_name.trim() || VOLCANO_DEFAULT_PROJECT_NAME,
    region: resolvedVolcanoRegion(draft.base_url, draft.region),
  };
}

export function draftToInput(draft: Draft): VideoProviderItemIn {
  const apiKey = draft.api_key.trim();
  const volcanoFields =
    draft.kind === "volcano" ? volcanoDraftInput(draft) : null;
  return {
    name: draft.name.trim(),
    kind: draft.kind,
    base_url: draft.base_url.trim(),
    ...(apiKey ? { api_key: apiKey } : {}),
    ...(volcanoFields?.access_key_id
      ? { access_key_id: volcanoFields.access_key_id }
      : {}),
    ...(volcanoFields?.secret_access_key
      ? { secret_access_key: volcanoFields.secret_access_key }
      : {}),
    ...(volcanoFields
      ? {
          project_name: volcanoFields.project_name,
          region: volcanoFields.region,
        }
      : {}),
    enabled: normalizeVideoProviderEnabled(draft.kind, draft.enabled),
    priority: Number(draft.priority) || 0,
    weight: Math.max(1, Number(draft.weight) || 1),
    concurrency: Math.max(1, Math.min(32, Number(draft.concurrency) || 1)),
    supports_idempotency: draft.supports_idempotency,
    proxy: draft.proxy.trim() || null,
    models: rowsToModels(draft.models),
  };
}

export function draftSaveError(
  drafts: Draft[],
  enabled: boolean,
  serverItems: VideoProviderItemOut[],
): string | null {
  if (enabled && drafts.length === 0) {
    return "开启视频生成前至少添加一个视频供应商";
  }
  const summaries = analyzeDrafts(drafts, enabled, serverItems);
  const firstError = summaries
    .flatMap((summary) => summary.issues)
    .find((issue) => issue.severity === "error");
  if (firstError) return firstError.message;
  if (enabled && !summaries.some(summaryIsUsable)) {
    return "启用视频生成前至少需要一个启用且可用的供应商";
  }
  return null;
}

export type StoredDraftHints = {
  key: string;
  accessKeyId: string;
  secretAccessKey: string;
  assetManagementReady: boolean;
  assetCredentialsRequireReplacement: boolean;
};

export function storedDraftHints(
  draft: Draft,
  serverItems: VideoProviderItemOut[],
): StoredDraftHints {
  const item = originalProvider(draft, serverItems);
  const renamed = draftWasRenamed(draft);
  if (!item) {
    return {
      key: "",
      accessKeyId: "",
      secretAccessKey: "",
      assetManagementReady: false,
      assetCredentialsRequireReplacement: false,
    };
  }
  const assetRule = evaluateVolcanoAssetCredentials({
    renamed,
    accessKeyId: draft.access_key_id,
    secretAccessKey: draft.secret_access_key,
    storedAccessKeyIdHint: item.access_key_id_hint,
    storedSecretAccessKeyHint: item.secret_access_key_hint,
    assetManagementReady: item.asset_management_ready,
  });
  return {
    key: renamed ? "" : item.api_key_hint?.trim() ?? "",
    accessKeyId: renamed ? "" : item.access_key_id_hint?.trim() ?? "",
    secretAccessKey: renamed ? "" : item.secret_access_key_hint?.trim() ?? "",
    assetManagementReady: !renamed && Boolean(item.asset_management_ready),
    assetCredentialsRequireReplacement: assetRule.replacementRequired,
  };
}

export function mirroredModelPatch(
  row: ModelDraft,
): Partial<ModelDraft> | null {
  const value = row.t2v.trim() || row.i2v.trim() || row.reference.trim();
  if (!value) return null;
  return {
    t2v: row.t2v.trim() || value,
    i2v: row.i2v.trim() || value,
    reference: row.reference.trim() || value,
  };
}

export function analyzeDrafts(
  drafts: Draft[],
  enabled: boolean,
  serverItems: VideoProviderItemOut[],
): ProviderSummary[] {
  const nameCounts = new Map<string, number>();
  for (const draft of drafts) {
    const name = draft.name.trim();
    if (name) nameCounts.set(name, (nameCounts.get(name) ?? 0) + 1);
  }
  return drafts
    .map((draft) =>
      analyzeDraft(
        draft,
        serverItems,
        (nameCounts.get(draft.name.trim()) ?? 0) > 1,
      ),
    )
    .map((summary) => {
      if (enabled || summary.enabled) return summary;
      return {
        ...summary,
        issues: summary.issues.filter((issue) => issue.severity !== "warning"),
      };
    });
}

export function actionCoverageLabel(
  capabilities: Set<VideoAction>,
): string {
  if (capabilities.size === 0) return "无动作";
  return VIDEO_ACTIONS.filter((action) => capabilities.has(action))
    .map((action) => ACTION_LABELS[action])
    .join(" / ");
}
