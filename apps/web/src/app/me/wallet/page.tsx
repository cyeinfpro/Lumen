"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, CreditCard, Gift, RefreshCw } from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card, toast } from "@/components/ui/primitives";
import {
  getMyBillingSnapshot,
  getMyWallet,
  listMyWalletTransactions,
  listMyRedemptions,
  redeemCode,
  type AuthUser,
  getMe,
} from "@/lib/apiClient";
import { errorToText, mapError } from "@/lib/errors";
import { formatRmb } from "@/lib/money";

function formatKind(kind: string): string {
  const labels: Record<string, string> = {
    topup_redeem: "兑换充值",
    hold: "预扣",
    settle: "结算",
    release: "释放",
    charge: "对话扣费",
    charge_completion: "对话扣费",
    refund: "退款",
    adjust_admin: "管理员调账",
    grant: "赠送",
  };
  return labels[kind] ?? kind;
}

function normalizeCode(value: string): string {
  const raw = value.toUpperCase().replace(/[^A-Z0-9]/g, "").replace(/^LMN/, "");
  const chunks = raw.slice(0, 16).match(/.{1,4}/g) ?? [];
  return chunks.length ? `LMN-${chunks.join("-")}` : "LMN-";
}

const TX_KIND_FILTERS = [
  { key: "all", label: "全部" },
  { key: "topup_redeem", label: "兑换充值" },
  { key: "hold", label: "预扣" },
  { key: "settle", label: "结算" },
  { key: "release", label: "释放" },
  { key: "charge", label: "扣费" },
] as const;

function microMoney(value?: number | null): string {
  return ((value ?? 0) / 1_000_000).toFixed(2);
}

export default function WalletPage() {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [txKind, setTxKind] = useState("all");

  const meQuery = useQuery<AuthUser>({ queryKey: ["me"], queryFn: getMe, retry: false });
  const walletQ = useQuery({ queryKey: ["me", "wallet"], queryFn: getMyWallet, retry: false });
  const snapshotQ = useQuery({
    queryKey: ["me", "billing", "snapshot"],
    queryFn: getMyBillingSnapshot,
    retry: false,
    enabled: meQuery.data?.account_mode === "wallet",
  });
  const txQ = useInfiniteQuery({
    queryKey: ["me", "wallet", "transactions", txKind],
    queryFn: ({ pageParam }) =>
      listMyWalletTransactions({
        cursor: pageParam,
        kind: txKind === "all" ? undefined : txKind,
        limit: 30,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    enabled: meQuery.data?.account_mode === "wallet",
  });
  const redemptionsQ = useInfiniteQuery({
    queryKey: ["me", "redemptions"],
    queryFn: ({ pageParam }) => listMyRedemptions({ cursor: pageParam, limit: 20 }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    enabled: meQuery.data?.account_mode === "wallet",
  });

  const wallet = walletQ.data;
  const snapshot = snapshotQ.data;
  const txItems = useMemo(
    () => txQ.data?.pages.flatMap((page) => page.items) ?? [],
    [txQ.data],
  );
  const redemptionItems = useMemo(
    () => redemptionsQ.data?.pages.flatMap((page) => page.items) ?? [],
    [redemptionsQ.data],
  );
  const low = useMemo(() => {
    if (!wallet?.balance || !wallet.low_balance_threshold) return false;
    return wallet.balance.micro < wallet.low_balance_threshold.micro;
  }, [wallet]);
  const stats24h = useMemo(() => {
    const latest = Math.max(0, ...txItems.map((tx) => Date.parse(tx.created_at)));
    if (latest <= 0) return { topup: 0, spend: 0 };
    const since = latest - 24 * 60 * 60 * 1000;
    let topup = 0;
    let spend = 0;
    for (const tx of txItems) {
      if (Date.parse(tx.created_at) < since) continue;
      if (tx.amount.micro > 0) topup += tx.amount.micro;
      if (tx.amount.micro < 0) spend += Math.abs(tx.amount.micro);
    }
    return { topup: topup / 1_000_000, spend: spend / 1_000_000 };
  }, [txItems]);

  const redeemMut = useMutation({
    mutationFn: () => redeemCode(code),
    onSuccess: async (out) => {
      const amountText = `+¥${formatRmb(out.amount.rmb)}`;
      setCode("");
      setMessage(amountText);
      toast.success("兑换成功", { description: amountText });
      await qc.invalidateQueries({ queryKey: ["me", "wallet"] });
      await qc.invalidateQueries({ queryKey: ["me", "billing", "snapshot"] });
    },
    onError: (err) => {
      const normalized = mapError(err);
      const description = errorToText(err);
      setMessage(description);
      toast.error(normalized.title, { description });
    },
  });

  if (wallet?.mode === "byok" || meQuery.data?.account_mode === "byok") {
    return (
      <SettingsShell title="钱包" subtitle="BYOK" maxWidth="max-w-3xl">
        <Card variant="subtle" padding="lg" className="space-y-3">
          <p className="type-card-title">BYOK 账号</p>
          <p className="type-body">
            你的账号由 BYOK 自助注册流程创建，所以费用直接由你在 OpenAI/Claude 等上游账单结算，Lumen 不维护钱包余额。
          </p>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-xs text-[var(--fg-0)] hover:bg-[var(--bg-2)]"
          >
            返回我的
          </Link>
        </Card>
      </SettingsShell>
    );
  }

  return (
    <SettingsShell title="钱包" subtitle="余额与兑换码" maxWidth="max-w-4xl">
      <div className="space-y-6">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">钱包</h1>
            <p className="type-body mt-1.5">查看余额、兑换额度和流水。</p>
          </div>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        {low && (
          <div className="flex items-center gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 text-sm text-[var(--danger-fg)]">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>余额不足，4K 图或多图任务可能无法生成。请先兑换充值或联系管理员。</span>
          </div>
        )}

        <div className="grid gap-4 md:grid-cols-[1fr_1.2fr]">
          <Card variant="default" padding="lg" className="min-h-[180px] space-y-4">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
                <CreditCard className="h-4 w-4" />
              </div>
              <div>
                <p className="type-caption text-[var(--fg-2)]">可用余额</p>
                <p className={low ? "type-page-title-sm font-mono tabular-nums text-[var(--danger-fg)]" : "type-page-title-sm font-mono tabular-nums"}>
                  {walletQ.isLoading ? "…" : `¥${formatRmb(wallet?.balance?.rmb)}`}
                </p>
              </div>
            </div>
            <p className="type-body-sm text-[var(--fg-2)]">
              预扣 ¥{formatRmb(wallet?.hold?.rmb)}
            </p>
            <p className="type-caption font-mono tabular-nums text-[var(--fg-2)]">
              24h 变化 +¥{stats24h.topup.toFixed(2)} / -¥{stats24h.spend.toFixed(2)}
            </p>
          </Card>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              redeemMut.mutate();
            }}
            className="grid min-h-[180px] grid-rows-[auto_1fr_auto] gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-5"
          >
            <div className="flex items-center gap-2 type-overline">
              <Gift className="h-3.5 w-3.5" />
              兑换码
            </div>
            <div className="space-y-2">
              <input
                value={code}
                onChange={(e) => setCode(normalizeCode(e.target.value))}
                placeholder="LMN-XXXX-XXXX-XXXX-XXXX"
                className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base tracking-[0.06em] outline-none focus:border-[var(--accent)]/50 sm:text-lg"
              />
              {message && (
                <div className="flex items-center gap-2 type-body-sm text-[var(--fg-1)]">
                  <Check className="h-4 w-4" />
                  {message}
                </div>
              )}
            </div>
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={redeemMut.isPending || code.replace(/[^A-Z0-9]/g, "").length < 19}
              loading={redeemMut.isPending}
              fullWidth
            >
              兑换
            </Button>
          </form>
        </div>

        {snapshot && (
          <Card variant="subtle" padding="lg" className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="type-card-title">30 天费用构成</p>
                <p className="type-caption text-[var(--fg-2)]">
                  费率倍率 {snapshot.billing_rate_multiplier}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void snapshotQ.refetch()}
                leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
              >
                刷新
              </Button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {[
                ["输入", snapshot.by_kind_30d.input],
                ["输出", snapshot.by_kind_30d.output],
                ["缓存读取", snapshot.by_kind_30d.cache_read],
                ["缓存写入", snapshot.by_kind_30d.cache_creation],
                ["图片", snapshot.by_kind_30d.image],
                ["推理", snapshot.by_kind_30d.reasoning],
              ].map(([label, value]) => (
                <div
                  key={label}
                  className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3"
                >
                  <p className="type-caption text-[var(--fg-2)]">{label}</p>
                  <p className="mt-1 text-base font-semibold tabular-nums">
                    ¥{microMoney(Number(value))}
                  </p>
                </div>
              ))}
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              {(["5h", "1d", "7d"] as const).map((key) => {
                const win = snapshot.windows[key];
                const pct =
                  win && win.limit_micro > 0
                    ? Math.min(100, Math.round((win.used_micro / win.limit_micro) * 100))
                    : 0;
                return (
                  <div
                    key={key}
                    className="min-h-[112px] rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3"
                  >
                    <div className="flex items-center justify-between text-xs text-[var(--fg-2)]">
                      <span>{key} 限额</span>
                      <span>
                        ¥{microMoney(win?.used_micro)} /{" "}
                        {win?.limit_micro ? `¥${microMoney(win.limit_micro)}` : "不限"}
                      </span>
                    </div>
                    <div className="mt-2 h-1.5 rounded-full bg-[var(--bg-2)]">
                      <div
                        className="h-full rounded-full bg-[var(--accent)]"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    {win?.resets_at && (
                      <p className="mt-2 type-caption text-[var(--fg-2)]">
                        重置 {new Date(win.resets_at).toLocaleString()}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        )}

        <Card variant="subtle" padding="none" className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
            <p className="type-card-title">流水</p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void txQ.refetch()}
              leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
            >
              刷新
            </Button>
          </div>
          <div className="scrollbar-thin flex flex-wrap gap-2 border-b border-[var(--border-subtle)] px-4 py-3 md:flex-nowrap md:overflow-x-auto md:overscroll-x-contain">
            {TX_KIND_FILTERS.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setTxKind(item.key)}
                className={[
                  "shrink-0 rounded-full border min-h-9 px-3 text-xs",
                  txKind === item.key
                    ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
                    : "border-[var(--border)] text-[var(--fg-2)]",
                ].join(" ")}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="divide-y divide-[var(--border-subtle)]">
            {txItems.map((tx) => (
              <div key={tx.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3">
                <div className="min-w-0">
                  <p className="truncate type-body-sm text-[var(--fg-0)]">{formatKind(tx.kind)}</p>
                  <p className="type-caption text-[var(--fg-2)]">
                    {new Date(tx.created_at).toLocaleString()}
                  </p>
                </div>
                <div className="text-right tabular-nums">
                  <p className={tx.amount.micro >= 0 ? "text-success" : "text-[var(--fg-0)]"}>
                    {tx.amount.micro >= 0 ? "+" : ""}¥{formatRmb(tx.amount.rmb)}
                  </p>
                  <p className="type-caption text-[var(--fg-2)]">
                    余额 ¥{formatRmb(tx.balance_after.rmb)}
                  </p>
                </div>
              </div>
            ))}
            {!txQ.isLoading && txItems.length === 0 && (
              <div className="px-4 py-8 text-center type-body-sm text-[var(--fg-2)]">
                暂无流水
              </div>
            )}
          </div>
          {txQ.hasNextPage && (
            <div className="border-t border-[var(--border-subtle)] px-4 py-3 text-center">
              <Button
                variant="outline"
                size="sm"
                loading={txQ.isFetchingNextPage}
                onClick={() => void txQ.fetchNextPage()}
              >
                加载更多
              </Button>
            </div>
          )}
        </Card>

        <Card variant="subtle" padding="none" className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
            <p className="type-card-title">我的兑换历史</p>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                navigator.clipboard
                  .writeText(
                    redemptionItems
                      .map((item) => `${new Date(item.redeemed_at).toLocaleString()} ¥${formatRmb(item.amount.rmb)}`)
                      .join("\n"),
                  )
                  .then(() => toast.success("兑换记录已复制"))
                  .catch(() => toast.error("复制失败"))
              }
            >
              复制记录
            </Button>
          </div>
          <div className="divide-y divide-[var(--border-subtle)]">
            {redemptionItems.map((item) => (
              <div key={item.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3">
                <div className="min-w-0">
                  <p className="type-body-sm text-[var(--fg-0)]">兑换码充值</p>
                  <p className="type-caption text-[var(--fg-2)]">
                    {new Date(item.redeemed_at).toLocaleString()}
                  </p>
                </div>
                <div className="text-right tabular-nums">
                  <p className="text-success">+¥{formatRmb(item.amount.rmb)}</p>
                  <p className="max-w-[44vw] truncate type-caption text-[var(--fg-2)] md:max-w-none">{item.code_id}</p>
                </div>
              </div>
            ))}
            {!redemptionsQ.isLoading && redemptionItems.length === 0 && (
              <div className="px-4 py-8 text-center type-body-sm text-[var(--fg-2)]">
                暂无兑换记录
              </div>
            )}
          </div>
          {redemptionsQ.hasNextPage && (
            <div className="border-t border-[var(--border-subtle)] px-4 py-3 text-center">
              <Button
                variant="outline"
                size="sm"
                loading={redemptionsQ.isFetchingNextPage}
                onClick={() => void redemptionsQ.fetchNextPage()}
              >
                加载更多
              </Button>
            </div>
          )}
        </Card>
      </div>
    </SettingsShell>
  );
}
