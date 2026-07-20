import { API_BASE, apiFetch } from "./http";
import type {
  AdminBillingBootstrapIn,
  AdminBillingOverviewOut,
  AdminOrphanHoldOut,
  AdminPricingBulkIn,
  AdminRedemptionBatchRedownloadOut,
  AdminRedemptionCodeCreateOut,
  AdminRedemptionCodeListOut,
  AdminRedemptionUsageListOut,
  AdminWalletAuditOut,
  AdminWalletDetailOut,
  AdminWalletListOut,
  BillingSnapshotOut,
  PricingRuleUpsertIn,
  PricingRulesOut,
  RedemptionUsageListOut,
  WalletOut,
  WalletTransactionListOut,
  WalletTransactionOut,
} from "../types";

// ——— Billing / Wallet ———

export function getMyWallet(): Promise<WalletOut> {
  return apiFetch<WalletOut>("/me/wallet");
}

export function getMyBillingSnapshot(): Promise<BillingSnapshotOut> {
  return apiFetch<BillingSnapshotOut>("/me/billing/snapshot");
}

export function listMyWalletTransactions(
  opts: { cursor?: string | null; limit?: number; kind?: string | null } = {},
): Promise<WalletTransactionListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.kind) qs.set("kind", opts.kind);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<WalletTransactionListOut>(`/me/wallet/transactions${suffix}`);
}

export function listMyRedemptions(
  opts: { cursor?: string | null; limit?: number } = {},
): Promise<RedemptionUsageListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<RedemptionUsageListOut>(`/me/redemptions${suffix}`);
}

export function getPricing(): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/me/pricing");
}

export function getAdminPricing(): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing");
}

export function getAdminBillingOverview(): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>("/admin/billing/overview");
}

export function bootstrapAdminBilling(
  body: AdminBillingBootstrapIn,
): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>("/admin/billing/bootstrap", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function rotateAdminRedemptionSecret(): Promise<AdminBillingOverviewOut> {
  return apiFetch<AdminBillingOverviewOut>(
    "/admin/billing/redemption_secret:rotate",
    {
      method: "POST",
    },
  );
}

export function runAdminWalletAudit(): Promise<AdminWalletAuditOut> {
  return apiFetch<AdminWalletAuditOut>("/admin/billing/wallet_audit");
}

export function listAdminOrphanHolds(
  opts: { min_age_minutes?: number; limit?: number } = {},
): Promise<AdminOrphanHoldOut[]> {
  const qs = new URLSearchParams();
  if (opts.min_age_minutes != null)
    qs.set("min_age_minutes", String(opts.min_age_minutes));
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminOrphanHoldOut[]>(`/admin/billing/orphan_holds${suffix}`);
}

export function releaseAdminOrphanHold(
  txId: string,
): Promise<WalletTransactionOut> {
  return apiFetch<WalletTransactionOut>(
    `/admin/billing/holds/${encodeURIComponent(txId)}:release`,
    { method: "POST" },
  );
}

export function updateAdminPricing(
  items: PricingRuleUpsertIn[],
  opts: {
    image_size_thresholds?: Record<string, number>;
    force?: boolean;
  } = {},
): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing", {
    method: "PUT",
    body: JSON.stringify({ items, ...opts }),
  });
}

export function bulkUpdateAdminPricing(
  body: AdminPricingBulkIn,
): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/billing/pricing/bulk", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function importOpenAiPricing(
  content: string,
  rate = 1,
): Promise<PricingRulesOut> {
  return apiFetch<PricingRulesOut>("/admin/pricing/import_openai", {
    method: "POST",
    body: JSON.stringify({ content, rate }),
  });
}

export function listAdminRedemptionCodes(
  opts: {
    status?: "all" | "active" | "revoked" | "expired" | "exhausted";
    batch_id?: string | null;
    q?: string | null;
    cursor?: string | null;
    limit?: number;
  } = {},
): Promise<AdminRedemptionCodeListOut> {
  const qs = new URLSearchParams();
  if (opts.status) qs.set("status", opts.status);
  if (opts.batch_id) qs.set("batch_id", opts.batch_id);
  if (opts.q) qs.set("q", opts.q);
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminRedemptionCodeListOut>(
    `/admin/redemption_codes${suffix}`,
  );
}

export function createAdminRedemptionCodes(body: {
  amount_rmb: string;
  count: number;
  max_redemptions?: number;
  expires_at?: string | null;
  note?: string | null;
}): Promise<AdminRedemptionCodeCreateOut> {
  return apiFetch<AdminRedemptionCodeCreateOut>("/admin/redemption_codes", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeAdminRedemptionCode(
  id: string,
): Promise<import("../types").AdminRedemptionCodeOut> {
  return apiFetch<import("../types").AdminRedemptionCodeOut>(
    `/admin/redemption_codes/${encodeURIComponent(id)}:revoke`,
    { method: "POST" },
  );
}

export function listAdminRedemptionCodeUsage(
  id: string,
): Promise<AdminRedemptionUsageListOut> {
  return apiFetch<AdminRedemptionUsageListOut>(
    `/admin/redemption_codes/${encodeURIComponent(id)}/usage`,
  );
}

export function revokeAdminRedemptionBatch(
  batchId: string,
): Promise<AdminRedemptionCodeListOut> {
  return apiFetch<AdminRedemptionCodeListOut>(
    `/admin/redemption_codes/batches/${encodeURIComponent(batchId)}:revoke`,
    { method: "POST" },
  );
}

export function adminRedemptionBatchCsvUrl(
  batchId: string,
  token: string,
): string {
  return `${API_BASE}/admin/redemption_codes/batches/${encodeURIComponent(batchId)}.csv?download_token=${encodeURIComponent(token)}`;
}

export function adminRedemptionBatchTxtUrl(
  batchId: string,
  token: string,
): string {
  return `${API_BASE}/admin/redemption_codes/batches/${encodeURIComponent(batchId)}.txt?download_token=${encodeURIComponent(token)}`;
}

export function redownloadAdminRedemptionBatch(
  batchId: string,
): Promise<AdminRedemptionBatchRedownloadOut> {
  return apiFetch<AdminRedemptionBatchRedownloadOut>(
    `/admin/redemption_codes/batches/${encodeURIComponent(batchId)}/redownload`,
    { method: "POST" },
  );
}

export function listAdminWallets(
  q?: string,
  mode: "wallet" | "byok" | "all" = "wallet",
  opts: { cursor?: string | null; limit?: number } = {},
): Promise<AdminWalletListOut> {
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  qs.set("mode", mode);
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<AdminWalletListOut>(`/admin/wallets${suffix}`);
}

export function getAdminWalletDetail(
  userId: string,
): Promise<AdminWalletDetailOut> {
  return apiFetch<AdminWalletDetailOut>(
    `/admin/wallets/${encodeURIComponent(userId)}`,
  );
}

export function listAdminWalletTransactions(
  userId: string,
  opts: {
    cursor?: string | null;
    limit?: number;
    kind?: string | null;
    ref_type?: string | null;
    ref_id?: string | null;
  } = {},
): Promise<WalletTransactionListOut> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.kind) qs.set("kind", opts.kind);
  if (opts.ref_type) qs.set("ref_type", opts.ref_type);
  if (opts.ref_id) qs.set("ref_id", opts.ref_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<WalletTransactionListOut>(
    `/admin/wallets/${encodeURIComponent(userId)}/transactions${suffix}`,
  );
}

export function adjustAdminWallet(
  userId: string,
  amount_rmb_signed: string,
  reason: string,
): Promise<WalletTransactionOut> {
  return apiFetch<WalletTransactionOut>(`/admin/wallets/${userId}:adjust`, {
    method: "POST",
    body: JSON.stringify({ amount_rmb_signed, reason }),
  });
}

export function setAdminAccountMode(
  userId: string,
  mode: "wallet" | "byok",
  on_residual_balance: "freeze" | "zero" = "freeze",
): Promise<import("../types").AdminWalletOut> {
  return apiFetch<import("../types").AdminWalletOut>(
    `/admin/users/${userId}:set_account_mode`,
    {
      method: "POST",
      body: JSON.stringify({ mode, on_residual_balance }),
    },
  );
}
