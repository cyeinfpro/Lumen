import { format } from "date-fns";

import { OPEN_EVENT, type LightboxItem } from "@/components/ui/lightbox/types";
import type {
  AdminRequestEventImageOut,
  AdminRequestEventLiveLane,
  AdminRequestEventOut,
} from "@/lib/types";
import type {
  EventKindFilter,
  RequestEventModelStat,
  StatusFilter,
  TimeRangeFilter,
} from "./RequestEventsHeader";

export const EMPTY_REQUEST_EVENTS: AdminRequestEventOut[] = [];
export const EMPTY_MODEL_STATS: RequestEventModelStat[] = [];
export const STATUS_META: Record<
  string,
  { label: string; badge: string; dot: string; row: string }
> = {
  queued: {
    label: "排队",
    badge: "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border)]",
    dot: "bg-[var(--fg-2)]",
    row: "border-l-[var(--border)]",
  },
  running: {
    label: "生成中",
    badge: "bg-info-soft text-info border-info-border",
    dot: "bg-info",
    row: "border-l-info/55",
  },
  streaming: {
    label: "回复中",
    badge: "bg-info-soft text-info border-info-border",
    dot: "bg-info",
    row: "border-l-info/55",
  },
  succeeded: {
    label: "成功",
    badge: "bg-success-soft text-success border-success-border",
    dot: "bg-success",
    row: "border-l-success/55",
  },
  failed: {
    label: "失败",
    badge: "bg-danger-soft text-danger border-danger-border",
    dot: "bg-danger",
    row: "border-l-danger/55",
  },
  canceled: {
    label: "已取消",
    badge: "bg-[var(--fg-2)]/10 text-[var(--fg-1)] border-[var(--border)]",
    dot: "bg-[var(--fg-2)]",
    row: "border-l-neutral-500/50",
  },
};

export function getStatusMeta(status: string) {
  return (
    STATUS_META[status] ?? {
      label: status || "未知",
      badge: "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border)]",
      dot: "bg-[var(--fg-2)]",
      row: "border-l-[var(--border)]",
    }
  );
}

export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return format(date, "yyyy-MM-dd HH:mm:ss");
  } catch {
    return value;
  }
}

export function formatAge(value: string | null): string {
  if (!value) return "时间未知";
  const created = new Date(value).getTime();
  if (!Number.isFinite(created)) return "时间未知";
  const diff = Math.max(0, Date.now() - created);
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

export function formatDuration(ms: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s`;
}

export function formatPixels(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value <= 0) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MP`;
  if (value >= 1000) return `${Math.round(value / 1000)} Kpx`;
  return `${value} px`;
}

export function eventKindLabel(event: AdminRequestEventOut): string {
  if (event.kind === "generation") {
    return event.action === "edit" ? "图生图" : "文生图";
  }
  return event.intent === "vision_qa" ? "视觉问答" : "对话";
}

export function statusLabel(status: string): string {
  return getStatusMeta(status).label;
}

export function imageRoleLabel(image: AdminRequestEventImageOut): string {
  if (image.roles.includes("output") && image.roles.includes("input")) {
    return "输入/输出";
  }
  if (image.roles.includes("output")) return "输出";
  return "参考";
}

export function imagePreviewSrc(image: AdminRequestEventImageOut): string {
  return image.thumb_url || image.preview_url || image.display_url || image.url;
}

export function previewImagesForEvent(
  event: AdminRequestEventOut,
  max = 3,
): AdminRequestEventImageOut[] {
  return [...event.images]
    .sort((a, b) => {
      const aOutput = a.roles.includes("output") ? 0 : 1;
      const bOutput = b.roles.includes("output") ? 0 : 1;
      return aOutput - bOutput;
    })
    .slice(0, max);
}

export function positiveDimension(value: number | null | undefined): number | undefined {
  return Number.isFinite(value) && value != null && value > 0 ? value : undefined;
}

export function imageSizeLabel(
  width: number | undefined,
  height: number | undefined,
): string | undefined {
  return width && height ? `${width}x${height}` : undefined;
}

export function toLightboxItem(
  image: AdminRequestEventImageOut,
  event: AdminRequestEventOut,
): LightboxItem {
  const previewUrl = image.display_url || image.preview_url || image.url;
  const thumbUrl =
    image.thumb_url || image.preview_url || image.display_url || image.url;
  const width = positiveDimension(image.width);
  const height = positiveDimension(image.height);
  return {
    id: image.id,
    url: image.url,
    previewUrl,
    thumbUrl,
    prompt: event.prompt || event.conversation_title || event.id,
    width,
    height,
    size_actual: imageSizeLabel(width, height),
    model: event.model || undefined,
    mime: image.mime || undefined,
    type: image.source,
    created_at: event.created_at,
    metadata: {
      role: imageRoleLabel(image),
      request_id: event.id,
      user: event.user_email,
      upstream: event.upstream_provider ?? undefined,
    },
  };
}

export function lightboxItemsForEvent(event: AdminRequestEventOut): LightboxItem[] {
  const seen = new Set<string>();
  const items: LightboxItem[] = [];
  for (const image of event.images) {
    if (!image.id || !image.url) continue;
    const key = `${image.id}:${image.url}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(toLightboxItem(image, event));
  }
  return items;
}

export function openEventImages(event: AdminRequestEventOut, initialImageId?: string) {
  if (typeof window === "undefined" || event.images.length === 0) return;
  const items = lightboxItemsForEvent(event);
  if (items.length === 0) return;
  const initialId =
    initialImageId && items.some((item) => item.id === initialImageId)
      ? initialImageId
      : items[0].id;
  window.dispatchEvent(
    new CustomEvent(OPEN_EVENT, {
      detail: { items, initialId },
    }),
  );
}

export function truncateMiddle(value: string, max = 16): string {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function liveLanes(event: AdminRequestEventOut): AdminRequestEventLiveLane[] {
  return event.live_lanes ?? [];
}

export function matchesSearch(event: AdminRequestEventOut, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    event.id,
    event.message_id,
    event.conversation_id,
    event.user_email,
    event.model,
    event.upstream_provider,
    event.upstream_route,
    event.upstream_endpoint,
    event.queue_lane,
    event.workflow_type,
    event.workflow_step_key,
    event.size_bucket,
    event.cost_class,
    upstreamText(event, "source"),
    upstreamText(event, "action_source"),
    upstreamText(event, "actual_source"),
    event.conversation_title,
    event.prompt,
    event.error_code,
    event.error_message,
    eventKindLabel(event),
    statusLabel(event.status),
    event.live_provider,
    ...liveLanes(event).flatMap((lane) => [lane.provider, lane.last_failed]),
    ...event.images.map((image) => image.id),
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(q));
}

export function displayValue(value: string | null | undefined, fallback = "—"): string {
  const text = typeof value === "string" ? value.trim() : "";
  return text || fallback;
}

export function upstreamText(event: AdminRequestEventOut, key: string): string | null {
  const value = event.upstream?.[key];
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text || null;
}

export function upstreamSource(event: AdminRequestEventOut): string | null {
  return upstreamText(event, "source") ?? upstreamText(event, "actual_source");
}

export function isActiveStatus(status: string): boolean {
  return status === "queued" || status === "running" || status === "streaming";
}

export function providerDisplayValue(event: AdminRequestEventOut): string {
  // 优先用 worker 实时写入 Redis 的 live_provider 快照——in-flight 期间能看到当前
  // 真在请求的 provider；dual_race 形如 "A vs B"；切号瞬间显示 "切换中"。
  if (isActiveStatus(event.status)) {
    const live = displayValue(event.live_provider ?? null, "");
    if (live) return live;
  }
  const provider = displayValue(event.upstream_provider, "");
  if (provider) return provider;
  if (event.kind === "completion") {
    return isActiveStatus(event.status) ? "等待上游结果" : "历史未记录";
  }
  if (event.upstream_route === "dual_race") {
    return isActiveStatus(event.status) ? "等待上游结果" : "历史未记录";
  }
  return isActiveStatus(event.status) ? "等待上游结果" : "未记录";
}

export function formatUnknownValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value || "—";
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function outputImageCount(event: AdminRequestEventOut): number {
  return event.images.filter((image) => image.roles.includes("output")).length;
}

export function modelStatLabel(model: string): string {
  const normalized = model.trim();
  if (
    normalized === "5.4" ||
    normalized === "5.4 mini" ||
    normalized === "5.4mini" ||
    normalized === "gpt-5.4" ||
    normalized === "gpt-5.4-mini"
  ) {
    return "Codex 原生";
  }
  if (normalized === "image2" || normalized === "gpt-image-2") {
    return "image2 直连";
  }
  return normalized || "未记录";
}

export function summarizeModelStats(
  events: AdminRequestEventOut[],
): RequestEventModelStat[] {
  const counts = new Map<string, number>();
  for (const event of events) {
    const model = modelStatLabel(displayValue(event.model, "未记录"));
    counts.set(model, (counts.get(model) ?? 0) + 1);
  }

  const total = events.length;
  if (total === 0) return [];

  return Array.from(counts.entries())
    .map(([model, count]) => ({
      model,
      count,
      share: count / total,
    }))
    .sort((a, b) => b.count - a.count || a.model.localeCompare(b.model));
}

export function summarizeEvents(events: AdminRequestEventOut[]) {
  let active = 0;
  let failed = 0;
  let succeeded = 0;
  let images = 0;
  let latestMs = 0;
  let completedDurationTotal = 0;
  let completedDurationCount = 0;

  for (const event of events) {
    if (
      event.status === "running" ||
      event.status === "streaming" ||
      event.status === "queued"
    ) {
      active += 1;
    }
    if (event.status === "failed") failed += 1;
    if (event.status === "succeeded") succeeded += 1;
    images += event.images.length;

    const latestValue = event.finished_at ?? event.created_at;
    const latestEventMs = new Date(latestValue).getTime();
    if (Number.isFinite(latestEventMs)) {
      latestMs = Math.max(latestMs, latestEventMs);
    }
    if (
      event.duration_ms != null &&
      Number.isFinite(event.duration_ms) &&
      event.duration_ms >= 0 &&
      event.status === "succeeded"
    ) {
      completedDurationTotal += event.duration_ms;
      completedDurationCount += 1;
    }
  }

  return {
    active,
    failed,
    succeeded,
    images,
    latestAt: latestMs > 0 ? new Date(latestMs).toISOString() : null,
    avgDurationMs:
      completedDurationCount > 0
        ? Math.round(completedDurationTotal / completedDurationCount)
        : null,
  };
}

export function requestEventStatus(status: StatusFilter): string | undefined {
  return status === "all" ? undefined : status;
}

export function requestEventRefreshInterval(autoRefresh: boolean): number | false {
  return autoRefresh ? 10_000 : false;
}

export function requestEventModelStats(
  hasSearch: boolean,
  filtered: AdminRequestEventOut[],
  fetched: RequestEventModelStat[] | undefined,
): RequestEventModelStat[] {
  if (hasSearch) return summarizeModelStats(filtered);
  return fetched ?? EMPTY_MODEL_STATS;
}

export function hasRequestEventFilters(
  kind: EventKindFilter,
  status: StatusFilter,
  range: TimeRangeFilter,
  search: string,
): boolean {
  return (
    kind !== "all" ||
    status !== "all" ||
    range !== "24h" ||
    search.trim().length > 0
  );
}
