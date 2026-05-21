"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CreditCard,
  MessageCircle,
  RefreshCw,
  Server,
  ShieldAlert,
  TimerReset,
  Wifi,
} from "lucide-react";

import {
  getAdminBillingOverview,
  getAdminContextHealth,
  getProviderStats,
  getSystemSettings,
  listAdminOrphanHolds,
  listAdminRequestEvents,
} from "@/lib/apiClient";
import { useAdminProxiesQuery, useProvidersQuery } from "@/lib/queries";
import { Button, Card } from "@/components/ui/primitives";
import { AdminUpdatePanel } from "./AdminUpdatePanel";

type HealthTargetTab =
  | "events"
  | "billing"
  | "providers"
  | "proxies"
  | "telegram"
  | "settings"
  | "storage";

type Tone = "ok" | "warn" | "danger" | "neutral";

interface HealthPanelProps {
  onOpenTab: (tab: HealthTargetTab) => void;
}

export function HealthPanel({ onOpenTab }: HealthPanelProps) {
  const providersQ = useProvidersQuery({ retry: false });
  const providerStatsQ = useQuery({
    queryKey: ["admin", "providers", "stats", "health"],
    queryFn: getProviderStats,
    retry: false,
  });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  const billingQ = useQuery({
    queryKey: ["admin", "billing", "overview", "health"],
    queryFn: getAdminBillingOverview,
    retry: false,
  });
  const orphanQ = useQuery({
    queryKey: ["admin", "billing", "orphan-holds", "health"],
    queryFn: () => listAdminOrphanHolds({ min_age_minutes: 60, limit: 5 }),
    retry: false,
  });
  const contextQ = useQuery({
    queryKey: ["admin", "context", "health", "dashboard"],
    queryFn: getAdminContextHealth,
    retry: false,
  });
  const settingsQ = useQuery({
    queryKey: ["admin", "settings", "health"],
    queryFn: getSystemSettings,
    retry: false,
  });
  const failedEventsQ = useQuery({
    queryKey: ["admin", "request-events", "failed", "24h", "health"],
    queryFn: () => listAdminRequestEvents({ limit: 6, status: "failed", range: "24h" }),
    retry: false,
  });

  const providerItems = providersQ.data?.items ?? [];
  const providerStats = providerStatsQ.data?.items ?? [];
  const enabledProviders = providerItems.filter((item) => item.enabled).length;
  const weakProviders = providerStats.filter(
    (item) => item.total >= 5 && item.success_rate < 0.9,
  );
  const enabledProxies = (proxiesQ.data?.items ?? []).filter((item) => item.enabled).length;
  const billing = billingQ.data;
  const context = contextQ.data;
  const settingMap = useMemo(
    () => new Map((settingsQ.data?.items ?? []).map((item) => [item.key, item])),
    [settingsQ.data?.items],
  );
  const telegramEnabled = settingValue(settingMap, "telegram.bot_enabled", "1") !== "0";
  const telegramTokenReady =
    Boolean(settingMap.get("telegram.bot_token")?.has_value) ||
    settingValue(settingMap, "telegram.bot_username", "") !== "";
  const failedEvents = failedEventsQ.data?.items ?? [];
  const orphanCount = orphanQ.data?.length ?? 0;

  const tiles = [
    {
      key: "providers",
      icon: <Server className="h-4 w-4" />,
      label: "Provider",
      value: providerStatsQ.isLoading || providersQ.isLoading ? "加载中" : `${enabledProviders} 个启用`,
      detail:
        weakProviders.length > 0
          ? `${weakProviders.length} 个近期成功率偏低`
          : "最近路由未发现明显异常",
      tone: weakProviders.length > 0 || enabledProviders === 0 ? "danger" : "ok",
      tab: "providers" as const,
    },
    {
      key: "proxies",
      icon: <Wifi className="h-4 w-4" />,
      label: "代理池",
      value: proxiesQ.isLoading ? "加载中" : `${enabledProxies} 个启用`,
      detail: enabledProxies > 0 ? "Telegram 与供应商可共用" : "未配置启用代理",
      tone: enabledProxies > 0 ? "ok" : "neutral",
      tab: "proxies" as const,
    },
    {
      key: "billing",
      icon: <CreditCard className="h-4 w-4" />,
      label: "计费",
      value: billingQ.isLoading ? "加载中" : billing?.billing_enabled ? "已开启" : "未开启",
      detail:
        billing && !billing.thresholds_pricing_aligned
          ? `缺少尺寸价格：${billing.thresholds_missing_prices.join(", ") || "-"}`
          : orphanCount > 0
          ? `${orphanCount} 个孤儿 hold 待处理`
          : "价格、兑换码和 hold 状态可用",
      tone:
        !billing?.billing_enabled || !billing?.thresholds_pricing_aligned || orphanCount > 0
          ? "warn"
          : "ok",
      tab: "billing" as const,
    },
    {
      key: "telegram",
      icon: <MessageCircle className="h-4 w-4" />,
      label: "Telegram",
      value: settingsQ.isLoading ? "加载中" : telegramEnabled ? "已启用" : "已关闭",
      detail: telegramTokenReady ? "绑定页和机器人链接已就绪" : "缺少 bot token 或 username",
      tone: telegramEnabled && telegramTokenReady ? "ok" : "warn",
      tab: "telegram" as const,
    },
    {
      key: "context",
      icon: <TimerReset className="h-4 w-4" />,
      label: "上下文",
      value: contextQ.isLoading ? "加载中" : context?.circuit_breaker_state ?? "未知",
      detail:
        context && context.last_24h.summary_attempts > 0
          ? `摘要成功率 ${Math.round(context.last_24h.summary_success_rate * 100)}%`
          : "暂无 24h 摘要样本",
      tone: context?.circuit_breaker_state === "closed" ? "ok" : "warn",
      tab: "settings" as const,
    },
    {
      key: "events",
      icon: <ShieldAlert className="h-4 w-4" />,
      label: "最近错误",
      value: failedEventsQ.isLoading ? "加载中" : `${failedEvents.length} 条`,
      detail: failedEvents[0]?.error_code ?? "近 24h 没有失败样本",
      tone: failedEvents.length > 0 ? "warn" : "ok",
      tab: "events" as const,
    },
  ];

  const dangerous = tiles.filter((tile) => tile.tone === "danger").length;
  const warnings = tiles.filter((tile) => tile.tone === "warn").length;
  const overallTone: Tone = dangerous > 0 ? "danger" : warnings > 0 ? "warn" : "ok";
  const busy =
    providersQ.isFetching ||
    providerStatsQ.isFetching ||
    proxiesQ.isFetching ||
    billingQ.isFetching ||
    orphanQ.isFetching ||
    contextQ.isFetching ||
    settingsQ.isFetching ||
    failedEventsQ.isFetching;

  const refreshAll = () => {
    void providersQ.refetch();
    void providerStatsQ.refetch();
    void proxiesQ.refetch();
    void billingQ.refetch();
    void orphanQ.refetch();
    void contextQ.refetch();
    void settingsQ.refetch();
    void failedEventsQ.refetch();
  };

  return (
    <div className="space-y-5">
      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <StatusIcon tone={overallTone} />
            <div>
              <p className="type-card-title">健康总览</p>
              <p className="type-body-sm mt-1 text-[var(--fg-2)]">
                {overallTone === "ok"
                  ? "核心通道暂无明显异常。"
                  : `当前有 ${dangerous} 个严重项、${warnings} 个提醒项。`}
              </p>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={refreshAll}
            disabled={busy}
            loading={busy}
            leftIcon={!busy ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
          >
            刷新
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {tiles.map((tile) => (
          <HealthTile
            key={tile.key}
            icon={tile.icon}
            label={tile.label}
            value={tile.value}
            detail={tile.detail}
            tone={tile.tone as Tone}
            onClick={() => onOpenTab(tile.tab)}
          />
        ))}
      </div>

      <AdminUpdatePanel />

      <Card variant="subtle" padding="none" className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-4 py-3">
          <div>
            <p className="type-card-title">近 24h 失败样本</p>
            <p className="type-caption text-[var(--fg-2)]">用于定位用户侧错误和 provider attempt。</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => onOpenTab("events")}>
            查看详情
          </Button>
        </div>
        <div className="divide-y divide-[var(--border-subtle)]">
          {failedEvents.map((event) => (
            <div key={event.id} className="grid gap-2 px-4 py-3 text-sm md:grid-cols-[160px_1fr_auto]">
              <span className="text-[var(--fg-2)]">{new Date(event.created_at).toLocaleString()}</span>
              <span className="min-w-0 truncate text-[var(--fg-0)]">
                {event.error_code ?? event.status} · {event.prompt ?? event.conversation_title ?? event.id}
              </span>
              <span className="font-mono text-xs text-[var(--fg-2)]">{event.kind}</span>
            </div>
          ))}
          {!failedEventsQ.isLoading && failedEvents.length === 0 && (
            <div className="px-4 py-8 text-center type-body-sm text-[var(--fg-2)]">
              暂无失败样本
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function HealthTile({
  icon,
  label,
  value,
  detail,
  tone,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail: string;
  tone: Tone;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-4 text-left transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-2)]"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 text-[var(--fg-2)]">
          {icon}
          <span className="type-caption">{label}</span>
        </div>
        <TonePill tone={tone} />
      </div>
      <p className="mt-3 text-lg font-semibold text-[var(--fg-0)]">{value}</p>
      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-[var(--fg-2)]">{detail}</p>
    </button>
  );
}

function StatusIcon({ tone }: { tone: Tone }) {
  const cls =
    tone === "ok"
      ? "border-success-border bg-success-soft text-success"
      : tone === "danger"
      ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
      : "border-warning-border bg-warning-soft text-warning";
  const Icon = tone === "ok" ? CheckCircle2 : tone === "danger" ? AlertTriangle : Activity;
  return (
    <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-control)] border ${cls}`}>
      <Icon className="h-5 w-5" />
    </div>
  );
}

function TonePill({ tone }: { tone: Tone }) {
  const cls =
    tone === "ok"
      ? "border-success-border bg-success-soft text-success"
      : tone === "danger"
      ? "border-danger-border bg-danger-soft text-[var(--danger-fg)]"
      : tone === "warn"
      ? "border-warning-border bg-warning-soft text-warning"
      : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]";
  const label = tone === "ok" ? "正常" : tone === "danger" ? "故障" : tone === "warn" ? "提醒" : "未配置";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] ${cls}`}>
      {label}
    </span>
  );
}

function settingValue(
  settingsByKey: Map<string, { value: string | null }>,
  key: string,
  fallback: string,
): string {
  const value = settingsByKey.get(key)?.value;
  return value == null || value === "" ? fallback : value;
}
