"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, CreditCard, Gift, RefreshCw } from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card, toast } from "@/components/ui/primitives";
import {
  getMyWallet,
  listMyWalletTransactions,
  redeemCode,
  type AuthUser,
  getMe,
} from "@/lib/apiClient";
import { errorToText, mapError } from "@/lib/errors";

function formatKind(kind: string): string {
  const labels: Record<string, string> = {
    topup_redeem: "兑换充值",
    hold: "预扣",
    settle: "结算",
    release: "释放",
    charge: "对话扣费",
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

export default function WalletPage() {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const meQuery = useQuery<AuthUser>({ queryKey: ["me"], queryFn: getMe, retry: false });
  const walletQ = useQuery({ queryKey: ["me", "wallet"], queryFn: getMyWallet, retry: false });
  const txQ = useQuery({
    queryKey: ["me", "wallet", "transactions"],
    queryFn: () => listMyWalletTransactions(),
    retry: false,
    // Why: gate on positive identification, not negation of "byok". With the
    // negated form, the query fires while `meQuery.data` is still undefined
    // and BYOK users get a 403 toast flash on first paint.
    enabled: meQuery.data?.account_mode === "wallet",
  });

  const wallet = walletQ.data;
  const low = useMemo(() => {
    if (!wallet?.balance || !wallet.low_balance_threshold) return false;
    return wallet.balance.micro < wallet.low_balance_threshold.micro;
  }, [wallet]);

  const redeemMut = useMutation({
    mutationFn: () => redeemCode(code),
    onSuccess: async (out) => {
      const amountText = `+¥${Number(out.amount.rmb).toFixed(2)}`;
      setCode("");
      setMessage(amountText);
      toast.success("兑换成功", { description: amountText });
      await qc.invalidateQueries({ queryKey: ["me", "wallet"] });
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
            你的账号通过 BYOK 注册，费用由上游 API 账单结算。
          </p>
          <Link
            href="/me"
            className="inline-flex h-8 items-center rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-xs text-[var(--fg-0)] hover:bg-white/4"
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
            className="inline-flex items-center gap-1.5 type-body-sm text-[var(--fg-1)] hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        <div className="grid gap-4 md:grid-cols-[1fr_1.2fr]">
          <Card variant="default" padding="lg" className="space-y-4">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-white/5">
                <CreditCard className="h-4 w-4" />
              </div>
              <div>
                <p className="type-caption text-[var(--fg-2)]">可用余额</p>
                <p className={low ? "type-page-title-sm text-[var(--danger-fg)]" : "type-page-title-sm"}>
                  {walletQ.isLoading ? "…" : `¥${Number(wallet?.balance?.rmb ?? 0).toFixed(2)}`}
                </p>
              </div>
            </div>
            <p className="type-body-sm text-[var(--fg-2)]">
              预扣 ¥{Number(wallet?.hold?.rmb ?? 0).toFixed(2)}
            </p>
          </Card>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              redeemMut.mutate();
            }}
            className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-5 space-y-3"
          >
            <div className="flex items-center gap-2 type-overline">
              <Gift className="h-3.5 w-3.5" />
              兑换码
            </div>
            <input
              value={code}
              onChange={(e) => setCode(normalizeCode(e.target.value))}
              placeholder="LMN-XXXX-XXXX-XXXX-XXXX"
              className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base tracking-[0.08em] outline-none focus:border-[var(--accent)]/50"
            />
            {message && (
              <div className="flex items-center gap-2 type-body-sm text-[var(--fg-1)]">
                <Check className="h-4 w-4" />
                {message}
              </div>
            )}
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
          <div className="divide-y divide-[var(--border-subtle)]">
            {(txQ.data?.items ?? []).map((tx) => (
              <div key={tx.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3">
                <div className="min-w-0">
                  <p className="type-body-sm text-[var(--fg-0)]">{formatKind(tx.kind)}</p>
                  <p className="type-caption text-[var(--fg-2)]">
                    {new Date(tx.created_at).toLocaleString()}
                  </p>
                </div>
                <div className="text-right tabular-nums">
                  <p className={tx.amount.micro >= 0 ? "text-success" : "text-[var(--fg-0)]"}>
                    {tx.amount.micro >= 0 ? "+" : ""}¥{Number(tx.amount.rmb).toFixed(2)}
                  </p>
                  <p className="type-caption text-[var(--fg-2)]">
                    余额 ¥{Number(tx.balance_after.rmb).toFixed(2)}
                  </p>
                </div>
              </div>
            ))}
            {!txQ.isLoading && (txQ.data?.items ?? []).length === 0 && (
              <div className="px-4 py-8 text-center type-body-sm text-[var(--fg-2)]">
                暂无流水
              </div>
            )}
          </div>
        </Card>
      </div>
    </SettingsShell>
  );
}
