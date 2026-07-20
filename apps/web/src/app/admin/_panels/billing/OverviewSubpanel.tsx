"use client";

import {
  Activity,
  CheckCircle2,
  KeyRound,
  RefreshCw,
  ShieldAlert,
  SlidersHorizontal,
  WalletCards,
  XCircle,
} from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { formatRmb } from "@/lib/money";
import type {
  AdminBillingOverviewOut,
  AdminOrphanHoldOut,
  AdminWalletAuditOut,
} from "@/lib/types";
import { MetricCard } from "../BillingPanelParts";
import type { BillingHealthItem } from "./overviewModel";
import { useBillingOverview } from "./useBillingOverview";

function HealthCard({
  health,
  onGoPricing,
  onRefresh,
}: {
  health: BillingHealthItem[];
  onGoPricing: () => void;
  onRefresh: () => void;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">健康检查</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            这里展示计费能否从创建兑换码到扣费完整跑通。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onRefresh}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          刷新
        </Button>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        {health.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={item.ok ? undefined : onGoPricing}
            className="flex items-start gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3 text-left"
          >
            {item.ok ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 text-success" />
            ) : (
              <XCircle className="mt-0.5 h-4 w-4 text-[var(--danger-fg)]" />
            )}
            <span className="min-w-0">
              <span className="block text-sm text-[var(--fg-0)]">
                {item.label}
              </span>
              <span className="block whitespace-normal break-words text-xs text-[var(--fg-2)]">
                {item.value}
              </span>
            </span>
          </button>
        ))}
      </div>
    </Card>
  );
}

function BootstrapCard({
  visible,
  rate,
  pending,
  onRateChange,
  onBootstrap,
}: {
  visible: boolean;
  rate: string;
  pending: boolean;
  onRateChange: (value: string) => void;
  onBootstrap: () => void;
}) {
  if (!visible) return null;
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 text-[var(--color-lumen-amber)]" />
        <p className="type-card-title">首次启用</p>
      </div>
      <div className="grid gap-3 md:grid-cols-[120px_auto]">
        <input
          value={rate}
          onChange={(event) => onRateChange(event.target.value)}
          inputMode="decimal"
          placeholder="USD→RMB"
          className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
        />
        <Button
          variant="primary"
          size="md"
          onClick={onBootstrap}
          loading={pending}
        >
          初始化计费
        </Button>
      </div>
    </Card>
  );
}

function BillingMetrics({
  overview,
}: {
  overview?: AdminBillingOverviewOut;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-4">
      <MetricCard
        label="钱包总余额"
        value={`¥${formatRmb(overview?.wallet_total_balance.rmb)}`}
        icon={<WalletCards className="h-4 w-4" />}
      />
      <MetricCard
        label="活跃预扣"
        value={`${overview?.active_holds_count ?? 0} 笔 / ¥${formatRmb(overview?.active_holds.rmb)}`}
        icon={<Activity className="h-4 w-4" />}
      />
      <MetricCard
        label="24h 兑换"
        value={`${overview?.codes_redeemed_24h ?? 0} 张 / ¥${formatRmb(overview?.codes_redeemed_24h_amount.rmb)}`}
        icon={<KeyRound className="h-4 w-4" />}
      />
      <MetricCard
        label="24h 扣费"
        value={`¥${formatRmb(overview?.charges_24h.rmb)}`}
        icon={<SlidersHorizontal className="h-4 w-4" />}
      />
    </div>
  );
}

function RecentAuditCard({
  overview,
  loading,
}: {
  overview?: AdminBillingOverviewOut;
  loading: boolean;
}) {
  const events = overview?.recent_audit_events ?? [];
  return (
    <Card variant="subtle" padding="none" className="overflow-hidden">
      <div className="border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="type-card-title">最近审计</p>
      </div>
      <div className="divide-y divide-[var(--border-subtle)] md:max-h-[360px] md:overflow-auto">
        {events.map((event) => (
          <div
            key={event.id}
            className="flex flex-wrap gap-x-4 gap-y-1 px-4 py-3 text-sm"
          >
            <span className="shrink-0 text-[var(--fg-2)]">
              {new Date(event.created_at).toLocaleString()}
            </span>
            <span className="max-w-full truncate font-mono text-xs text-[var(--fg-0)] md:max-w-[260px]">
              {event.event_type}
            </span>
            <span className="truncate text-[var(--fg-2)]">
              {event.target_user_id ?? event.user_id ?? "-"}
            </span>
          </div>
        ))}
        {!loading && events.length === 0 && (
          <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
            暂无审计事件
          </div>
        )}
      </div>
    </Card>
  );
}

function AuditResultBanner({
  result,
}: {
  result: AdminWalletAuditOut | null;
}) {
  if (!result) return null;
  return (
    <div
      className={[
        "rounded-[var(--radius-control)] border px-3 py-2 text-sm",
        result.ok
          ? "border-success-border bg-success-soft text-success"
          : "border-danger-border bg-danger-soft text-[var(--danger-fg)]",
      ].join(" ")}
    >
      {result.ok
        ? `对账通过: ${result.transactions} 笔流水`
        : `发现 ${result.mismatch_count} 个不一致`}
    </div>
  );
}

function OrphanHoldRow({
  item,
  releasePending,
  onRelease,
}: {
  item: AdminOrphanHoldOut;
  releasePending: boolean;
  onRelease: (txId: string) => void;
}) {
  const reference = `${item.tx.ref_type}:${item.tx.ref_id}`;
  const holdRmb = Math.abs(item.tx.amount.micro) / 1_000_000;
  return (
    <div className="grid gap-3 px-4 py-3 text-sm md:grid-cols-[1fr_auto]">
      <div className="min-w-0">
        <p
          className="truncate font-mono text-xs text-[var(--fg-0)]"
          title={reference}
        >
          {reference}
        </p>
        <p className="text-[var(--fg-2)]">
          user {item.user_id} · 预扣 ¥{formatRmb(holdRmb)} ·{" "}
          {Math.round(item.age_seconds / 60)} 分钟
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          if (window.confirm("确认强制释放这个 hold？")) {
            onRelease(item.tx.id);
          }
        }}
        loading={releasePending}
      >
        强制释放
      </Button>
    </div>
  );
}

function ReconciliationCard({
  auditResult,
  auditPending,
  orphanHolds,
  orphanHoldsLoading,
  releaseHoldPending,
  onAudit,
  onRelease,
}: {
  auditResult: AdminWalletAuditOut | null;
  auditPending: boolean;
  orphanHolds: AdminOrphanHoldOut[];
  orphanHoldsLoading: boolean;
  releaseHoldPending: boolean;
  onAudit: () => void;
  onRelease: (txId: string) => void;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="type-card-title">对账与孤儿 hold</p>
          <p className="type-body-sm text-[var(--fg-2)]">
            对账会回放钱包流水；孤儿 hold 是 60 分钟以上未 settle/release
            的预扣。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onAudit}
          loading={auditPending}
        >
          运行对账
        </Button>
      </div>
      <AuditResultBanner result={auditResult} />
      <div className="divide-y divide-[var(--border-subtle)] rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
        {orphanHolds.map((item) => (
          <OrphanHoldRow
            key={item.tx.id}
            item={item}
            releasePending={releaseHoldPending}
            onRelease={onRelease}
          />
        ))}
        {!orphanHoldsLoading && orphanHolds.length === 0 && (
          <div className="px-4 py-6 text-center text-sm text-[var(--fg-2)]">
            暂无孤儿 hold
          </div>
        )}
      </div>
    </Card>
  );
}

export function OverviewSubpanel({
  onGoPricing,
}: {
  onGoPricing: () => void;
}) {
  const state = useBillingOverview();
  return (
    <div className="space-y-5">
      <HealthCard
        health={state.health}
        onGoPricing={onGoPricing}
        onRefresh={() => void state.refreshOverview()}
      />
      <BootstrapCard
        visible={state.showBootstrap}
        rate={state.bootstrapRate}
        pending={state.bootstrapPending}
        onRateChange={state.setBootstrapRate}
        onBootstrap={state.bootstrap}
      />
      <BillingMetrics overview={state.overview} />
      <RecentAuditCard
        overview={state.overview}
        loading={state.overviewLoading}
      />
      <ReconciliationCard
        auditResult={state.auditResult}
        auditPending={state.auditPending}
        orphanHolds={state.orphanHolds}
        orphanHoldsLoading={state.orphanHoldsLoading}
        releaseHoldPending={state.releaseHoldPending}
        onAudit={state.audit}
        onRelease={state.releaseHold}
      />
    </div>
  );
}
