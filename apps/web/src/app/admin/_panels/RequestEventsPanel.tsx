"use client";

/* eslint-disable @next/next/no-img-element -- Admin request event thumbnails use authenticated API URLs. */

import {
  Fragment,
  type ReactNode,
  useDeferredValue,
  useMemo,
  useState,
} from "react";
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Eye,
  Image as ImageIcon,
} from "lucide-react";

import { useAdminRequestEventsQuery } from "@/lib/queries";
import type {
  AdminRequestEventLiveLane,
  AdminRequestEventOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  EmptyBlock,
  ErrorBlock,
  ListSkeleton,
} from "../_components/AdminFeedback";
import {
  RequestEventsHeader,
  type EventKindFilter,
  type StatusFilter,
  type TimeRangeFilter,
} from "./request-events/RequestEventsHeader";

import {
  EMPTY_REQUEST_EVENTS,
  getStatusMeta,
  formatDateTime,
  formatAge,
  formatDuration,
  formatPixels,
  eventKindLabel,
  imageRoleLabel,
  imagePreviewSrc,
  previewImagesForEvent,
  lightboxItemsForEvent,
  openEventImages,
  truncateMiddle,
  liveLanes,
  matchesSearch,
  displayValue,
  upstreamText,
  upstreamSource,
  isActiveStatus,
  providerDisplayValue,
  formatUnknownValue,
  outputImageCount,
  summarizeEvents,
  requestEventStatus,
  requestEventRefreshInterval,
  requestEventModelStats,
  hasRequestEventFilters,
} from "./request-events/RequestEventDomain";

export function RequestEventsPanel() {
  const [kind, setKind] = useState<EventKindFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [range, setRange] = useState<TimeRangeFilter>("24h");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [search, setSearch] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const deferredSearch = useDeferredValue(search);

  const q = useAdminRequestEventsQuery(
    {
      limit: 120,
      kind,
      status: requestEventStatus(status),
      range,
    },
    {
      refetchInterval: requestEventRefreshInterval(autoRefresh),
    },
  );

  const rows = q.data?.items ?? EMPTY_REQUEST_EVENTS;
  const filtered = useMemo(
    () => rows.filter((event) => matchesSearch(event, deferredSearch)),
    [rows, deferredSearch],
  );
  const summary = useMemo(() => summarizeEvents(filtered), [filtered]);
  const fetchedSummary = useMemo(() => summarizeEvents(rows), [rows]);
  const hasSearch = deferredSearch.trim().length > 0;
  const modelStats = useMemo(
    () => requestEventModelStats(hasSearch, filtered, q.data?.model_stats),
    [filtered, hasSearch, q.data?.model_stats],
  );
  const modelStatsTotal = useMemo(
    () => modelStats.reduce((total, stat) => total + stat.count, 0),
    [modelStats],
  );
  const hasActiveFilters = hasRequestEventFilters(
    kind,
    status,
    range,
    search,
  );
  const resetFilters = () => {
    setKind("all");
    setStatus("all");
    setRange("24h");
    setSearch("");
  };

  return (
    <section className="space-y-4" aria-labelledby="request-events-title">
      <RequestEventsHeader
        rowsCount={rows.length}
        filteredCount={filtered.length}
        latestLabel={formatAge(fetchedSummary.latestAt)}
        averageDurationLabel={formatDuration(summary.avgDurationMs)}
        summary={summary}
        kind={kind}
        status={status}
        range={range}
        autoRefresh={autoRefresh}
        search={search}
        hasActiveFilters={hasActiveFilters}
        modelStats={modelStats}
        modelStatsTotal={modelStatsTotal}
        hasSearch={hasSearch}
        fetching={q.isFetching}
        loading={q.isLoading}
        onSearch={setSearch}
        onClearSearch={() => setSearch("")}
        onKindChange={setKind}
        onStatusChange={setStatus}
        onRangeChange={setRange}
        onToggleAutoRefresh={() => setAutoRefresh((value) => !value)}
        onReset={resetFilters}
        onRefresh={() => void q.refetch()}
      />

      <div
        className="overflow-hidden rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/70 backdrop-blur-sm"
        aria-busy={q.isFetching}
        aria-live="polite"
      >
        <RequestEventsResultState
          loading={q.isLoading}
          errorMessage={q.isError ? q.error?.message ?? "未知错误" : null}
          rowCount={rows.length}
          filteredCount={filtered.length}
          onRetry={() => void q.refetch()}
        >
          <>
            <div className="hidden overflow-x-auto [-webkit-overflow-scrolling:touch] lg:block">
              <table className="w-full min-w-[1040px] text-sm">
                <thead className="sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--bg-1)]/95 text-xs uppercase tracking-wider text-[var(--fg-1)] backdrop-blur">
                  <tr>
                    <th className="w-9 py-3 px-3" />
                    <th className="text-left py-3 px-3 font-medium">结束时间</th>
                    <th className="text-left py-3 px-3 font-medium">事件</th>
                    <th className="text-left py-3 px-3 font-medium">模型</th>
                    <th className="text-left py-3 px-3 font-medium">上游</th>
                    <th className="text-left py-3 px-3 font-medium">用户</th>
                    <th className="text-left py-3 px-3 font-medium">图片</th>
                    <th className="text-right py-3 px-3 font-medium">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((event) => {
                    const expanded = expandedId === event.id;
                    const detailId = `request-event-detail-${event.id}`;
                    const statusMeta = getStatusMeta(event.status);
                    return (
                      <Fragment key={event.id}>
                        <tr
                          className={cn(
                            "border-t border-l-2 border-[var(--border-subtle)] align-top transition-colors hover:bg-[var(--bg-2)]",
                            statusMeta.row,
                            expanded && "bg-[var(--bg-2)]",
                          )}
                        >
                          <td className="py-3 px-3">
                            <button
                              type="button"
                              onClick={() =>
                                setExpandedId(expanded ? null : event.id)
                              }
                              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-[var(--radius-card)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] sm:h-8 sm:w-8 sm:min-h-0 sm:min-w-0 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25"
                              aria-label={expanded ? "收起详情" : "展开详情"}
                              aria-expanded={expanded}
                              aria-controls={detailId}
                            >
                              {expanded ? (
                                <ChevronDown className="w-4 h-4" />
                              ) : (
                                <ChevronRight className="w-4 h-4" />
                              )}
                            </button>
                          </td>
                          <td className="py-3 px-3 font-mono text-xs tabular-nums whitespace-nowrap text-[var(--fg-1)]">
                            {formatDateTime(event.finished_at)}
                          </td>
                          <td className="py-3 px-3">
                            <div className="flex flex-col gap-1">
                              <span className="text-[var(--fg-0)]">
                                {eventKindLabel(event)}
                              </span>
                              <StatusBadge status={event.status} />
                            </div>
                          </td>
                          <td className="py-3 px-3 text-[var(--fg-1)] max-w-[220px]">
                            <span
                              className="line-clamp-2 break-words"
                              title={event.model}
                            >
                              {displayValue(event.model)}
                            </span>
                          </td>
                          <td className="py-3 px-3">
                            <ProviderCell event={event} />
                          </td>
                          <td className="py-3 px-3 text-[var(--fg-1)] max-w-[210px]">
                            <span
                              className="line-clamp-2 break-all"
                              title={event.user_email}
                            >
                              {displayValue(event.user_email)}
                            </span>
                          </td>
                          <td className="py-3 px-3">
                            <ImagesButton event={event} />
                          </td>
                          <td className="py-3 px-3 text-right font-mono text-xs tabular-nums text-[var(--fg-1)] whitespace-nowrap">
                            {formatDuration(event.duration_ms)}
                          </td>
                        </tr>
                        {expanded && (
                          <tr className="border-t border-[var(--border-subtle)]" id={detailId}>
                            <td colSpan={8} className="px-4 pb-5 pt-2">
                              <EventDetails event={event} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <ul className="divide-y divide-white/5 lg:hidden">
              {filtered.map((event) => {
                const expanded = expandedId === event.id;
                const detailId = `request-event-mobile-detail-${event.id}`;
                const statusMeta = getStatusMeta(event.status);
                return (
                  <li
                    key={event.id}
                    className={cn(
                      "space-y-3 border-l-2 p-4 lg:grid lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start lg:gap-4 lg:space-y-0",
                      statusMeta.row,
                      expanded && "bg-[var(--bg-2)]",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => setExpandedId(expanded ? null : event.id)}
                      className="w-full min-w-0 space-y-3 rounded-[var(--radius-panel)] text-left focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25"
                      aria-expanded={expanded}
                      aria-controls={detailId}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-[var(--fg-0)]">
                              {eventKindLabel(event)}
                            </span>
                            <StatusBadge status={event.status} />
                          </div>
                          <p className="mt-1 font-mono text-xs text-[var(--fg-2)] tabular-nums">
                            结束 {formatDateTime(event.finished_at)}
                          </p>
                        </div>
                        {expanded ? (
                          <ChevronDown className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
                        )}
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
                        <MiniField label="模型" value={displayValue(event.model)} />
                        <MiniField
                          label="上游"
                          value={providerDisplayValue(event)}
                        />
                        <MiniField label="用户" value={displayValue(event.user_email)} />
                        <MiniField
                          label="耗时"
                          value={formatDuration(event.duration_ms)}
                        />
                      </div>
                    </button>
                    <div className="lg:self-center lg:justify-self-end">
                      <ImagesButton event={event} />
                    </div>
                    {expanded && (
                      <div id={detailId} className="lg:col-span-2">
                        <EventDetails event={event} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        </RequestEventsResultState>
      </div>
    </section>
  );
}

function RequestEventsResultState({
  loading,
  errorMessage,
  rowCount,
  filteredCount,
  onRetry,
  children,
}: {
  loading: boolean;
  errorMessage: string | null;
  rowCount: number;
  filteredCount: number;
  onRetry: () => void;
  children: ReactNode;
}) {
  if (loading) return <ListSkeleton rows={7} />;
  if (errorMessage) {
    return <ErrorBlock message={errorMessage} onRetry={onRetry} />;
  }
  if (filteredCount === 0) {
    return (
      <EmptyBlock
        title={rowCount === 0 ? "暂无请求事件" : "没有匹配结果"}
        description={
          rowCount === 0
            ? "用户发起图片或对话请求后会出现在这里"
            : "试试切换过滤条件或换个关键词"
        }
      />
    );
  }
  return children;
}

function StatusBadge({ status }: { status: string }) {
  const meta = getStatusMeta(status);
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-[var(--radius-control)] border px-1.5 py-0.5 text-[11px]",
        meta.badge,
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
      {meta.label}
    </span>
  );
}

function ProviderCell({ event }: { event: AdminRequestEventOut }) {
  const route = displayValue(event.upstream_route || event.upstream_endpoint);
  const lanes = liveLanes(event);
  const isLive = isActiveStatus(event.status);
  const hasLiveLaneDetail =
    isLive && lanes.length > 0 && lanes.some((lane) => lane.provider || lane.last_failed);

  if (hasLiveLaneDetail) {
    return (
      <div className="flex max-w-[220px] flex-col gap-1 text-xs">
        {lanes.map((lane, idx) => (
          <LiveLaneRow key={`${lane.label}-${idx}`} lane={lane} />
        ))}
        <span className="truncate text-[var(--fg-2)]" title={route}>
          {route}
        </span>
      </div>
    );
  }

  const provider = providerDisplayValue(event);
  return (
    <div className="flex max-w-[190px] flex-col gap-1 text-xs">
      <span className="truncate text-[var(--fg-0)]" title={provider}>
        {provider}
      </span>
      <span className="truncate text-[var(--fg-2)]" title={route}>
        {route}
      </span>
    </div>
  );
}

function LiveLaneRow({ lane }: { lane: AdminRequestEventLiveLane }) {
  const isFailover = lane.status === "failover";
  const provider = lane.provider?.trim();
  const dotClass = isFailover
    ? "bg-warning animate-pulse"
    : provider
      ? "bg-success animate-pulse"
      : "bg-[var(--fg-3)]";
  const labelText = lane.label || "lane";
  let providerText: string;
  if (provider) {
    providerText = provider;
  } else if (isFailover && lane.last_failed) {
    providerText = `切换中 (上一个 ${lane.last_failed})`;
  } else {
    providerText = "等待中";
  }
  const tip = [
    `${labelText}: ${providerText}`,
    lane.route ? `路由=${lane.route}` : null,
    lane.endpoint ? `接口=${lane.endpoint}` : null,
  ]
    .filter(Boolean)
    .join(" • ");
  return (
    <span
      className="flex items-center gap-1.5 truncate text-[var(--fg-0)]"
      title={tip}
    >
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", dotClass)} />
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-[var(--fg-2)]">
        {labelText}
      </span>
      <span
        className={cn(
          "truncate",
          isFailover ? "text-warning" : "text-[var(--fg-1)]",
        )}
      >
        {providerText}
      </span>
    </span>
  );
}

function ImagesButton({ event }: { event: AdminRequestEventOut }) {
  if (event.images.length === 0) {
    return <span className="text-xs text-[var(--fg-2)]">—</span>;
  }
  const outputCount = outputImageCount(event);
  const canOpen = lightboxItemsForEvent(event).length > 0;
  const previews = previewImagesForEvent(event);
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        openEventImages(event);
      }}
      disabled={!canOpen}
      className="inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] px-2.5 text-xs text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-3)] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25"
      aria-label={`查看 ${event.images.length} 张事件图片`}
    >
      <span className="flex shrink-0 -space-x-1">
        {previews.map((image) => (
          <span
            key={image.id}
            className="h-7 w-7 overflow-hidden rounded-[var(--radius-control)] border border-black/40 bg-[var(--bg-2)] shadow-sm"
          >
            <img
              src={imagePreviewSrc(image)}
              alt=""
              loading="lazy"
              className="h-full w-full object-cover"
            />
          </span>
        ))}
      </span>
      <Eye className="w-3.5 h-3.5" />
      查看
      <span className="rounded bg-[var(--bg-3)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--fg-1)]">
        {event.images.length}
      </span>
      {outputCount > 0 && (
        <span className="rounded bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]">
          输出 {outputCount}
        </span>
      )}
    </button>
  );
}

function EventDetails({ event }: { event: AdminRequestEventOut }) {
  const upstreamEntries = Object.entries(event.upstream ?? {});
  const outputCount = outputImageCount(event);

  return (
    <div className="space-y-4 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-0)]/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border-subtle)] pb-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-[var(--fg-0)]">
            {eventKindLabel(event)}
          </span>
          <StatusBadge status={event.status} />
          {outputCount > 0 && (
            <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-[var(--accent)]/20 bg-[var(--accent)]/10 px-1.5 py-0.5 text-[11px] text-[var(--accent)]">
              <ImageIcon className="h-3 w-3" />
              输出 {outputCount}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--fg-2)]">
          <span className="font-mono tabular-nums">
            {formatDuration(event.duration_ms)}
          </span>
          <span className="font-mono tabular-nums">
            {formatAge(event.finished_at ?? event.created_at)}
          </span>
        </div>
      </div>

      <EventDetailGrid event={event} />

      {isActiveStatus(event.status) && liveLanes(event).length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            <Activity className="w-3.5 h-3.5" />
            实时供应商（任务心跳）
          </div>
          <div className="flex flex-col gap-1.5 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-3">
            {liveLanes(event).map((lane, idx) => (
              <LiveLaneRow key={`detail-${lane.label}-${idx}`} lane={lane} />
            ))}
          </div>
        </div>
      )}

      {event.prompt && (
        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            提示词
          </div>
          <p className="max-h-32 overflow-auto whitespace-pre-wrap rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-3 text-xs leading-relaxed text-[var(--fg-1)]">
            {event.prompt}
          </p>
        </div>
      )}

      {event.error_message && (
        <div>
          <div className="mb-1.5 type-overline text-danger">
            错误信息
          </div>
          <p className="max-h-28 overflow-auto whitespace-pre-wrap rounded-[var(--radius-control)] border border-danger-border bg-danger-soft p-3 text-xs leading-relaxed text-danger">
            {event.error_message}
          </p>
        </div>
      )}

      {event.images.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            <ImageIcon className="w-3.5 h-3.5" />
            图片文件
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
            {event.images.map((image, index) => (
              <button
                key={`${image.id}:${index}`}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  openEventImages(event, image.id);
                }}
                disabled={!image.url}
                className="group relative aspect-square overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] text-left transition-colors hover:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25"
              >
                <img
                  src={imagePreviewSrc(image)}
                  alt={imageRoleLabel(image)}
                  loading="lazy"
                  className="absolute inset-0 h-full w-full object-cover transition-transform duration-200 group-hover:scale-[1.02]"
                />
                <span className="absolute inset-x-0 bottom-0 flex min-w-0 items-center justify-between gap-2 bg-black/65 px-2 py-1.5 text-xs text-white backdrop-blur-sm">
                  <span className="shrink-0">{imageRoleLabel(image)}</span>
                  <span className="truncate font-mono text-white/75">
                    {image.width > 0 && image.height > 0
                      ? `${image.width}x${image.height}`
                      : "尺寸未知"}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {upstreamEntries.length > 0 && (
        <div>
          <div className="mb-2 text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
            上游参数
          </div>
          <div className="flex flex-wrap gap-2">
            {upstreamEntries.map(([key, value]) => (
              <span
                key={key}
                title={formatUnknownValue(value)}
                className="inline-flex max-w-full items-center gap-1 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-2 py-1 text-xs text-[var(--fg-1)]"
              >
                <span className="shrink-0 text-[var(--fg-2)]">{key}</span>
                <span className="truncate font-mono text-[var(--fg-1)]">
                  {formatUnknownValue(value)}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

interface EventDetailItem {
  label: string;
  value: string;
  mono?: boolean;
}

function eventDetailItems(event: AdminRequestEventOut): EventDetailItem[] {
  const conversationLabel = event.conversation_title
    ? `${event.conversation_title} (${truncateMiddle(event.conversation_id ?? "")})`
    : displayValue(event.conversation_id);
  const items: EventDetailItem[] = [
    { label: "请求编号", value: event.id, mono: true },
    { label: "消息编号", value: event.message_id, mono: true },
    { label: "会话", value: conversationLabel },
    { label: "阶段", value: displayValue(event.progress_stage) },
    { label: "创建时间", value: formatDateTime(event.created_at), mono: true },
    { label: "开始时间", value: formatDateTime(event.started_at), mono: true },
    { label: "结束时间", value: formatDateTime(event.finished_at), mono: true },
    { label: "队列泳道", value: displayValue(event.queue_lane, "未记录") },
    {
      label: "排队耗时",
      value: formatDuration(event.queue_wait_ms ?? null),
      mono: true,
    },
    { label: "尺寸桶", value: displayValue(event.size_bucket, "—") },
    { label: "像素量", value: formatPixels(event.pixel_count), mono: true },
    { label: "成本类型", value: displayValue(event.cost_class, "—") },
    { label: "上游端点", value: displayValue(event.upstream_endpoint) },
    {
      label: "上游路由",
      value: displayValue(event.upstream_route, "未记录"),
    },
    { label: "上游", value: providerDisplayValue(event) },
    { label: "尝试次数", value: String(event.attempt), mono: true },
  ];
  const source = upstreamSource(event);
  const actionSource = upstreamText(event, "action_source");
  if (source) items.push({ label: "来源", value: source });
  if (actionSource) items.push({ label: "动作来源", value: actionSource });
  if (event.workflow_type) {
    items.push({ label: "工作流", value: event.workflow_type });
  }
  if (event.workflow_step_key) {
    items.push({ label: "工作流步骤", value: event.workflow_step_key });
  }
  if (event.tokens_in != null) {
    items.push({
      label: "输入令牌数",
      value: String(event.tokens_in),
      mono: true,
    });
  }
  if (event.tokens_out != null) {
    items.push({
      label: "输出令牌数",
      value: String(event.tokens_out),
      mono: true,
    });
  }
  if (event.error_code) {
    items.push({ label: "错误码", value: event.error_code });
  }
  return items;
}

function EventDetailGrid({ event }: { event: AdminRequestEventOut }) {
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {eventDetailItems(event).map((item) => (
        <Detail
          key={item.label}
          label={item.label}
          value={item.value}
          mono={item.mono}
        />
      ))}
    </div>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
        {label}
      </div>
      <div
        className={cn(
          "mt-1 break-words text-xs text-[var(--fg-1)]",
          mono && "font-mono tabular-nums",
        )}
        title={value || "—"}
      >
        {value || "—"}
      </div>
    </div>
  );
}

function MiniField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
        {label}
      </div>
      <div
        className="mt-0.5 line-clamp-2 break-words text-xs text-[var(--fg-1)]"
        title={value || "—"}
      >
        {value || "—"}
      </div>
    </div>
  );
}
