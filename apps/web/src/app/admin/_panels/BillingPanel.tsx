"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  CheckCircle2,
  EyeOff,
  KeyRound,
  Plus,
  RefreshCw,
  Save,
  ShieldAlert,
  SlidersHorizontal,
  WalletCards,
  XCircle,
} from "lucide-react";

import {
  bulkUpdateAdminPricing,
  bootstrapAdminBilling,
  getAdminBillingOverview,
  getAdminPricing,
  getSystemSettings,
  importOpenAiPricing,
  listAdminOrphanHolds,
  releaseAdminOrphanHold,
  rotateAdminRedemptionSecret,
  runAdminWalletAudit,
  updateAdminPricing,
  updateSystemSettings,
} from "@/lib/apiClient";
import type { AdminWalletAuditOut } from "@/lib/types";
import type { PricingRuleOut, PricingRuleUpsertIn } from "@/lib/types";
import { Button, Card, toast } from "@/components/ui/primitives";
import { RedemptionPanel } from "./RedemptionPanel";

const DEFAULT_IMAGE_THRESHOLDS: Record<string, number> = {
  "1k": 1_572_864,
  "2k": 3_686_400,
  "4k": 8_294_400,
};

type BillingSubTab = "overview" | "pricing" | "codes" | "wallets";

const SUB_TABS: { key: BillingSubTab; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "pricing", label: "定价" },
  { key: "codes", label: "兑换码" },
  { key: "wallets", label: "用户钱包" },
];

function money(value?: string | number | null): string {
  return Number(value ?? 0).toFixed(2);
}

function microToRmb(value?: string | null): string {
  const raw = Number(value ?? 0);
  if (!Number.isFinite(raw)) return "0";
  return String(raw / 1_000_000);
}

function rmbToMicro(value: string): string {
  const raw = Number(value);
  if (!Number.isFinite(raw)) return "0";
  return String(Math.round(raw * 1_000_000));
}

function settingValue(
  settingsByKey: Map<string, string | null>,
  key: string,
  fallback: string,
): string {
  const value = settingsByKey.get(key);
  return value == null || value === "" ? fallback : value;
}

function groupModelRules(items: PricingRuleOut[]) {
  const map = new Map<
    string,
    {
      model: string;
      input?: PricingRuleOut;
      output?: PricingRuleOut;
      updated_at?: string;
    }
  >();
  for (const item of items) {
    if (item.scope !== "chat_model") continue;
    const row = map.get(item.key) ?? { model: item.key };
    if (item.unit === "per_1k_tokens_in") row.input = item;
    if (item.unit === "per_1k_tokens_out") row.output = item;
    row.updated_at = [row.updated_at, item.updated_at].filter(Boolean).sort().at(-1);
    map.set(item.key, row);
  }
  return Array.from(map.values()).sort((a, b) => a.model.localeCompare(b.model));
}

export function BillingPanel() {
  const [tab, setTab] = useState<BillingSubTab>("overview");

  return (
    <div className="space-y-5">
      <div className="overflow-x-auto scrollbar-thin">
        <div className="inline-flex rounded-full border border-[var(--border)] bg-white/[0.04] p-1">
          {SUB_TABS.map((item) => {
            const active = tab === item.key;
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => setTab(item.key)}
                className={[
                  "rounded-full px-3.5 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-[var(--accent)] text-black"
                    : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                ].join(" ")}
              >
                {item.label}
              </button>
            );
          })}
        </div>
      </div>

      {tab === "overview" ? (
        <OverviewSubpanel onGoPricing={() => setTab("pricing")} />
      ) : tab === "pricing" ? (
        <PricingSubpanel />
      ) : tab === "codes" ? (
        <RedemptionPanel section="codes" />
      ) : (
        <RedemptionPanel section="wallets" />
      )}
    </div>
  );
}

function OverviewSubpanel({ onGoPricing }: { onGoPricing: () => void }) {
  const qc = useQueryClient();
  const overviewQ = useQuery({
    queryKey: ["admin", "billing", "overview"],
    queryFn: getAdminBillingOverview,
    retry: false,
  });
  const [bootstrapRate, setBootstrapRate] = useState("1.0");
  const [auditResult, setAuditResult] = useState<AdminWalletAuditOut | null>(null);

  const orphanQ = useQuery({
    queryKey: ["admin", "billing", "orphan-holds"],
    queryFn: () => listAdminOrphanHolds({ min_age_minutes: 60, limit: 20 }),
    retry: false,
  });

  const bootstrapMut = useMutation({
    mutationFn: () =>
      bootstrapAdminBilling({
        enabled: true,
        usd_to_rmb_rate: Number(bootstrapRate) || 1,
      }),
    onSuccess: async () => {
      toast.success("计费初始化完成");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] }),
        qc.invalidateQueries({ queryKey: ["admin", "settings"] }),
        qc.invalidateQueries({ queryKey: ["admin", "pricing"] }),
      ]);
    },
    onError: (err) => toast.error("初始化失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const auditMut = useMutation({
    mutationFn: runAdminWalletAudit,
    onSuccess: (out) => {
      setAuditResult(out);
      if (out.ok) {
        toast.success("钱包对账通过", {
          description: `${out.transactions} 笔流水 / ${out.users} 个用户`,
        });
      } else {
        toast.error("钱包对账发现异常", { description: `${out.mismatch_count} 个不一致` });
      }
    },
    onError: (err) => toast.error("对账失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const releaseHoldMut = useMutation({
    mutationFn: releaseAdminOrphanHold,
    onSuccess: async () => {
      toast.success("孤儿 hold 已释放");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "billing", "orphan-holds"] }),
        qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] }),
      ]);
    },
    onError: (err) => toast.error("释放失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const overview = overviewQ.data;
  const health = [
    {
      label: "计费开关",
      ok: overview?.billing_enabled ?? false,
      value: overview?.billing_enabled ? "开启" : "关闭",
    },
    {
      label: "兑换码 secret",
      ok: overview?.redemption_secret_configured ?? false,
      value: overview?.redemption_secret_configured ? "已配置" : "未配置",
    },
    {
      label: "尺寸价格",
      ok: overview?.thresholds_pricing_aligned ?? false,
      value:
        overview?.thresholds_pricing_aligned
          ? "已对齐"
          : `缺少 ${overview?.thresholds_missing_prices.join(", ") || "-"}`,
    },
  ];

  return (
    <div className="space-y-5">
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
            onClick={() => void overviewQ.refetch()}
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
                <span className="block text-sm text-[var(--fg-0)]">{item.label}</span>
                <span className="block truncate text-xs text-[var(--fg-2)]">{item.value}</span>
              </span>
            </button>
          ))}
        </div>
      </Card>

      {overview && (!overview.bootstrap_completed || !overview.redemption_secret_configured) && (
        <Card variant="subtle" padding="lg" className="space-y-4">
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-[var(--color-lumen-amber)]" />
            <p className="type-card-title">首次启用</p>
          </div>
          <div className="grid gap-3 md:grid-cols-[120px_auto]">
            <input
              value={bootstrapRate}
              onChange={(e) => setBootstrapRate(e.target.value)}
              inputMode="decimal"
              placeholder="USD→RMB"
              className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
            />
            <Button
              variant="primary"
              size="md"
              onClick={() => bootstrapMut.mutate()}
              loading={bootstrapMut.isPending}
            >
              初始化计费
            </Button>
          </div>
        </Card>
      )}

      <div className="grid gap-3 md:grid-cols-4">
        <MetricCard
          label="钱包总余额"
          value={`¥${money(overview?.wallet_total_balance.rmb)}`}
          icon={<WalletCards className="h-4 w-4" />}
        />
        <MetricCard
          label="活跃预扣"
          value={`${overview?.active_holds_count ?? 0} 笔 / ¥${money(overview?.active_holds.rmb)}`}
          icon={<Activity className="h-4 w-4" />}
        />
        <MetricCard
          label="24h 兑换"
          value={`${overview?.codes_redeemed_24h ?? 0} 张 / ¥${money(overview?.codes_redeemed_24h_amount.rmb)}`}
          icon={<KeyRound className="h-4 w-4" />}
        />
        <MetricCard
          label="24h 扣费"
          value={`¥${money(overview?.charges_24h.rmb)}`}
          icon={<SlidersHorizontal className="h-4 w-4" />}
        />
      </div>

      <Card variant="subtle" padding="none" className="overflow-hidden">
        <div className="border-b border-[var(--border-subtle)] px-4 py-3">
          <p className="type-card-title">最近审计</p>
        </div>
        <div className="max-h-[360px] divide-y divide-[var(--border-subtle)] overflow-auto">
          {(overview?.recent_audit_events ?? []).map((event) => (
            <div key={event.id} className="grid gap-1 px-4 py-3 text-sm md:grid-cols-[180px_220px_1fr]">
              <span className="text-[var(--fg-2)]">{new Date(event.created_at).toLocaleString()}</span>
              <span className="font-mono text-xs text-[var(--fg-0)]">{event.event_type}</span>
              <span className="truncate text-[var(--fg-2)]">
                {event.target_user_id ?? event.user_id ?? "-"}
              </span>
            </div>
          ))}
          {!overviewQ.isLoading && (overview?.recent_audit_events ?? []).length === 0 && (
            <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">暂无审计事件</div>
          )}
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-card-title">对账与孤儿 hold</p>
            <p className="type-body-sm text-[var(--fg-2)]">
              对账会回放钱包流水；孤儿 hold 是 60 分钟以上未 settle/release 的预扣。
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => auditMut.mutate()}
            loading={auditMut.isPending}
          >
            运行对账
          </Button>
        </div>
        {auditResult && (
          <div
            className={[
              "rounded-[var(--radius-control)] border px-3 py-2 text-sm",
              auditResult.ok
                ? "border-success-border bg-success-soft text-success"
                : "border-danger-border bg-danger-soft text-[var(--danger-fg)]",
            ].join(" ")}
          >
            {auditResult.ok
              ? `对账通过: ${auditResult.transactions} 笔流水`
              : `发现 ${auditResult.mismatch_count} 个不一致`}
          </div>
        )}
        <div className="divide-y divide-[var(--border-subtle)] rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
          {(orphanQ.data ?? []).map((item) => (
            <div key={item.tx.id} className="grid gap-3 px-4 py-3 text-sm md:grid-cols-[1fr_auto]">
              <div className="min-w-0">
                <p className="font-mono text-xs text-[var(--fg-0)]">{item.tx.ref_type}:{item.tx.ref_id}</p>
                <p className="text-[var(--fg-2)]">
                  user {item.user_id} · 预扣 ¥{money(Math.abs(item.tx.amount.micro) / 1_000_000)} · {Math.round(item.age_seconds / 60)} 分钟
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  if (window.confirm("确认强制释放这个 hold？")) {
                    releaseHoldMut.mutate(item.tx.id);
                  }
                }}
                loading={releaseHoldMut.isPending}
              >
                强制释放
              </Button>
            </div>
          ))}
          {!orphanQ.isLoading && (orphanQ.data ?? []).length === 0 && (
            <div className="px-4 py-6 text-center text-sm text-[var(--fg-2)]">
              暂无孤儿 hold
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function MetricCard({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon: ReactNode;
}) {
  return (
    <Card variant="subtle" padding="md" className="space-y-2">
      <div className="flex items-center gap-2 text-[var(--fg-2)]">
        {icon}
        <span className="type-caption">{label}</span>
      </div>
      <p className="text-lg font-semibold tabular-nums text-[var(--fg-0)]">{value}</p>
    </Card>
  );
}

function PricingSubpanel() {
  const qc = useQueryClient();
  const pricingQ = useQuery({
    queryKey: ["admin", "pricing"],
    queryFn: getAdminPricing,
    retry: false,
  });
  const settingsQ = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: getSystemSettings,
    retry: false,
  });
  const [imagePrices, setImagePrices] = useState<Record<string, string>>({});
  const [imageThresholds, setImageThresholds] = useState<Record<string, string>>({});
  const [customTiers, setCustomTiers] = useState<string[]>([]);
  const [newTier, setNewTier] = useState("");
  const [newTierThreshold, setNewTierThreshold] = useState("");
  const [priceFile, setPriceFile] = useState("");
  const [rateDraft, setRateDraft] = useState<string | null>(null);
  const [enabledDraft, setEnabledDraft] = useState<string | null>(null);
  const [allowNegativeDraft, setAllowNegativeDraft] = useState<string | null>(null);
  const [showEstimateDraft, setShowEstimateDraft] = useState<string | null>(null);
  const [lowBalanceRmbDraft, setLowBalanceRmbDraft] = useState<string | null>(null);
  const [secretConfirmed, setSecretConfirmed] = useState(false);
  const [modelDrafts, setModelDrafts] = useState<Record<string, string>>({});
  const [bulkModel, setBulkModel] = useState("");
  const [bulkChannel, setBulkChannel] = useState("");
  const [bulkRates, setBulkRates] = useState<Record<string, string>>({});

  const settingsByKey = useMemo(
    () => new Map(settingsQ.data?.items.map((item) => [item.key, item.value]) ?? []),
    [settingsQ.data?.items],
  );
  const settingsItemsByKey = useMemo(
    () => new Map(settingsQ.data?.items.map((item) => [item.key, item]) ?? []),
    [settingsQ.data?.items],
  );
  const enabled = enabledDraft ?? settingValue(settingsByKey, "billing.enabled", "0");
  const allowNegative =
    allowNegativeDraft ?? settingValue(settingsByKey, "billing.allow_negative_balance", "0");
  const showEstimate =
    showEstimateDraft ?? settingValue(settingsByKey, "billing.show_estimate_in_composer", "1");
  const lowBalanceRmb =
    lowBalanceRmbDraft ??
    microToRmb(settingValue(settingsByKey, "billing.low_balance_warn_micro", "2000000"));
  const rate = rateDraft ?? settingValue(settingsByKey, "billing.usd_to_rmb_rate", "1.0");
  const secretConfigured =
    settingsItemsByKey.get("billing.redemption_code_secret")?.has_value ?? false;

  const savedThresholds = useMemo(
    () => pricingQ.data?.image_size_thresholds ?? DEFAULT_IMAGE_THRESHOLDS,
    [pricingQ.data?.image_size_thresholds],
  );

  const imageRows = useMemo(() => {
    const tiers = new Set<string>(Object.keys(DEFAULT_IMAGE_THRESHOLDS));
    for (const key of Object.keys(savedThresholds)) tiers.add(key);
    for (const key of customTiers) tiers.add(key);
    for (const item of pricingQ.data?.items ?? []) {
      if (item.scope === "image_size" && item.unit === "per_image") tiers.add(item.key);
    }
    return Array.from(tiers)
      .sort((a, b) => (savedThresholds[a] ?? 0) - (savedThresholds[b] ?? 0) || a.localeCompare(b))
      .map((tier) => {
        const row = pricingQ.data?.items.find(
          (item) => item.scope === "image_size" && item.key === tier && item.unit === "per_image",
        );
        return { tier, row, threshold: savedThresholds[tier] ?? 0 };
      });
  }, [customTiers, pricingQ.data?.items, savedThresholds]);

  const modelRows = useMemo(
    () => groupModelRules(pricingQ.data?.items ?? []),
    [pricingQ.data?.items],
  );

  const invalidateBilling = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["admin", "pricing"] }),
      qc.invalidateQueries({ queryKey: ["admin", "settings"] }),
      qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] }),
      qc.invalidateQueries({ queryKey: ["me", "pricing"] }),
      qc.invalidateQueries({ queryKey: ["me", "wallet"] }),
    ]);
  };

  const saveImageMut = useMutation({
    mutationFn: async () => {
      const items: PricingRuleUpsertIn[] = imageRows.map(({ tier, row }) => ({
        scope: "image_size",
        key: tier,
        variant: "default",
        unit: "per_image",
        price_rmb: imagePrices[tier] ?? row?.price.rmb ?? "0",
        enabled: row?.enabled ?? true,
        note: row?.note ?? "",
      }));
      const thresholds = Object.fromEntries(
        imageRows.map(({ tier, threshold }) => [
          tier,
          Number(imageThresholds[tier] ?? threshold) || 0,
        ]),
      );
      return updateAdminPricing(items, { image_size_thresholds: thresholds });
    },
    onSuccess: async () => {
      toast.success("尺寸定价已保存");
      setImagePrices({});
      setImageThresholds({});
      setCustomTiers([]);
      await invalidateBilling();
    },
    onError: (err) => toast.error("保存失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const saveGlobalMut = useMutation({
    mutationFn: () =>
      updateSystemSettings([
        { key: "billing.enabled", value: enabled },
        { key: "billing.allow_negative_balance", value: allowNegative },
        { key: "billing.show_estimate_in_composer", value: showEstimate },
        { key: "billing.low_balance_warn_micro", value: rmbToMicro(lowBalanceRmb) },
        { key: "billing.usd_to_rmb_rate", value: rate },
      ]),
    onSuccess: async () => {
      toast.success("全局设置已保存");
      await invalidateBilling();
    },
    onError: (err) => toast.error("保存失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const rotateSecretMut = useMutation({
    mutationFn: rotateAdminRedemptionSecret,
    onSuccess: async () => {
      setSecretConfirmed(false);
      toast.success(secretConfigured ? "兑换码 secret 已轮换" : "兑换码 secret 已生成");
      await invalidateBilling();
    },
    onError: (err) => toast.error("更新 secret 失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const importMut = useMutation({
    mutationFn: () => importOpenAiPricing(priceFile, Number(rate) || 1),
    onSuccess: async () => {
      toast.success("对话模型价格已导入");
      setPriceFile("");
      await invalidateBilling();
    },
    onError: (err) => toast.error("导入失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const bulkMut = useMutation({
    mutationFn: () => {
      const rateValue = (key: string) => {
        const value = bulkRates[key]?.trim();
        return value ? value : undefined;
      };
      const numberValue = (key: string) => {
        const raw = bulkRates[key]?.trim();
        if (!raw) return undefined;
        const value = Number(raw);
        return Number.isFinite(value) ? value : undefined;
      };
      return bulkUpdateAdminPricing({
        model: bulkModel.trim(),
        channel: bulkChannel.trim() || null,
        rates: {
          input: rateValue("input"),
          output: rateValue("output"),
          cache_read: rateValue("cache_read"),
          cache_creation: rateValue("cache_creation"),
          cache_creation_5m: rateValue("cache_creation_5m"),
          cache_creation_1h: rateValue("cache_creation_1h"),
          image_output: rateValue("image_output"),
          reasoning: rateValue("reasoning"),
          input_priority: rateValue("input_priority"),
          output_priority: rateValue("output_priority"),
          cache_read_priority: rateValue("cache_read_priority"),
          long_context_threshold: numberValue("long_context_threshold"),
          long_context_input_multiplier: numberValue("long_context_input_multiplier"),
          long_context_output_multiplier: numberValue("long_context_output_multiplier"),
        },
      });
    },
    onSuccess: async () => {
      toast.success("批量模型定价已保存");
      setBulkModel("");
      setBulkChannel("");
      setBulkRates({});
      await invalidateBilling();
    },
    onError: (err) => toast.error("批量保存失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const saveModelsMut = useMutation({
    mutationFn: () => {
      const items: PricingRuleUpsertIn[] = [];
      for (const row of modelRows) {
        if (row.input) {
          items.push({
            scope: "chat_model",
            key: row.model,
            variant: row.input.variant,
            unit: "per_1k_tokens_in",
            price_rmb: modelDrafts[`${row.model}:in`] ?? row.input.price.rmb,
            enabled: row.input.enabled,
            note: row.input.note,
          });
        }
        if (row.output) {
          items.push({
            scope: "chat_model",
            key: row.model,
            variant: row.output.variant,
            unit: "per_1k_tokens_out",
            price_rmb: modelDrafts[`${row.model}:out`] ?? row.output.price.rmb,
            enabled: row.output.enabled,
            note: row.output.note,
          });
        }
      }
      return updateAdminPricing(items);
    },
    onSuccess: async () => {
      setModelDrafts({});
      toast.success("模型价格已保存");
      await invalidateBilling();
    },
    onError: (err) => toast.error("保存失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const disableModelMut = useMutation({
    mutationFn: (row: ReturnType<typeof groupModelRules>[number]) => {
      const items: PricingRuleUpsertIn[] = [];
      if (row.input) {
        items.push({
          scope: "chat_model",
          key: row.model,
          variant: row.input.variant,
          unit: "per_1k_tokens_in",
          price_rmb: row.input.price.rmb,
          enabled: false,
          note: row.input.note,
        });
      }
      if (row.output) {
        items.push({
          scope: "chat_model",
          key: row.model,
          variant: row.output.variant,
          unit: "per_1k_tokens_out",
          price_rmb: row.output.price.rmb,
          enabled: false,
          note: row.output.note,
        });
      }
      return updateAdminPricing(items);
    },
    onSuccess: async () => {
      toast.success("模型已停用");
      await invalidateBilling();
    },
    onError: (err) => toast.error("停用失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const addTier = () => {
    const tier = newTier.trim().toLowerCase();
    if (!tier) {
      toast.warning("请填写档位名称");
      return;
    }
    if (imageRows.some((row) => row.tier === tier)) {
      toast.warning("档位已存在");
      return;
    }
    const threshold = Number(newTierThreshold) || 0;
    setCustomTiers((prev) => (prev.includes(tier) ? prev : [...prev, tier]));
    setImagePrices((prev) => ({ ...prev, [tier]: "0" }));
    setImageThresholds((prev) => ({ ...prev, [tier]: String(threshold) }));
    setNewTier("");
    setNewTierThreshold("");
  };

  return (
    <div className="space-y-5">
      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-card-title">全局设置</p>
            <p className="type-body-sm text-[var(--fg-2)]">
              开关、低余额提示和兑换码 secret 集中在这里。
            </p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => saveGlobalMut.mutate()}
            loading={saveGlobalMut.isPending}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            保存设置
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-5">
          <SwitchField
            label="计费开关"
            checked={enabled === "1"}
            onChange={(checked) => setEnabledDraft(checked ? "1" : "0")}
          />
          <SwitchField
            label="允许负余额"
            checked={allowNegative === "1"}
            onChange={(checked) => setAllowNegativeDraft(checked ? "1" : "0")}
          />
          <SwitchField
            label="发送框预估"
            checked={showEstimate === "1"}
            onChange={(checked) => setShowEstimateDraft(checked ? "1" : "0")}
          />
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">低余额提示 (¥)</span>
            <input
              value={lowBalanceRmb}
              onChange={(e) => setLowBalanceRmbDraft(e.target.value)}
              inputMode="decimal"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
            />
          </label>
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">USD→RMB</span>
            <input
              value={rate}
              onChange={(e) => setRateDraft(e.target.value)}
              inputMode="decimal"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </div>
        <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <EyeOff className="h-4 w-4 text-[var(--fg-2)]" />
              <div>
                <p className="text-sm text-[var(--fg-0)]">兑换码 secret</p>
                <p className="type-caption text-[var(--fg-2)]">
                  {secretConfigured ? "已配置；轮换会撤销所有未兑换码" : "未配置；创建和兑换都会被拒绝"}
                </p>
              </div>
            </div>
            <div className="w-full md:w-auto">
              <Button
                variant={secretConfigured ? "outline" : "primary"}
                size="md"
                disabled={secretConfigured && !secretConfirmed}
                loading={rotateSecretMut.isPending}
                onClick={() => rotateSecretMut.mutate()}
              >
                {secretConfigured ? "轮换" : "生成"}
              </Button>
            </div>
          </div>
          {secretConfigured && (
            <label className="mt-3 flex items-center gap-2 text-xs text-[var(--fg-2)]">
              <input
                type="checkbox"
                checked={secretConfirmed}
                onChange={(e) => setSecretConfirmed(e.target.checked)}
              />
              我确认轮换 secret 会作废所有未兑换码
            </label>
          )}
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-card-title">缓存感知模型定价</p>
            <p className="type-body-sm text-[var(--fg-2)]">
              一次写入输入、输出、缓存、推理和长上下文价格。
            </p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => bulkMut.mutate()}
            loading={bulkMut.isPending}
            disabled={!bulkModel.trim()}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            批量保存
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-[1fr_180px]">
          <input
            value={bulkModel}
            onChange={(e) => setBulkModel(e.target.value)}
            placeholder="模型，如 claude-sonnet-4-6"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <input
            value={bulkChannel}
            onChange={(e) => setBulkChannel(e.target.value)}
            placeholder="channel，可空"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[
            ["input", "输入 ¥/1K"],
            ["output", "输出 ¥/1K"],
            ["cache_read", "缓存读 ¥/1K"],
            ["cache_creation", "缓存写 ¥/1K"],
            ["cache_creation_5m", "缓存写 5m ¥/1K"],
            ["cache_creation_1h", "缓存写 1h ¥/1K"],
            ["image_output", "图片输出 ¥/1K"],
            ["reasoning", "推理 ¥/1K"],
            ["input_priority", "Priority 输入"],
            ["output_priority", "Priority 输出"],
            ["cache_read_priority", "Priority 缓存读"],
            ["long_context_threshold", "长上下文阈值"],
            ["long_context_input_multiplier", "长上下文输入倍数"],
            ["long_context_output_multiplier", "长上下文输出倍数"],
          ].map(([key, label]) => (
            <label key={key} className="space-y-1.5">
              <span className="type-caption text-[var(--fg-2)]">{label}</span>
              <input
                value={bulkRates[key] ?? ""}
                onChange={(e) =>
                  setBulkRates((prev) => ({ ...prev, [key]: e.target.value }))
                }
                inputMode="decimal"
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
              />
            </label>
          ))}
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-card-title">尺寸定价</p>
            <p className="type-body-sm text-[var(--fg-2)]">
              阈值和价格会在后端同一事务保存，避免前后端档位漂移。
            </p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => saveImageMut.mutate()}
            loading={saveImageMut.isPending}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            保存尺寸定价
          </Button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] text-sm">
            <thead className="text-left text-[var(--fg-2)]">
              <tr className="border-b border-[var(--border-subtle)]">
                <th className="px-3 py-2">档位</th>
                <th className="px-3 py-2">像素下界</th>
                <th className="px-3 py-2">单价 (¥/张)</th>
                <th className="px-3 py-2">状态</th>
              </tr>
            </thead>
            <tbody>
              {imageRows.map(({ tier, row, threshold }) => (
                <tr key={tier} className="border-b border-[var(--border-subtle)]">
                  <td className="px-3 py-2 font-mono">{tier}</td>
                  <td className="px-3 py-2">
                    <input
                      value={imageThresholds[tier] ?? String(threshold)}
                      onChange={(e) =>
                        setImageThresholds((prev) => ({ ...prev, [tier]: e.target.value }))
                      }
                      inputMode="numeric"
                      className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                    />
                  </td>
                  <td className="px-3 py-2">
                    <input
                      value={imagePrices[tier] ?? row?.price.rmb ?? ""}
                      onChange={(e) =>
                        setImagePrices((prev) => ({ ...prev, [tier]: e.target.value }))
                      }
                      inputMode="decimal"
                      placeholder="0.20"
                      className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
                    />
                  </td>
                  <td className="px-3 py-2">{row?.enabled === false ? "停用" : "启用"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_1fr_auto]">
          <input
            value={newTier}
            onChange={(e) => setNewTier(e.target.value)}
            placeholder="新增档位，如 8k"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <input
            value={newTierThreshold}
            onChange={(e) => setNewTierThreshold(e.target.value)}
            placeholder="像素下界，如 33177600"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <Button
            variant="outline"
            size="md"
            type="button"
            onClick={addTier}
            leftIcon={<Plus className="h-3.5 w-3.5" />}
          >
            添加档位
          </Button>
        </div>
      </Card>

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-card-title">对话模型定价</p>
            <p className="type-body-sm text-[var(--fg-2)]">
              可直接编辑当前模型，也可以粘贴 OpenAI 价目批量导入。
            </p>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => saveModelsMut.mutate()}
            loading={saveModelsMut.isPending}
            disabled={modelRows.length === 0}
            leftIcon={<Save className="h-3.5 w-3.5" />}
          >
            保存模型价格
          </Button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[840px] text-sm">
            <thead className="text-left text-[var(--fg-2)]">
              <tr className="border-b border-[var(--border-subtle)]">
                <th className="px-3 py-2">模型</th>
                <th className="px-3 py-2">输入 ¥/1K</th>
                <th className="px-3 py-2">输出 ¥/1K</th>
                <th className="px-3 py-2">状态</th>
                <th className="px-3 py-2">更新于</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {modelRows.map((row) => {
                const enabled = Boolean(row.input?.enabled || row.output?.enabled);
                return (
                  <tr key={row.model} className="border-b border-[var(--border-subtle)]">
                    <td className="px-3 py-2 font-mono text-xs">{row.model}</td>
                    <td className="px-3 py-2">
                      <input
                        value={modelDrafts[`${row.model}:in`] ?? row.input?.price.rmb ?? ""}
                        disabled={!row.input}
                        onChange={(e) =>
                          setModelDrafts((prev) => ({
                            ...prev,
                            [`${row.model}:in`]: e.target.value,
                          }))
                        }
                        className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50 disabled:opacity-50"
                      />
                    </td>
                    <td className="px-3 py-2">
                      <input
                        value={modelDrafts[`${row.model}:out`] ?? row.output?.price.rmb ?? ""}
                        disabled={!row.output}
                        onChange={(e) =>
                          setModelDrafts((prev) => ({
                            ...prev,
                            [`${row.model}:out`]: e.target.value,
                          }))
                        }
                        className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50 disabled:opacity-50"
                      />
                    </td>
                    <td className="px-3 py-2">{enabled ? "启用" : "停用"}</td>
                    <td className="px-3 py-2 text-[var(--fg-2)]">
                      {row.updated_at ? new Date(row.updated_at).toLocaleString() : "-"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => disableModelMut.mutate(row)}
                        disabled={!enabled}
                      >
                        停用
                      </Button>
                    </td>
                  </tr>
                );
              })}
              {!pricingQ.isLoading && modelRows.length === 0 && (
                <tr>
                  <td className="px-3 py-8 text-center text-[var(--fg-2)]" colSpan={6}>
                    暂无模型价格
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_120px_auto]">
          <textarea
            value={priceFile}
            onChange={(e) => setPriceFile(e.target.value)}
            rows={5}
            placeholder="- model: gpt-5.5&#10;  input_usd_per_1m: 5.00&#10;  output_usd_per_1m: 15.00"
            className="w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <input
            value={rate}
            onChange={(e) => setRateDraft(e.target.value)}
            inputMode="decimal"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          />
          <Button
            variant="outline"
            size="md"
            onClick={() => importMut.mutate()}
            loading={importMut.isPending}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            导入
          </Button>
        </div>
      </Card>
    </div>
  );
}

function SwitchField({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={[
          "flex h-10 w-full items-center justify-between rounded-[var(--radius-control)] border px-3 text-sm",
          checked
            ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
            : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
        ].join(" ")}
      >
        <span>{checked ? "开启" : "关闭"}</span>
        <span
          className={[
            "relative h-5 w-9 rounded-full transition-colors",
            checked ? "bg-[var(--accent)]" : "bg-white/10",
          ].join(" ")}
        >
          <span
            className={[
              "absolute top-0.5 h-4 w-4 rounded-full bg-[var(--bg-0)] transition-transform",
              checked ? "translate-x-4" : "translate-x-0.5",
            ].join(" ")}
          />
        </span>
      </button>
    </div>
  );
}
