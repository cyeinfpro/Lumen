"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Gift, RefreshCw, Search, Slash, UserCog, Wallet } from "lucide-react";

import {
  adjustAdminWallet,
  adminRedemptionBatchCsvUrl,
  createAdminRedemptionCodes,
  listAdminRedemptionCodeUsage,
  listAdminRedemptionCodes,
  listAdminWallets,
  revokeAdminRedemptionBatch,
  revokeAdminRedemptionCode,
  setAdminAccountMode,
} from "@/lib/apiClient";
import { Button, Card } from "@/components/ui/primitives";

export function RedemptionPanel() {
  const qc = useQueryClient();
  const codesQ = useQuery({
    queryKey: ["admin", "redemption-codes"],
    queryFn: listAdminRedemptionCodes,
    retry: false,
  });
  const [amount, setAmount] = useState("50");
  const [count, setCount] = useState("10");
  const [note, setNote] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [walletQText, setWalletQText] = useState("");
  const [walletMode, setWalletMode] = useState<"wallet" | "byok" | "all">("wallet");
  const [adjustUserId, setAdjustUserId] = useState("");
  const [modeUserId, setModeUserId] = useState("");
  const [nextMode, setNextMode] = useState<"wallet" | "byok">("wallet");
  const [residualMode, setResidualMode] = useState<"freeze" | "zero">("freeze");
  const [usageCodeId, setUsageCodeId] = useState("");
  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  const walletsQ = useQuery({
    queryKey: ["admin", "wallets", walletQText, walletMode],
    queryFn: () => listAdminWallets(walletQText, walletMode),
    enabled: walletQText.trim().length > 0,
    retry: false,
  });

  const usageQ = useQuery({
    queryKey: ["admin", "redemption-code-usage", usageCodeId],
    queryFn: () => listAdminRedemptionCodeUsage(usageCodeId),
    enabled: Boolean(usageCodeId),
    retry: false,
  });

  const createMut = useMutation({
    mutationFn: () => {
      const trimmedExpiry = expiresAt.trim();
      const isoExpiry = trimmedExpiry
        ? new Date(trimmedExpiry).toISOString()
        : null;
      return createAdminRedemptionCodes({
        amount_rmb: amount,
        count: Number(count) || 1,
        note: note || null,
        expires_at: isoExpiry,
      });
    },
    onSuccess: async (out) => {
      setStatus(`已创建 ${out.count} 张兑换码（CSV 下载在新标签页）`);
      // Why: open in a new tab so the admin doesn't lose page state, and so
      // that the 5-minute download window starts AFTER the request comes back
      // with `Content-Disposition: attachment`.
      window.open(
        adminRedemptionBatchCsvUrl(out.batch_id, out.download_token),
        "_blank",
        "noopener,noreferrer",
      );
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "创建失败"),
  });

  const revokeMut = useMutation({
    mutationFn: revokeAdminRedemptionCode,
    onSuccess: async () => {
      setStatus("兑换码已撤销");
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "撤销失败"),
  });

  const revokeBatchMut = useMutation({
    mutationFn: revokeAdminRedemptionBatch,
    onSuccess: async () => {
      setStatus("批次已撤销");
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "批次撤销失败"),
  });

  const adjustMut = useMutation({
    mutationFn: () => adjustAdminWallet(adjustUserId, adjustAmount, adjustReason),
    onSuccess: async () => {
      setStatus("钱包调账已写入");
      await qc.invalidateQueries({ queryKey: ["admin", "wallets"] });
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "调账失败"),
  });

  const modeMut = useMutation({
    mutationFn: () => setAdminAccountMode(modeUserId, nextMode, residualMode),
    onSuccess: async (out) => {
      setStatus(`${out.email} 已切换为 ${out.account_mode}`);
      await qc.invalidateQueries({ queryKey: ["admin", "wallets"] });
    },
    onError: (err) => setStatus(err instanceof Error ? err.message : "账号模式切换失败"),
  });

  return (
    <div className="space-y-5">
      {status && (
        <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-white/5 px-3 py-2 text-sm text-[var(--fg-1)]">
          {status}
        </div>
      )}

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex items-center gap-2 type-card-title">
          <Gift className="h-4 w-4" />
          批量发码
        </div>
        <div className="grid gap-3 md:grid-cols-[120px_120px_180px_1fr_auto]">
          <input
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="面额"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            value={count}
            onChange={(e) => setCount(e.target.value)}
            placeholder="数量"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            placeholder="有效期"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="备注"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <Button
            variant="primary"
            size="md"
            onClick={() => createMut.mutate()}
            loading={createMut.isPending}
            leftIcon={<Download className="h-3.5 w-3.5" />}
          >
            创建并下载
          </Button>
        </div>
      </Card>

      <Card variant="subtle" padding="none" className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
          <p className="type-card-title">兑换码</p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void codesQ.refetch()}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            刷新
          </Button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead className="text-left text-[var(--fg-2)]">
              <tr className="border-b border-[var(--border-subtle)]">
                <th className="px-4 py-2">前缀</th>
                <th className="px-4 py-2">面额</th>
                <th className="px-4 py-2">兑换</th>
                <th className="px-4 py-2">批次</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {(codesQ.data?.items ?? []).map((code) => (
                <tr key={code.id} className="border-b border-[var(--border-subtle)]">
                  <td className="px-4 py-2 font-mono">{code.code_prefix}</td>
                  <td className="px-4 py-2">¥{Number(code.amount.rmb).toFixed(2)}</td>
                  <td className="px-4 py-2">{code.redeemed_count}/{code.max_redemptions}</td>
                  <td className="px-4 py-2 font-mono text-xs">{code.batch_id}</td>
                  <td className="px-4 py-2">{code.revoked_at ? "已撤销" : "可兑"}</td>
                  <td className="px-4 py-2 text-right">
                    {!code.revoked_at && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => revokeMut.mutate(code.id)}
                        leftIcon={<Slash className="h-3.5 w-3.5" />}
                      >
                        撤销
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-2"
                      onClick={() => setUsageCodeId(code.id)}
                    >
                      兑换记录
                    </Button>
                    {!code.revoked_at && code.batch_id && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="ml-2"
                        onClick={() => code.batch_id && revokeBatchMut.mutate(code.batch_id)}
                        loading={revokeBatchMut.isPending}
                      >
                        撤销批次
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {usageCodeId && (
        <Card variant="subtle" padding="none" className="overflow-hidden">
          <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
            <p className="type-card-title">兑换记录</p>
            <Button variant="ghost" size="sm" onClick={() => setUsageCodeId("")}>
              关闭
            </Button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="text-left text-[var(--fg-2)]">
                <tr className="border-b border-[var(--border-subtle)]">
                  <th className="px-4 py-2">用户</th>
                  <th className="px-4 py-2">面额</th>
                  <th className="px-4 py-2">流水</th>
                  <th className="px-4 py-2">IP Hash</th>
                  <th className="px-4 py-2">时间</th>
                </tr>
              </thead>
              <tbody>
                {(usageQ.data?.items ?? []).map((item) => (
                  <tr key={item.id} className="border-b border-[var(--border-subtle)]">
                    <td className="px-4 py-2">
                      <span className="block truncate">{item.user_email ?? item.user_id}</span>
                      <span className="font-mono text-xs text-[var(--fg-3)]">{item.user_id}</span>
                    </td>
                    <td className="px-4 py-2">¥{Number(item.amount.rmb).toFixed(2)}</td>
                    <td className="px-4 py-2 font-mono text-xs">{item.wallet_tx_id}</td>
                    <td className="px-4 py-2 font-mono text-xs">{item.ip_hash ?? "-"}</td>
                    <td className="px-4 py-2">{new Date(item.redeemed_at).toLocaleString()}</td>
                  </tr>
                ))}
                {!usageQ.isLoading && (usageQ.data?.items ?? []).length === 0 && (
                  <tr>
                    <td className="px-4 py-6 text-[var(--fg-2)]" colSpan={5}>
                      暂无兑换记录
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex items-center gap-2 type-card-title">
          <Wallet className="h-4 w-4" />
          用户钱包调账
        </div>
        <div className="grid gap-3 md:grid-cols-[1fr_140px_auto]">
          <input
            value={walletQText}
            onChange={(e) => setWalletQText(e.target.value)}
            placeholder="搜索 email 或 user id"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <select
            value={walletMode}
            onChange={(e) => setWalletMode(e.target.value as "wallet" | "byok" | "all")}
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          >
            <option value="wallet">wallet</option>
            <option value="byok">byok</option>
            <option value="all">all</option>
          </select>
          <Button
            variant="outline"
            size="md"
            onClick={() => void walletsQ.refetch()}
            leftIcon={<Search className="h-3.5 w-3.5" />}
          >
            搜索
          </Button>
        </div>
        <div className="grid gap-2">
          {(walletsQ.data?.items ?? []).map((item) => (
            <button
              key={item.user_id}
              type="button"
              onClick={() => {
                setAdjustUserId(item.user_id);
                setModeUserId(item.user_id);
                setNextMode(item.account_mode === "wallet" ? "byok" : "wallet");
              }}
              className="grid grid-cols-[1fr_auto] gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-left"
            >
              <span className="min-w-0 truncate">
                {item.email}
                <span className="ml-2 font-mono text-xs text-[var(--fg-3)]">{item.account_mode}</span>
              </span>
              <span className="tabular-nums">
                {item.account_mode === "wallet"
                  ? `¥${Number(item.wallet.balance?.rmb ?? 0).toFixed(2)}`
                  : "BYOK"}
              </span>
            </button>
          ))}
        </div>
        <div className="grid gap-3 md:grid-cols-[1fr_120px_1fr_auto]">
          <input
            value={adjustUserId}
            onChange={(e) => setAdjustUserId(e.target.value)}
            placeholder="user id"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            value={adjustAmount}
            onChange={(e) => setAdjustAmount(e.target.value)}
            placeholder="+10 / -5"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <input
            value={adjustReason}
            onChange={(e) => setAdjustReason(e.target.value)}
            placeholder="理由"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <Button
            variant="primary"
            size="md"
            onClick={() => adjustMut.mutate()}
            loading={adjustMut.isPending}
          >
            写入
          </Button>
        </div>
        <div className="grid gap-3 border-t border-[var(--border-subtle)] pt-4 md:grid-cols-[1fr_140px_140px_auto]">
          <input
            value={modeUserId}
            onChange={(e) => setModeUserId(e.target.value)}
            placeholder="切换模式的 user id"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <select
            value={nextMode}
            onChange={(e) => setNextMode(e.target.value as "wallet" | "byok")}
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          >
            <option value="wallet">转 wallet</option>
            <option value="byok">转 byok</option>
          </select>
          <select
            value={residualMode}
            onChange={(e) => setResidualMode(e.target.value as "freeze" | "zero")}
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          >
            <option value="freeze">冻结余额</option>
            <option value="zero">清零余额</option>
          </select>
          <Button
            variant="outline"
            size="md"
            onClick={() => modeMut.mutate()}
            loading={modeMut.isPending}
            leftIcon={<UserCog className="h-3.5 w-3.5" />}
          >
            切换
          </Button>
        </div>
      </Card>
    </div>
  );
}
