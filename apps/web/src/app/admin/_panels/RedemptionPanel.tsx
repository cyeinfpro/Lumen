"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  MoreHorizontal,
  Copy,
  Download,
  Gift,
  RefreshCw,
  Search,
  Slash,
  UserCog,
  Wallet,
  X,
} from "lucide-react";

import {
  adjustAdminWallet,
  adminRedemptionBatchCsvUrl,
  adminRedemptionBatchTxtUrl,
  createAdminRedemptionCodes,
  getAdminWalletDetail,
  listAdminRedemptionCodeUsage,
  listAdminRedemptionCodes,
  listAdminWallets,
  listAdminWalletTransactions,
  redownloadAdminRedemptionBatch,
  revokeAdminRedemptionBatch,
  revokeAdminRedemptionCode,
  setAdminAccountMode,
} from "@/lib/apiClient";
import type { AdminRedemptionCodeOut, AdminRedemptionCodeCreateOut } from "@/lib/types";
import { Button, Card, toast } from "@/components/ui/primitives";
import { ActionSheet, type ActionItem } from "@/components/ui/primitives/mobile";
import { formatRmb } from "@/lib/money";

type Section = "codes" | "wallets" | "all";
type CodeStatus = "all" | "active" | "revoked" | "expired" | "exhausted";

const STATUS_LABEL: Record<CodeStatus, string> = {
  all: "全部",
  active: "可兑",
  exhausted: "已兑完",
  revoked: "撤销",
  expired: "过期",
};

const TX_KIND_LABEL: Record<string, string> = {
  all: "全部",
  hold: "预扣",
  settle: "结算",
  release: "释放",
  charge: "扣费",
  charge_completion: "扣费",
  topup_redeem: "兑换充值",
  adjust_admin: "管理员调账",
};

interface NewCodesModalState {
  batchId: string;
  amountRmb: string;
  downloadToken: string;
  codes: string[];
}

export function RedemptionPanel({ section = "all" }: { section?: Section }) {
  return (
    <div className="space-y-5">
      {section !== "wallets" && <CodesSubpanel />}
      {section !== "codes" && <UserWalletsSubpanel />}
    </div>
  );
}

function formatMoney(value?: string | null): string {
  return formatRmb(value);
}

async function copyText(text: string, label = "已复制") {
  await navigator.clipboard.writeText(text);
  toast.success(label);
}

function codeStatusLabel(code: AdminRedemptionCodeOut): string {
  return STATUS_LABEL[code.status] ?? code.status;
}

function CodesSubpanel() {
  const qc = useQueryClient();
  const [amount, setAmount] = useState("50");
  const [count, setCount] = useState("10");
  const [maxRedemptions, setMaxRedemptions] = useState("1");
  const [note, setNote] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [status, setStatus] = useState<CodeStatus>("active");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [usageCodeId, setUsageCodeId] = useState("");
  const [modal, setModal] = useState<NewCodesModalState | null>(null);
  const [actionsCode, setActionsCode] = useState<AdminRedemptionCodeOut | null>(null);

  const codesQ = useQuery({
    queryKey: ["admin", "redemption-codes", status, q, cursor],
    queryFn: () =>
      listAdminRedemptionCodes({
        status,
        q: q || undefined,
        cursor,
        limit: 100,
      }),
    retry: false,
  });

  const usageQ = useQuery({
    queryKey: ["admin", "redemption-code-usage", usageCodeId],
    queryFn: () => listAdminRedemptionCodeUsage(usageCodeId),
    enabled: Boolean(usageCodeId),
    retry: false,
  });

  const openCodesModal = (out: AdminRedemptionCodeCreateOut) => {
    setModal({
      batchId: out.batch_id,
      amountRmb: out.amount.rmb,
      downloadToken: out.download_token,
      codes: out.plaintext_codes ?? [],
    });
  };

  const createMut = useMutation({
    mutationFn: () => {
      const trimmedExpiry = expiresAt.trim();
      const isoExpiry = trimmedExpiry ? new Date(trimmedExpiry).toISOString() : null;
      return createAdminRedemptionCodes({
        amount_rmb: amount,
        count: Number(count) || 1,
        max_redemptions: Math.max(1, Number(maxRedemptions) || 1),
        note: note || null,
        expires_at: isoExpiry,
      });
    },
    onSuccess: async (out) => {
      openCodesModal(out);
      toast.success(`已创建 ${out.count} 张兑换码`);
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
      await qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] });
    },
    onError: (err) => toast.error("创建失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const redownloadMut = useMutation({
    mutationFn: redownloadAdminRedemptionBatch,
    onSuccess: (out) => {
      setModal({
        batchId: out.batch_id,
        amountRmb: "-",
        downloadToken: out.download_token,
        codes: out.plaintext_codes,
      });
      toast.success("已重新取回明文码");
    },
    onError: (err) => toast.error("明文已过期", { description: err instanceof Error ? err.message : undefined }),
  });

  const revokeMut = useMutation({
    mutationFn: revokeAdminRedemptionCode,
    onSuccess: async () => {
      toast.success("兑换码已撤销");
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
      await qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] });
    },
    onError: (err) => toast.error("撤销失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const revokeBatchMut = useMutation({
    mutationFn: revokeAdminRedemptionBatch,
    onSuccess: async () => {
      toast.success("批次已撤销");
      await qc.invalidateQueries({ queryKey: ["admin", "redemption-codes"] });
      await qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] });
    },
    onError: (err) => toast.error("批次撤销失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const totalValue = useMemo(() => {
    const a = Number(amount) || 0;
    const c = Math.max(1, Number(count) || 1);
    return a * c;
  }, [amount, count]);
  const actionSheetActions = useMemo<ActionItem[]>(() => {
    if (!actionsCode) return [];
    const code = actionsCode;
    const actions: ActionItem[] = [
      {
        key: "prefix",
        label: "复制前缀",
        icon: <Copy className="h-4 w-4" />,
        onSelect: () => void copyText(code.code_prefix, "前缀已复制"),
      },
    ];
    if (code.batch_id) {
      const batchId = code.batch_id;
      actions.push({
        key: "redownload",
        label: "重新查看",
        icon: <Download className="h-4 w-4" />,
        disabled: redownloadMut.isPending,
        onSelect: () => redownloadMut.mutate(batchId),
      });
    }
    actions.push({
      key: "usage",
      label: "查看记录",
      onSelect: () => setUsageCodeId(code.id),
    });
    if (!code.revoked_at) {
      actions.push({
        key: "revoke",
        label: "撤销兑换码",
        icon: <Slash className="h-4 w-4" />,
        destructive: true,
        onSelect: () => {
          if (window.confirm("确认撤销这张兑换码？")) revokeMut.mutate(code.id);
        },
      });
    }
    if (!code.revoked_at && code.batch_id) {
      const batchId = code.batch_id;
      actions.push({
        key: "revoke-batch",
        label: "撤销批次",
        destructive: true,
        onSelect: () => {
          if (window.confirm("确认撤销整个批次？")) revokeBatchMut.mutate(batchId);
        },
      });
    }
    return actions;
  }, [actionsCode, redownloadMut, revokeBatchMut, revokeMut]);

  return (
    <>
      <Card variant="subtle" padding="lg" className="space-y-4">
        <div className="flex items-center gap-2 type-card-title">
          <Gift className="h-4 w-4" />
          批量发码
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1.5">
            <span className="block min-h-4 type-caption text-[var(--fg-2)]">面额 (¥/张)</span>
            <input
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              inputMode="decimal"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1.5">
            <span className="block min-h-4 type-caption text-[var(--fg-2)]">数量</span>
            <input
              value={count}
              onChange={(e) => setCount(e.target.value)}
              inputMode="numeric"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1.5">
            <span className="block min-h-4 type-caption text-[var(--fg-2)]">每码最大兑换次数</span>
            <input
              value={maxRedemptions}
              onChange={(e) => setMaxRedemptions(e.target.value)}
              inputMode="numeric"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
          <label className="space-y-1.5">
            <span className="block min-h-4 type-caption text-[var(--fg-2)]">有效期</span>
            <input
              type="datetime-local"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
          </label>
        </div>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="备注"
          className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm md:max-w-[480px]"
        />
        <div className="flex flex-col-reverse items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="type-body-sm text-[var(--fg-2)]">
            本批次总价值 ¥{totalValue.toFixed(2)}
          </p>
          <Button
            variant="primary"
            size="md"
            className="w-full sm:w-auto"
            onClick={() => {
              if ((Number(count) || 1) > 200 && !window.confirm("将生成超过 200 张明文 code，确认继续？")) {
                return;
              }
              createMut.mutate();
            }}
            loading={createMut.isPending}
          >
            创建兑换码
          </Button>
        </div>
      </Card>

      <Card variant="subtle" padding="none" className="overflow-hidden">
        <div className="space-y-3 border-b border-[var(--border-subtle)] px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
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
          <div className="scrollbar-thin flex flex-nowrap items-center gap-2 overflow-x-auto overscroll-x-contain">
            {(Object.keys(STATUS_LABEL) as CodeStatus[]).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => {
                  setStatus(item);
                  setCursor(null);
                }}
                className={[
                  "shrink-0 rounded-full border px-3 py-1 text-xs",
                  status === item
                    ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
                    : "border-[var(--border)] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                ].join(" ")}
              >
                {STATUS_LABEL[item]}
              </button>
            ))}
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setCursor(null);
              setQ(qInput.trim());
            }}
            className="grid gap-2 md:grid-cols-[1fr_auto]"
          >
            <input
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              placeholder="搜索前缀或 batch id"
              className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
            />
            <Button variant="outline" size="md" type="submit" fullWidth leftIcon={<Search className="h-3.5 w-3.5" />}>
              搜索
            </Button>
          </form>
        </div>
        <div className="data-stack-on-mobile md:overflow-x-auto">
          <table className="w-full text-sm md:min-w-[980px]">
            <thead className="text-left text-[var(--fg-2)]">
              <tr className="border-b border-[var(--border-subtle)]">
                <th className="px-4 py-2">前缀</th>
                <th className="px-4 py-2">面额</th>
                <th className="px-4 py-2">兑换</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2">批次</th>
                <th className="px-4 py-2">备注</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {(codesQ.data?.items ?? []).map((code) => (
                <tr key={code.id} className="border-b border-[var(--border-subtle)]">
                  <td data-label="前缀" className="px-4 py-2 font-mono">{code.code_prefix}</td>
                  <td data-label="面额" className="px-4 py-2 tabular-nums">¥{formatMoney(code.amount.rmb)}</td>
                  <td data-label="兑换" className="px-4 py-2 tabular-nums">
                    {code.redeemed_count} / {code.max_redemptions}
                    <span className="ml-1 text-[var(--fg-3)]">可用 {code.usable_count}</span>
                  </td>
                  <td data-label="状态" className="px-4 py-2">{codeStatusLabel(code)}</td>
                  <td data-label="批次" className="px-4 py-2">
                    <button
                      type="button"
                      onClick={() => code.batch_id && copyText(code.batch_id, "批次已复制")}
                      className="max-w-full truncate font-mono text-xs text-[var(--fg-1)] hover:text-[var(--fg-0)] md:max-w-[160px]"
                    >
                      {code.batch_id ?? "-"}
                    </button>
                  </td>
                  <td data-label="备注" className="px-4 py-2 text-[var(--fg-2)]">
                    <span className="line-clamp-2 md:block md:max-w-[180px] md:truncate">{code.note ?? "-"}</span>
                  </td>
                  <td data-actions="true" className="px-4 py-2 text-right">
                    <div className="hidden justify-end md:flex md:flex-wrap md:gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => copyText(code.code_prefix, "前缀已复制")}
                        leftIcon={<Copy className="h-3.5 w-3.5" />}
                      >
                        前缀
                      </Button>
                      {code.batch_id && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => redownloadMut.mutate(code.batch_id!)}
                          loading={redownloadMut.isPending}
                        >
                          重新查看
                        </Button>
                      )}
                      {!code.revoked_at && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            if (window.confirm("确认撤销这张兑换码？")) revokeMut.mutate(code.id);
                          }}
                          leftIcon={<Slash className="h-3.5 w-3.5" />}
                        >
                          撤销
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setUsageCodeId(code.id)}
                      >
                        记录
                      </Button>
                      {!code.revoked_at && code.batch_id && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            if (window.confirm("确认撤销整个批次？")) {
                              revokeBatchMut.mutate(code.batch_id!);
                            }
                          }}
                          loading={revokeBatchMut.isPending}
                        >
                          撤销批次
                        </Button>
                      )}
                    </div>
                    <div className="md:hidden">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setActionsCode(code)}
                        leftIcon={<MoreHorizontal className="h-3.5 w-3.5" />}
                      >
                        更多
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
              {!codesQ.isLoading && (codesQ.data?.items ?? []).length === 0 && (
                <tr>
                  <td className="px-4 py-8 text-center text-[var(--fg-2)]" colSpan={7}>
                    暂无兑换码
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="flex justify-end border-t border-[var(--border-subtle)] px-4 py-3">
          <Button
            variant="outline"
            size="sm"
            disabled={!codesQ.data?.next_cursor}
            onClick={() => setCursor(codesQ.data?.next_cursor ?? null)}
          >
            下一页
          </Button>
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
          <div className="data-stack-on-mobile md:overflow-x-auto">
            <table className="w-full text-sm md:min-w-[720px]">
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
                    <td data-label="用户" className="px-4 py-2">
                      <span className="block min-w-0 truncate">{item.user_email ?? item.user_id}</span>
                      <span className="block min-w-0 truncate font-mono text-xs text-[var(--fg-3)]">{item.user_id}</span>
                    </td>
                    <td data-label="面额" className="px-4 py-2 tabular-nums">¥{formatMoney(item.amount.rmb)}</td>
                    <td data-label="流水" className="truncate px-4 py-2 font-mono text-xs">{item.wallet_tx_id}</td>
                    <td data-label="IP Hash" className="truncate px-4 py-2 font-mono text-xs">{item.ip_hash ?? "-"}</td>
                    <td data-label="时间" className="px-4 py-2">{new Date(item.redeemed_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {modal && <NewCodesModal state={modal} onClose={() => setModal(null)} />}
      <ActionSheet
        open={Boolean(actionsCode)}
        onClose={() => setActionsCode(null)}
        title={actionsCode?.code_prefix ?? "兑换码"}
        actions={actionSheetActions}
      />
    </>
  );
}

function NewCodesModal({
  state,
  onClose,
}: {
  state: NewCodesModalState;
  onClose: () => void;
}) {
  const allCodes = state.codes.join("\n");
  return (
    <div className="fixed inset-0 z-[var(--z-dialog,90)] flex items-end justify-center bg-black/60 backdrop-blur-sm mobile-dialog-shell sm:items-center">
      <div className="mobile-dialog-panel flex w-full max-w-3xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:rounded-[var(--radius-panel)] sm:border-b">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <p className="type-card-title">已生成 {state.codes.length} 张兑换码</p>
            <p className="truncate type-caption text-[var(--fg-2)]">批次 {state.batchId}</p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="shrink-0 rounded-full p-2 text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 border-b border-[var(--border)] px-5 py-3">
          <Button
            variant="primary"
            size="sm"
            onClick={() => copyText(allCodes, "全部兑换码已复制")}
            leftIcon={<Copy className="h-3.5 w-3.5" />}
          >
            复制全部
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              window.open(adminRedemptionBatchCsvUrl(state.batchId, state.downloadToken), "_blank", "noopener,noreferrer")
            }
            leftIcon={<Download className="h-3.5 w-3.5" />}
          >
            下载 CSV
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              window.open(adminRedemptionBatchTxtUrl(state.batchId, state.downloadToken), "_blank", "noopener,noreferrer")
            }
            leftIcon={<Download className="h-3.5 w-3.5" />}
          >
            下载 TXT
          </Button>
        </div>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-5">
          <div className="space-y-2">
            {state.codes.map((code) => (
              <div
                key={code}
                className="grid grid-cols-[1fr_auto] items-center gap-3 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-2"
              >
                <code className="min-w-0 select-all overflow-x-auto whitespace-nowrap font-mono text-sm scrollbar-thin">
                  {code}
                </code>
                <Button variant="ghost" size="sm" onClick={() => copyText(code, "兑换码已复制")}>
                  复制
                </Button>
              </div>
            ))}
          </div>
        </div>
        <div className="mobile-dialog-footer shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/72 px-5 py-3 text-xs text-[var(--fg-2)]">
          关闭后，5 分钟内可在列表里点“重新查看”再次取回明文；超过窗口后明文不会再被保存。
        </div>
      </div>
    </div>
  );
}

function UserWalletsSubpanel() {
  const qc = useQueryClient();
  const [walletQText, setWalletQText] = useState("");
  const [walletSearch, setWalletSearch] = useState("");
  const [walletMode, setWalletMode] = useState<"wallet" | "byok" | "all">("all");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [nextMode, setNextMode] = useState<"wallet" | "byok">("wallet");
  const [residualMode, setResidualMode] = useState<"freeze" | "zero">("freeze");
  const [txKind, setTxKind] = useState("all");

  useEffect(() => {
    const handle = window.setTimeout(() => {
      setWalletSearch(walletQText.trim());
    }, 250);
    return () => window.clearTimeout(handle);
  }, [walletQText]);

  const walletsQ = useQuery({
    queryKey: ["admin", "wallets", walletSearch, walletMode],
    queryFn: () => listAdminWallets(walletSearch, walletMode, { limit: 100 }),
    retry: false,
  });

  const detailQ = useQuery({
    queryKey: ["admin", "wallet-detail", selectedUserId],
    queryFn: () => getAdminWalletDetail(selectedUserId),
    enabled: Boolean(selectedUserId),
    retry: false,
  });

  const txQ = useQuery({
    queryKey: ["admin", "wallet-transactions", selectedUserId, txKind],
    queryFn: () =>
      listAdminWalletTransactions(selectedUserId, {
        kind: txKind === "all" ? undefined : txKind,
        limit: 20,
      }),
    enabled: Boolean(selectedUserId),
    retry: false,
  });

  const adjustMut = useMutation({
    mutationFn: () => adjustAdminWallet(selectedUserId, adjustAmount, adjustReason),
    onSuccess: async () => {
      toast.success("钱包调账已写入");
      setAdjustAmount("");
      setAdjustReason("");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "wallets"] }),
        qc.invalidateQueries({ queryKey: ["admin", "wallet-detail", selectedUserId] }),
        qc.invalidateQueries({ queryKey: ["admin", "wallet-transactions", selectedUserId] }),
        qc.invalidateQueries({ queryKey: ["admin", "billing", "overview"] }),
      ]);
    },
    onError: (err) => toast.error("调账失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const modeMut = useMutation({
    mutationFn: () => setAdminAccountMode(selectedUserId, nextMode, residualMode),
    onSuccess: async (out) => {
      toast.success(`${out.email} 已切换为 ${out.account_mode}`);
      setNextMode(out.account_mode === "wallet" ? "byok" : "wallet");
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "wallets"] }),
        qc.invalidateQueries({ queryKey: ["admin", "wallet-detail", selectedUserId] }),
      ]);
    },
    onError: (err) => toast.error("账号模式切换失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const selected = detailQ.data;
  const walletItems = walletsQ.data?.items ?? [];
  const transactions = txQ.data?.items ?? selected?.transactions ?? [];

  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex items-center gap-2 type-card-title">
        <Wallet className="h-4 w-4" />
        用户钱包
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setWalletSearch(walletQText.trim());
        }}
        className="grid gap-3 md:grid-cols-[1fr_140px_auto]"
      >
        <input
          value={walletQText}
          onChange={(e) => setWalletQText(e.target.value)}
          placeholder="邮箱 / 用户 ID"
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
        <Button variant="outline" size="md" type="submit" leftIcon={<Search className="h-3.5 w-3.5" />}>
          刷新
        </Button>
      </form>

      <div className="grid gap-2">
        {walletsQ.isLoading && (
          <div className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-4 text-center text-sm text-[var(--fg-2)]">
            加载中
          </div>
        )}
        {walletItems.map((item) => (
          <button
            key={item.user_id}
            type="button"
            onClick={() => {
              setSelectedUserId(item.user_id);
              setNextMode(item.account_mode === "wallet" ? "byok" : "wallet");
            }}
            className={[
              "grid grid-cols-[1fr_auto] gap-2 rounded-[var(--radius-control)] border px-3 py-2 text-left",
              selectedUserId === item.user_id
                ? "border-[var(--accent)] bg-[var(--accent)]/10"
                : "border-[var(--border)] bg-[var(--bg-0)]",
            ].join(" ")}
          >
            <span className="min-w-0 truncate">
              {item.email}
              <span className="ml-2 font-mono text-xs text-[var(--fg-3)]">{item.account_mode}</span>
              {item.last_topup_at && (
                <span className="ml-2 text-xs text-[var(--fg-3)]">
                  最近充值 {new Date(item.last_topup_at).toLocaleDateString()}
                </span>
              )}
            </span>
            <span className="tabular-nums">
              {item.wallet.balance ? `¥${formatMoney(item.wallet.balance.rmb)}` : "BYOK"}
            </span>
          </button>
        ))}
        {!walletsQ.isLoading && walletItems.length === 0 && (
          <div className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-4 text-center text-sm text-[var(--fg-2)]">
            没有匹配用户
          </div>
        )}
      </div>

      {selected && (
        <div className="grid gap-4 border-t border-[var(--border-subtle)] pt-4 xl:grid-cols-[280px_1fr]">
          <div className="space-y-3">
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
              <p className="truncate text-sm font-medium text-[var(--fg-0)]">{selected.email}</p>
              <p className="mt-1 truncate font-mono text-xs text-[var(--fg-3)]">{selected.user_id}</p>
              <div className="mt-4 grid gap-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-[var(--fg-2)]">模式</span>
                  <span>{selected.account_mode}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[var(--fg-2)]">余额</span>
                  <span>¥{formatMoney(selected.wallet.balance?.rmb)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[var(--fg-2)]">预扣</span>
                  <span>¥{formatMoney(selected.wallet.hold?.rmb)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[var(--fg-2)]">最近充值</span>
                  <span className="min-w-0 truncate text-right">{selected.last_topup_at ? new Date(selected.last_topup_at).toLocaleString() : "-"}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-[var(--fg-2)]">最近扣费</span>
                  <span className="min-w-0 truncate text-right">{selected.last_charge_at ? new Date(selected.last_charge_at).toLocaleString() : "-"}</span>
                </div>
              </div>
            </div>
            <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
              <p className="text-sm font-medium">调账</p>
              <input
                value={adjustAmount}
                onChange={(e) => setAdjustAmount(e.target.value)}
                placeholder="+10 / -5"
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
              />
              <input
                value={adjustReason}
                onChange={(e) => setAdjustReason(e.target.value)}
                placeholder="理由"
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
              />
              <Button
                variant="primary"
                size="md"
                fullWidth
                onClick={() => {
                  if (!window.confirm(`确认调账 ${adjustAmount} RMB？`)) return;
                  adjustMut.mutate();
                }}
                loading={adjustMut.isPending}
                disabled={!adjustAmount || !adjustReason}
              >
                写入调账
              </Button>
            </div>
            <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
              <p className="text-sm font-medium">切换账号模式</p>
              <select
                value={nextMode}
                onChange={(e) => setNextMode(e.target.value as "wallet" | "byok")}
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
              >
                <option value="wallet">转 wallet</option>
                <option value="byok">转 byok</option>
              </select>
              <select
                value={residualMode}
                onChange={(e) => setResidualMode(e.target.value as "freeze" | "zero")}
                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
              >
                <option value="freeze">冻结余额</option>
                <option value="zero">清零余额</option>
              </select>
              <Button
                variant="outline"
                size="md"
                fullWidth
                onClick={() => {
                  if (!window.confirm(`确认切换为 ${nextMode}？`)) return;
                  modeMut.mutate();
                }}
                loading={modeMut.isPending}
                leftIcon={<UserCog className="h-3.5 w-3.5" />}
              >
                切换模式
              </Button>
            </div>
          </div>

          <div className="space-y-4">
            <div className="scrollbar-thin flex flex-nowrap gap-2 overflow-x-auto overscroll-x-contain">
              {Object.entries(TX_KIND_LABEL).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTxKind(key)}
                  className={[
                    "shrink-0 rounded-full border px-3 py-1 text-xs",
                    txKind === key
                      ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
                      : "border-[var(--border)] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
              <div className="border-b border-[var(--border-subtle)] px-4 py-3 text-sm font-medium">
                流水
              </div>
              <div className="divide-y divide-[var(--border-subtle)]">
                {transactions.map((tx) => (
                  <div key={tx.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 text-sm">
                    <div className="min-w-0">
                      <p className="truncate">{TX_KIND_LABEL[tx.kind] ?? tx.kind}</p>
                      <p className="truncate font-mono text-xs text-[var(--fg-3)]">
                        {tx.ref_type ?? "-"} {tx.ref_id ?? ""}
                      </p>
                    </div>
                    <div className="text-right tabular-nums">
                      <p>{tx.amount.micro >= 0 ? "+" : ""}¥{formatMoney(tx.amount.rmb)}</p>
                      <p className="text-xs text-[var(--fg-3)]">
                        {new Date(tx.created_at).toLocaleString()}
                      </p>
                    </div>
                  </div>
                ))}
                {!txQ.isLoading && transactions.length === 0 && (
                  <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
                    暂无流水
                  </div>
                )}
              </div>
            </div>
            <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
              <div className="border-b border-[var(--border-subtle)] px-4 py-3 text-sm font-medium">
                最近兑换
              </div>
              <div className="divide-y divide-[var(--border-subtle)]">
                {(selected.redemptions ?? []).map((item) => (
                  <div key={item.id} className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 text-sm">
                    <span className="min-w-0 truncate font-mono text-xs">{item.code_id}</span>
                    <span className="tabular-nums">¥{formatMoney(item.amount.rmb)}</span>
                  </div>
                ))}
                {(selected.redemptions ?? []).length === 0 && (
                  <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
                    暂无兑换记录
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
