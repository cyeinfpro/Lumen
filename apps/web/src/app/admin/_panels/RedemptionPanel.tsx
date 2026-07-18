"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, Slash } from "lucide-react";

import {
  adjustAdminWallet,
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
import { toast } from "@/components/ui/primitives";
import { ActionSheet, type ActionItem } from "@/components/ui/primitives/mobile";
import {
  CodeBatchForm,
  NewCodesModal,
  RedemptionCodesCard,
  RedemptionUsageCard,
  WalletDetailSection,
  WalletList,
  WalletSearchForm,
  WalletsCard,
  type AccountMode,
  type CodeStatus,
  type NewCodesModalState,
  type ResidualMode,
  type WalletMode,
} from "./RedemptionPanelViews";

type Section = "codes" | "wallets" | "all";

export function RedemptionPanel({ section = "all" }: { section?: Section }) {
  return (
    <div className="space-y-5">
      {section !== "wallets" && <CodesSubpanel />}
      {section !== "codes" && <UserWalletsSubpanel />}
    </div>
  );
}

async function copyText(text: string, label = "已复制") {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(label);
  } catch (err) {
    toast.error("复制失败", {
      description: err instanceof Error ? err.message : undefined,
    });
  }
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

  const createCodes = () => {
    if (
      (Number(count) || 1) > 200 &&
      !window.confirm("将生成超过 200 张明文 code，确认继续？")
    ) {
      return;
    }
    createMut.mutate();
  };

  return (
    <>
      <CodeBatchForm
        amount={amount}
        count={count}
        maxRedemptions={maxRedemptions}
        note={note}
        expiresAt={expiresAt}
        totalValue={totalValue}
        isCreating={createMut.isPending}
        onAmountChange={setAmount}
        onCountChange={setCount}
        onMaxRedemptionsChange={setMaxRedemptions}
        onNoteChange={setNote}
        onExpiresAtChange={setExpiresAt}
        onCreate={createCodes}
      />
      <RedemptionCodesCard
        status={status}
        qInput={qInput}
        items={codesQ.data?.items ?? []}
        isLoading={codesQ.isLoading}
        isError={codesQ.isError}
        errorMessage={codesQ.error?.message ?? "兑换码加载失败"}
        nextCursor={codesQ.data?.next_cursor}
        onStatusChange={(nextStatus) => {
          setStatus(nextStatus);
          setCursor(null);
        }}
        onQInputChange={setQInput}
        onSearch={() => {
          setCursor(null);
          setQ(qInput.trim());
        }}
        onRefresh={() => void codesQ.refetch()}
        onRetry={() => void codesQ.refetch()}
        onNextPage={setCursor}
        actions={{
          onCopy: (text, label) => void copyText(text, label),
          onRedownloadBatch: redownloadMut.mutate,
          onRevokeCode: revokeMut.mutate,
          onRevokeBatch: revokeBatchMut.mutate,
          onOpenUsage: setUsageCodeId,
          onOpenMobileActions: setActionsCode,
          redownloadPending: redownloadMut.isPending,
          revokeBatchPending: revokeBatchMut.isPending,
        }}
      />
      <RedemptionUsageCard
        open={Boolean(usageCodeId)}
        items={usageQ.data?.items ?? []}
        isLoading={usageQ.isLoading}
        isError={usageQ.isError}
        errorMessage={usageQ.error?.message ?? "兑换记录加载失败"}
        onRetry={() => void usageQ.refetch()}
        onClose={() => setUsageCodeId("")}
      />

      {modal && (
        <NewCodesModal
          state={modal}
          onClose={() => setModal(null)}
          onCopy={(text, label) => void copyText(text, label)}
        />
      )}
      <ActionSheet
        open={Boolean(actionsCode)}
        onClose={() => setActionsCode(null)}
        title={actionsCode?.code_prefix ?? "兑换码"}
        actions={actionSheetActions}
      />
    </>
  );
}

function UserWalletsSubpanel() {
  const qc = useQueryClient();
  const [walletQText, setWalletQText] = useState("");
  const [walletSearch, setWalletSearch] = useState("");
  const [walletMode, setWalletMode] = useState<WalletMode>("all");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [adjustAmount, setAdjustAmount] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [nextMode, setNextMode] = useState<AccountMode>("wallet");
  const [residualMode, setResidualMode] = useState<ResidualMode>("freeze");
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
    <WalletsCard>
      <WalletSearchForm
        query={walletQText}
        mode={walletMode}
        onQueryChange={setWalletQText}
        onModeChange={setWalletMode}
        onSubmit={() => setWalletSearch(walletQText.trim())}
      />
      <WalletList
        items={walletItems}
        selectedUserId={selectedUserId}
        isLoading={walletsQ.isLoading}
        isError={walletsQ.isError}
        errorMessage={walletsQ.error?.message ?? "用户钱包加载失败"}
        onRetry={() => void walletsQ.refetch()}
        onSelect={(item) => {
          setSelectedUserId(item.user_id);
          setNextMode(item.account_mode === "wallet" ? "byok" : "wallet");
        }}
      />
      <WalletDetailSection
        selected={selected}
        adjustAmount={adjustAmount}
        adjustReason={adjustReason}
        nextMode={nextMode}
        residualMode={residualMode}
        txKind={txKind}
        transactions={transactions}
        transactionsLoading={txQ.isLoading}
        transactionsError={txQ.isError}
        transactionsErrorMessage={txQ.error?.message ?? "流水加载失败"}
        adjustPending={adjustMut.isPending}
        modePending={modeMut.isPending}
        onAdjustAmountChange={setAdjustAmount}
        onAdjustReasonChange={setAdjustReason}
        onAdjust={() => {
          if (!window.confirm(`确认调账 ${adjustAmount} RMB？`)) return;
          adjustMut.mutate();
        }}
        onNextModeChange={setNextMode}
        onResidualModeChange={setResidualMode}
        onChangeMode={() => {
          if (!window.confirm(`确认切换为 ${nextMode}？`)) return;
          modeMut.mutate();
        }}
        onTxKindChange={setTxKind}
        onRetryTransactions={() => void txQ.refetch()}
      />
    </WalletsCard>
  );
}
