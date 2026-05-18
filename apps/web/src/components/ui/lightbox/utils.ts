import type { LightboxItem } from "./types";

export type LightboxMetadataRow = {
  label: string;
  value: string;
  badge?: string;
};

export type LightboxMetadataSection = {
  title: string;
  rows: LightboxMetadataRow[];
};

const FALLBACK_URL_BASE = "https://lumen.local";

function hasText(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function formatBooleanMode(value: boolean | undefined): string | null {
  if (value === true) return "快速";
  if (value === false) return "标准";
  return null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

const NESTED_METADATA_KEYS = [
  "diagnostics",
  "generation_diagnostics",
  "image_diagnostics",
  "upstream_request",
  "upstream_details",
  "runtime",
  "run",
  "result",
  "image_generation",
  "generation",
  "metadata_jsonb",
] as const;

const REQUEST_PARAM_RECORD_KEYS = [
  "requested_params",
  "request_params",
  "requested_image_params",
  "input_params",
  "image_params",
  "params_requested",
] as const;

const EFFECTIVE_PARAM_RECORD_KEYS = [
  "effective_params",
  "actual_params",
  "effective_image_params",
  "actual_image_params",
  "resolved_params",
  "upstream_params",
  "upstream_request",
  "upstream_details",
  "params_effective",
] as const;

type ParamRead = {
  found: boolean;
  value: unknown;
};

function pushUniqueRecord(
  target: Record<string, unknown>[],
  seen: Set<Record<string, unknown>>,
  record: Record<string, unknown> | null,
) {
  if (!record || seen.has(record)) return;
  seen.add(record);
  target.push(record);
}

function collectRecordSources(item: LightboxItem): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  const seen = new Set<Record<string, unknown>>();
  pushUniqueRecord(records, seen, item as unknown as Record<string, unknown>);
  pushUniqueRecord(records, seen, asRecord(item.metadata));

  for (let i = 0; i < records.length; i += 1) {
    const record = records[i];
    if (!record) continue;
    for (const key of NESTED_METADATA_KEYS) {
      pushUniqueRecord(records, seen, asRecord(record[key]));
    }
  }
  return records;
}

function nestedRecordsForKeys(
  item: LightboxItem,
  keys: readonly string[],
): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  const seen = new Set<Record<string, unknown>>();
  for (const source of collectRecordSources(item)) {
    for (const key of keys) {
      pushUniqueRecord(records, seen, asRecord(source[key]));
    }
  }
  return records;
}

function topLevelParamRecords(item: LightboxItem): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = [];
  const seen = new Set<Record<string, unknown>>();
  pushUniqueRecord(records, seen, item as unknown as Record<string, unknown>);
  pushUniqueRecord(records, seen, asRecord(item.metadata));
  return records;
}

function requestParamRecords(item: LightboxItem): Record<string, unknown>[] {
  return [
    ...nestedRecordsForKeys(item, REQUEST_PARAM_RECORD_KEYS),
    ...topLevelParamRecords(item),
  ];
}

function effectiveParamRecords(item: LightboxItem): Record<string, unknown>[] {
  return [
    ...nestedRecordsForKeys(item, EFFECTIVE_PARAM_RECORD_KEYS),
    ...topLevelParamRecords(item),
  ];
}

function readField(
  sources: Record<string, unknown>[],
  keys: readonly string[],
): ParamRead {
  for (const source of sources) {
    for (const key of keys) {
      if (hasOwn(source, key)) {
        return { found: true, value: source[key] };
      }
    }
  }
  return { found: false, value: undefined };
}

function firstTextFromSources(
  sources: Record<string, unknown>[],
  keys: readonly string[],
): string | null {
  const read = readField(sources, keys);
  if (typeof read.value === "string" && read.value.trim()) {
    return read.value.trim();
  }
  if (typeof read.value === "number" && Number.isFinite(read.value)) {
    return String(read.value);
  }
  return null;
}

function firstNumberFromSources(
  sources: Record<string, unknown>[],
  keys: readonly string[],
): number | null {
  const read = readField(sources, keys);
  if (typeof read.value === "number" && Number.isFinite(read.value)) {
    return read.value;
  }
  if (typeof read.value === "string" && read.value.trim()) {
    const parsed = Number(read.value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function firstBooleanFromSources(
  sources: Record<string, unknown>[],
  keys: readonly string[],
): boolean | null {
  const read = readField(sources, keys);
  if (typeof read.value === "boolean") return read.value;
  if (typeof read.value === "string") {
    const normalized = read.value.trim().toLowerCase();
    if (["true", "yes", "1", "enabled"].includes(normalized)) return true;
    if (["false", "no", "0", "disabled"].includes(normalized)) return false;
  }
  return null;
}

function firstArrayFromSources(
  sources: Record<string, unknown>[],
  keys: readonly string[],
): unknown[] | null {
  const read = readField(sources, keys);
  return Array.isArray(read.value) ? read.value : null;
}

function formatParamValue(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? String(value) : null;
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : null;
  return null;
}

function normalizeParamComparison(value: unknown): string | null {
  const formatted = formatParamValue(value);
  if (!formatted) return null;
  return formatted
    .trim()
    .toLowerCase()
    .replace(/^image\//, "")
    .replace(/\s+/g, " ");
}

function formatDurationMs(ms: number): string | null {
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 1000)} s`;
}

function formatProviderAttempt(value: unknown): {
  provider: string | null;
  status: string | null;
} | null {
  if (typeof value === "string" && value.trim()) {
    return { provider: value.trim(), status: null };
  }
  const record = asRecord(value);
  if (!record) return null;
  const provider = firstTextFromSources([record], [
    "provider",
    "provider_name",
    "name",
    "upstream_provider",
    "actual_provider",
  ]);
  const status = firstTextFromSources([record], ["status", "outcome", "state"]);
  if (!provider && !status) return null;
  return { provider, status };
}

function providerAttempts(item: LightboxItem): Array<{
  provider: string | null;
  status: string | null;
}> {
  const raw = firstArrayFromSources(collectRecordSources(item), [
    "provider_attempts",
    "attempts",
    "providers_attempted",
  ]);
  if (!raw) return [];
  return raw
    .map(formatProviderAttempt)
    .filter((attempt): attempt is { provider: string | null; status: string | null } =>
      Boolean(attempt),
    );
}

function lastSuccessfulProvider(
  attempts: Array<{ provider: string | null; status: string | null }>,
): string | null {
  for (let i = attempts.length - 1; i >= 0; i -= 1) {
    const attempt = attempts[i];
    const status = attempt?.status?.toLowerCase() ?? "";
    if (
      attempt?.provider &&
      (status.includes("success") ||
        status.includes("succeeded") ||
        status.includes("ok") ||
        status.includes("used"))
    ) {
      return attempt.provider;
    }
  }
  return attempts.at(-1)?.provider ?? null;
}

export function getLightboxRevisedPrompt(item: LightboxItem): string | null {
  return firstTextFromSources(collectRecordSources(item), [
    "revised_prompt",
    "revisedPrompt",
    "model_revised_prompt",
  ]);
}

export function extensionFromMime(mime: string | null | undefined): string | null {
  if (!mime) return null;
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (!normalized) return null;
  const imagePrefix = "image/";
  const ext = normalized.startsWith(imagePrefix)
    ? normalized.slice(imagePrefix.length)
    : normalized.split("/")[1];
  if (!ext) return null;
  if (ext === "jpeg" || ext === "pjpeg") return "jpg";
  if (ext === "svg+xml") return "svg";
  return ext.replace(/[^a-z0-9]+/g, "");
}

export function extensionFromSrc(
  src: string | null | undefined,
  baseUrl = FALLBACK_URL_BASE,
): string | null {
  if (!src) return null;
  if (src.startsWith("data:")) {
    const mimeMatch = src.match(/^data:([^;,]+)[;,]/);
    return extensionFromMime(mimeMatch?.[1]);
  }
  try {
    const pathname = new URL(src, baseUrl).pathname;
    const match = pathname.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  } catch {
    const match = src.split("?")[0]?.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  }
}

export function getLightboxMimeType(
  item: Pick<LightboxItem, "mime" | "mime_type" | "content_type" | "type">,
): string | null {
  const value = item.mime ?? item.mime_type ?? item.content_type ?? item.type;
  if (!hasText(value)) return null;
  return value.includes("/") ? value : null;
}

export function inferLightboxFileExtension(item: LightboxItem): string {
  return (
    extensionFromMime(getLightboxMimeType(item)) ??
    extensionFromSrc(item.filename ?? item.file_name) ??
    extensionFromSrc(item.url) ??
    extensionFromSrc(item.previewUrl) ??
    "png"
  );
}

export function getLightboxDownloadFilename(item: LightboxItem): string {
  const providedName = item.filename ?? item.file_name;
  if (hasText(providedName) && /\.[a-z0-9]+$/i.test(providedName)) {
    return providedName;
  }
  const ext = inferLightboxFileExtension(item);
  const base = hasText(providedName) ? providedName : `lumen-${item.id || "image"}`;
  return `${base}.${ext}`;
}

export function formatImageDimensions(
  item: Pick<LightboxItem, "size_actual" | "width" | "height">,
): string | null {
  if (hasText(item.size_actual)) return item.size_actual;
  if (
    typeof item.width === "number" &&
    typeof item.height === "number" &&
    item.width > 0 &&
    item.height > 0
  ) {
    return `${item.width} x ${item.height}`;
  }
  return null;
}

export function formatLightboxDate(
  value: string | number | Date | null | undefined,
  locale = "zh-CN",
): string | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function buildCompactLightboxMetadata(item: LightboxItem): string[] {
  return [
    item.aspect_ratio ? `比例 ${item.aspect_ratio}` : null,
    formatImageDimensions(item),
    item.seed !== undefined && item.seed !== null ? `seed ${String(item.seed)}` : null,
    item.quality ? `render ${item.quality}` : null,
    formatBooleanMode(item.fast),
    item.model ?? item.model_id ?? null,
  ].filter((value): value is string => Boolean(value));
}

export function buildLightboxMetadataSections(
  item: LightboxItem,
): LightboxMetadataSection[] {
  const dimensions = formatImageDimensions(item);
  const mime = getLightboxMimeType(item);
  const type = item.type && item.type !== mime ? item.type : null;
  const createdAt = formatLightboxDate(item.created_at);
  const effectiveParams = effectiveParamRecords(item);
  const renderQuality =
    formatParamValue(readField(effectiveParams, ["render_quality", "quality"]).value) ??
    item.quality ??
    item.render_quality ??
    null;
  const outputFormat = formatParamValue(
    readField(effectiveParams, ["output_format", "format", "image_job_format"]).value,
  );
  const outputCompression = formatParamValue(
    readField(effectiveParams, ["output_compression", "compression"]).value,
  );
  const background = formatParamValue(
    readField(effectiveParams, ["background"]).value,
  );
  const moderation = formatParamValue(
    readField(effectiveParams, ["moderation"]).value,
  );
  const rows = {
    generation: [
      dimensions ? { label: "尺寸", value: dimensions } : null,
      item.aspect_ratio ? { label: "比例", value: item.aspect_ratio } : null,
      item.seed !== undefined && item.seed !== null
        ? { label: "Seed", value: String(item.seed) }
        : null,
      renderQuality ? { label: "渲染", value: renderQuality } : null,
      outputFormat ? { label: "格式", value: outputFormat } : null,
      outputCompression ? { label: "压缩", value: outputCompression } : null,
      background ? { label: "背景", value: background } : null,
      moderation ? { label: "审核", value: moderation } : null,
      formatBooleanMode(item.fast)
        ? { label: "模式", value: formatBooleanMode(item.fast) as string }
        : null,
      item.model ?? item.model_id
        ? { label: "模型", value: item.model ?? item.model_id ?? "" }
        : null,
    ],
    diff: buildParamDiffRows(item),
    runtime: buildRuntimeRows(item),
    file: [
      mime ? { label: "MIME", value: mime } : null,
      type ? { label: "类型", value: type } : null,
      { label: "扩展名", value: inferLightboxFileExtension(item) },
    ],
    record: [
      createdAt ? { label: "创建时间", value: createdAt } : null,
      { label: "ID", value: item.id },
    ],
  };

  return [
    { title: "生成参数", rows: compactRows(rows.generation) },
    { title: "参数差异", rows: rows.diff },
    { title: "运行信息", rows: rows.runtime },
    { title: "文件信息", rows: compactRows(rows.file) },
    { title: "记录", rows: compactRows(rows.record) },
  ].filter((section) => section.rows.length > 0);
}

function buildParamDiffRows(item: LightboxItem): LightboxMetadataRow[] {
  const requested = requestParamRecords(item);
  const effective = effectiveParamRecords(item);
  const effectiveOutputFormat = readField(effective, [
    "output_format",
    "format",
    "image_job_format",
  ]);
  const effectiveOutputFormatNormalized = normalizeParamComparison(
    effectiveOutputFormat.value,
  );
  const definitions: Array<{
    label: string;
    requestKeys: readonly string[];
    effectiveKeys: readonly string[];
  }> = [
    {
      label: "尺寸",
      requestKeys: ["size_requested", "requested_size", "size", "fixed_size"],
      effectiveKeys: ["size_actual", "actual_size", "resolved_size", "size"],
    },
    {
      label: "渲染",
      requestKeys: ["render_quality", "quality"],
      effectiveKeys: ["render_quality", "quality"],
    },
    {
      label: "格式",
      requestKeys: ["output_format", "format"],
      effectiveKeys: ["output_format", "format", "image_job_format"],
    },
    {
      label: "压缩",
      requestKeys: ["output_compression", "compression"],
      effectiveKeys: ["output_compression", "compression"],
    },
    {
      label: "背景",
      requestKeys: ["background"],
      effectiveKeys: ["background"],
    },
    {
      label: "审核",
      requestKeys: ["moderation"],
      effectiveKeys: ["moderation"],
    },
  ];

  const rows: LightboxMetadataRow[] = [];
  for (const definition of definitions) {
    const requestedValue = readField(requested, definition.requestKeys);
    const effectiveValue = readField(effective, definition.effectiveKeys);
    const requestedLabel = formatParamValue(requestedValue.value);
    let effectiveLabel = formatParamValue(effectiveValue.value);

    if (
      definition.label === "压缩" &&
      requestedValue.found &&
      !effectiveValue.found &&
      effectiveOutputFormatNormalized === "png"
    ) {
      effectiveLabel = "未发送";
    }

    if (!requestedValue.found || !requestedLabel || !effectiveLabel) continue;
    const same =
      normalizeParamComparison(requestedValue.value) ===
      normalizeParamComparison(effectiveValue.value);
    if (same) continue;
    rows.push({
      label: definition.label,
      value: `${requestedLabel} → ${effectiveLabel}`,
      badge: "已自动调整",
    });
  }
  return rows;
}

function buildRuntimeRows(item: LightboxItem): LightboxMetadataRow[] {
  const sources = collectRecordSources(item);
  const attempts = providerAttempts(item);
  const firstProvider =
    firstTextFromSources(sources, [
      "initial_provider",
      "first_provider",
      "requested_provider",
      "provider_initial",
    ]) ??
    attempts[0]?.provider ??
    null;
  const actualProvider =
    firstTextFromSources(sources, [
      "actual_provider",
      "upstream_provider",
      "successful_provider",
      "selected_provider",
      "provider_name",
      "provider",
    ]) ?? lastSuccessfulProvider(attempts);
  const route = firstTextFromSources(sources, [
    "actual_route",
    "upstream_route",
    "route",
    "image_route",
  ]);
  const endpoint = firstTextFromSources(sources, [
    "actual_endpoint",
    "upstream_endpoint",
    "endpoint",
    "image_job_endpoint_used",
  ]);
  const proxyName = firstTextFromSources(sources, [
    "proxy_name",
    "proxy",
    "proxy_used",
    "egress_proxy",
  ]);
  const proxyEnabled = firstBooleanFromSources(sources, [
    "proxy_enabled",
    "using_proxy",
    "proxy_used",
  ]);
  const durationMs =
    firstNumberFromSources(sources, [
      "upstream_duration_ms",
      "duration_ms",
      "elapsed_ms",
    ]) ??
    (() => {
      const seconds = firstNumberFromSources(sources, [
        "upstream_duration_seconds",
        "duration_seconds",
      ]);
      return seconds !== null ? seconds * 1000 : null;
    })();
  const failoverCount =
    firstNumberFromSources(sources, ["failover_count", "provider_failover_count"]) ??
    (attempts.length > 1 ? attempts.length - 1 : null);
  const failoverValue =
    failoverCount !== null && failoverCount > 0
      ? `是 · ${Math.round(failoverCount)} 次`
      : firstBooleanFromSources(sources, ["failover", "provider_failover"]) === true
        ? "是"
        : firstBooleanFromSources(sources, ["failover", "provider_failover"]) === false
          ? "否"
          : null;
  const debugId = firstTextFromSources(sources, [
    "debug_id",
    "trace_id",
    "request_id",
    "image_job_id",
    "generation_id",
  ]);
  const safeError = firstTextFromSources(sources, [
    "safe_error_summary",
    "upstream_error_summary",
    "error_summary",
    "failure_summary",
  ]);
  const attemptChain = attempts
    .map((attempt) => attempt.provider)
    .filter((provider): provider is string => Boolean(provider))
    .join(" → ");

  return compactRows([
    actualProvider ? { label: "Provider", value: actualProvider } : null,
    firstProvider && actualProvider && firstProvider !== actualProvider
      ? { label: "首次尝试", value: firstProvider }
      : null,
    attemptChain && attemptChain !== actualProvider
      ? { label: "尝试链路", value: attemptChain }
      : null,
    route ? { label: "路由", value: route } : null,
    endpoint ? { label: "端点", value: endpoint } : null,
    proxyName
      ? { label: "代理", value: `已启用 · ${proxyName}` }
      : proxyEnabled !== null
        ? { label: "代理", value: proxyEnabled ? "已启用" : "未启用" }
        : null,
    durationMs !== null && formatDurationMs(durationMs)
      ? { label: "耗时", value: formatDurationMs(durationMs) as string }
      : null,
    failoverValue ? { label: "Failover", value: failoverValue } : null,
    debugId ? { label: "Debug ID", value: debugId } : null,
    safeError ? { label: "错误摘要", value: safeError } : null,
  ]);
}

function compactRows(
  rows: Array<LightboxMetadataRow | null>,
): LightboxMetadataRow[] {
  return rows.filter((row): row is LightboxMetadataRow => Boolean(row?.value));
}

export async function fetchImageBlob(src: string): Promise<Blob> {
  const response = src.startsWith("data:")
    ? await fetch(src)
    : await fetch(src, { credentials: "include" });
  if (!response.ok) {
    throw new Error(`Image download failed: ${response.status}`);
  }
  return response.blob();
}

// 触发"另存为"行为：data: 直接走 a.download；http(s) 先 fetch 成 Blob
// 再用 ObjectURL，避免浏览器把 image/* 直接打开预览。
export async function triggerImageDownload(
  src: string,
  filename: string,
): Promise<void> {
  if (typeof document === "undefined") return;
  if (src.startsWith("data:")) {
    const a = document.createElement("a");
    a.href = src;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    return;
  }
  const blob = await fetchImageBlob(src);
  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
