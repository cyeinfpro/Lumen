import type {
  ProviderItemIn,
  ProviderItemOut,
  ProviderPurpose,
  ProviderProxyIn,
  ProviderProxyOut,
} from "@/lib/types";

export const WEIGHT_COLORS = [
  "var(--color-lumen-amber)",
  "#6366f1",
  "#ec4899",
  "#14b8a6",
  "#f97316",
  "#8b5cf6",
  "#06b6d4",
  "#84cc16",
];

export const PROVIDER_PURPOSES: Array<{ value: ProviderPurpose; label: string }> = [
  { value: "chat", label: "对话" },
  { value: "image", label: "生图" },
  { value: "embedding", label: "Embedding" },
];

export const DEFAULT_PURPOSES: ProviderPurpose[] = ["chat", "image"];

export function normalizePurposes(value: ProviderPurpose[] | null | undefined): ProviderPurpose[] {
  const next = value?.filter((purpose) =>
    PROVIDER_PURPOSES.some((option) => option.value === purpose),
  ) ?? [];
  return next.length > 0 ? next : DEFAULT_PURPOSES;
}

export function purposeLabel(value: ProviderPurpose): string {
  return PROVIDER_PURPOSES.find((option) => option.value === value)?.label ?? value;
}

// ---------------------------------------------------------------------------
// Draft 类型和工具函数
// ---------------------------------------------------------------------------

export type Draft = Omit<ProviderItemIn, "api_key" | "proxy"> & {
  _key: number;
  api_key: string;
  proxy: string | null;
};
export type FieldErrors = Record<string, string>;

let _draftSeq = 0;
export function nextKey() {
  return ++_draftSeq;
}

export function toDraft(p: ProviderItemOut): Draft {
  // BUG-040: 已有 provider 的 api_key 不会被加载到前端 state（设空字符串）。
  // 提交时若 api_key 为空则维持原值。显示使用后端返回的 api_key_hint（已脱敏）。
  return {
    _key: nextKey(),
    name: p.name,
    base_url: p.base_url,
    api_key: "",
    priority: p.priority,
    weight: p.weight,
    enabled: p.enabled,
    purposes: normalizePurposes(p.purposes),
    image_jobs_enabled: p.image_jobs_enabled,
    image_jobs_endpoint: p.image_jobs_endpoint ?? "auto",
    image_jobs_endpoint_lock: p.image_jobs_endpoint_lock ?? false,
    image_jobs_base_url: p.image_jobs_base_url ?? "",
    image_edit_input_transport: p.image_edit_input_transport ?? "url",
    image_concurrency: Math.max(1, p.image_concurrency ?? 1),
    proxy: p.proxy ?? null,
  };
}

export function emptyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "",
    base_url: "",
    api_key: "",
    priority: 0,
    weight: 1,
    enabled: true,
    purposes: [...DEFAULT_PURPOSES],
    image_jobs_enabled: false,
    image_jobs_endpoint: "auto",
    image_jobs_endpoint_lock: false,
    image_jobs_base_url: "",
    image_edit_input_transport: "url",
    image_concurrency: 1,
    proxy: null,
  };
}

export function providerHasStoredKey(provider: ProviderItemOut | null | undefined): boolean {
  return Boolean(provider?.api_key_hint?.trim());
}

export function proxyOutToIn(p: ProviderProxyOut): ProviderProxyIn {
  return {
    name: p.name,
    type: p.type,
    host: p.host,
    port: p.port,
    username: p.username ?? null,
    password: "",
    private_key_path: p.private_key_path ?? null,
    enabled: p.enabled,
  };
}

export function providerOutToIn(
  p: ProviderItemOut,
  patch: Partial<Pick<ProviderItemIn, "enabled" | "purposes">> = {},
): ProviderItemIn {
  return {
    name: p.name,
    base_url: p.base_url,
    priority: p.priority,
    weight: Math.max(1, p.weight),
    enabled: patch.enabled ?? p.enabled,
    purposes: patch.purposes ?? normalizePurposes(p.purposes),
    image_jobs_enabled: p.image_jobs_enabled,
    image_jobs_endpoint: p.image_jobs_endpoint ?? "auto",
    image_jobs_endpoint_lock: p.image_jobs_endpoint_lock ?? false,
    image_jobs_base_url: p.image_jobs_base_url ?? "",
    image_edit_input_transport: p.image_edit_input_transport ?? "url",
    image_concurrency: Math.max(1, p.image_concurrency ?? 1),
    proxy: p.proxy ?? null,
  };
}

export type PriorityGroup = {
  priority: number;
  items: ProviderItemOut[];
  label: string;
};

export function groupByPriority(items: ProviderItemOut[]): PriorityGroup[] {
  const map = new Map<number, ProviderItemOut[]>();
  for (const p of items) {
    const arr = map.get(p.priority) ?? [];
    arr.push(p);
    map.set(p.priority, arr);
  }
  const sorted = [...map.entries()].sort(([a], [b]) => b - a);
  return sorted.map(([priority, items], idx) => ({
    priority,
    items,
    label: idx === 0 && sorted.length > 1 ? "主要" : idx > 0 ? "后备" : "",
  }));
}

export function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 1000) return "刚刚";
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s 前`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m 前`;
  return `${Math.floor(diff / 3_600_000)}h 前`;
}

export function endpointDisplayLabel(value: string | null | undefined): string {
  if (value === "generations") return "生成接口";
  if (value === "responses") return "响应接口";
  return "自动";
}

export function editTransportDisplayLabel(value: string | null | undefined): string {
  return value === "file" ? "文件" : "链接";
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------
