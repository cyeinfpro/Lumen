import type { AdminBillingOverviewOut } from "@/lib/types";

export type BillingHealthItem = {
  label: string;
  ok: boolean;
  value: string;
};

export function buildBillingHealth(
  overview?: AdminBillingOverviewOut,
): BillingHealthItem[] {
  const missingPrices =
    overview?.thresholds_missing_prices.join(", ") || "-";
  return [
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
      value: overview?.thresholds_pricing_aligned
        ? "已对齐"
        : `缺少 ${missingPrices}`,
    },
  ];
}

export function shouldShowBillingBootstrap(
  overview?: AdminBillingOverviewOut,
): boolean {
  if (!overview) return false;
  return !overview.bootstrap_completed || !overview.redemption_secret_configured;
}
