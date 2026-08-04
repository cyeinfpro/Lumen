"use client";

import type { FormEvent, ReactNode } from "react";
import { Search, UserCog, Wallet } from "lucide-react";

import {
  Button,
  Card,
  Input,
  Select,
  StatusBadge,
} from "@/components/ui/primitives";
import { formatRmb } from "@/lib/money";
import type {
  AdminRedemptionUsageOut,
  AdminWalletDetailOut,
  AdminWalletOut,
  WalletTransactionOut,
} from "@/lib/types";
import { ErrorBlock } from "../_components/AdminFeedback";

type AccountMode = "wallet" | "byok";
type ResidualMode = "freeze" | "zero";
type WalletMode = "wallet" | "byok" | "all";

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

function formatMoney(value?: string | null): string {
  return formatRmb(value);
}

export function WalletSearchForm({
  query,
  mode,
  onQueryChange,
  onModeChange,
  onSubmit,
}: {
  query: string;
  mode: WalletMode;
  onQueryChange: (value: string) => void;
  onModeChange: (mode: WalletMode) => void;
  onSubmit: () => void;
}) {
  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit();
  };

  return (
    <form
      onSubmit={submitSearch}
      className="grid gap-3 md:grid-cols-[1fr_140px_auto]"
    >
      <Input
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        placeholder="邮箱 / 用户 ID"
      />
      <Select
        value={mode}
        onChange={(event) => onModeChange(event.target.value as WalletMode)}
      >
        <option value="wallet">钱包</option>
        <option value="byok">自带密钥</option>
        <option value="all">全部</option>
      </Select>
      <Button
        variant="outline"
        size="md"
        type="submit"
        leftIcon={<Search className="h-3.5 w-3.5" />}
      >
        刷新
      </Button>
    </form>
  );
}

function WalletListItem({
  item,
  selected,
  onSelect,
}: {
  item: AdminWalletOut;
  selected: boolean;
  onSelect: (item: AdminWalletOut) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(item)}
      className={[
        "grid grid-cols-[1fr_auto] gap-2 rounded-[var(--radius-control)] border px-3 py-2 text-left",
        selected
          ? "border-[var(--accent)] bg-[var(--accent)]/10"
          : "border-[var(--border)] bg-[var(--bg-0)]",
      ].join(" ")}
    >
      <span className="min-w-0 truncate">
        {item.email}
        <StatusBadge
          status={item.account_mode}
          className="ml-2 align-middle"
        />
        {item.last_topup_at && (
          <span className="ml-2 text-xs text-[var(--fg-3)]">
            最近充值 {new Date(item.last_topup_at).toLocaleDateString()}
          </span>
        )}
      </span>
      <span className="tabular-nums">
        {item.wallet.balance
          ? `¥${formatMoney(item.wallet.balance.rmb)}`
          : "BYOK"}
      </span>
    </button>
  );
}

export function WalletList({
  items,
  selectedUserId,
  isLoading,
  isError,
  errorMessage,
  onRetry,
  onSelect,
}: {
  items: AdminWalletOut[];
  selectedUserId: string;
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
  onSelect: (item: AdminWalletOut) => void;
}) {
  if (isLoading) {
    return (
      <div className="grid gap-2">
        <div className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-4 text-center text-sm text-[var(--fg-2)]">
          加载中
        </div>
      </div>
    );
  }
  if (isError) {
    return (
      <div className="grid gap-2">
        <div role="alert">
          <ErrorBlock message={errorMessage} onRetry={onRetry} />
        </div>
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="grid gap-2">
        <div className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-4 text-center text-sm text-[var(--fg-2)]">
          没有匹配用户
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-2">
      {items.map((item) => (
        <WalletListItem
          key={item.user_id}
          item={item}
          selected={selectedUserId === item.user_id}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

function WalletSummaryCard({ selected }: { selected: AdminWalletDetailOut }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
      <p className="truncate text-sm font-medium text-[var(--fg-0)]">
        {selected.email}
      </p>
      <p className="mt-1 truncate font-mono text-xs text-[var(--fg-3)]">
        {selected.user_id}
      </p>
      <div className="mt-4 grid gap-2 text-sm">
        <div className="flex justify-between">
          <span className="text-[var(--fg-2)]">模式</span>
          <StatusBadge status={selected.account_mode} />
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
          <span className="min-w-0 truncate text-right">
            {selected.last_topup_at
              ? new Date(selected.last_topup_at).toLocaleString()
              : "-"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--fg-2)]">最近扣费</span>
          <span className="min-w-0 truncate text-right">
            {selected.last_charge_at
              ? new Date(selected.last_charge_at).toLocaleString()
              : "-"}
          </span>
        </div>
      </div>
    </div>
  );
}

function WalletAdjustmentCard({
  amount,
  reason,
  isPending,
  onAmountChange,
  onReasonChange,
  onSubmit,
}: {
  amount: string;
  reason: string;
  isPending: boolean;
  onAmountChange: (value: string) => void;
  onReasonChange: (value: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
      <p className="text-sm font-medium">调账</p>
      <Input
        value={amount}
        onChange={(event) => onAmountChange(event.target.value)}
        placeholder="+10 / -5"
      />
      <Input
        value={reason}
        onChange={(event) => onReasonChange(event.target.value)}
        placeholder="理由"
      />
      <Button
        variant="primary"
        size="md"
        fullWidth
        onClick={onSubmit}
        loading={isPending}
        disabled={!amount || !reason}
      >
        写入调账
      </Button>
    </div>
  );
}

function WalletModeCard({
  nextMode,
  residualMode,
  isPending,
  onNextModeChange,
  onResidualModeChange,
  onSubmit,
}: {
  nextMode: AccountMode;
  residualMode: ResidualMode;
  isPending: boolean;
  onNextModeChange: (mode: AccountMode) => void;
  onResidualModeChange: (mode: ResidualMode) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
      <p className="text-sm font-medium">切换账号模式</p>
      <Select
        value={nextMode}
        onChange={(event) =>
          onNextModeChange(event.target.value as AccountMode)
        }
      >
        <option value="wallet">转钱包模式</option>
        <option value="byok">转自带密钥模式</option>
      </Select>
      <Select
        value={residualMode}
        onChange={(event) =>
          onResidualModeChange(event.target.value as ResidualMode)
        }
      >
        <option value="freeze">冻结余额</option>
        <option value="zero">清零余额</option>
      </Select>
      <Button
        variant="outline"
        size="md"
        fullWidth
        onClick={onSubmit}
        loading={isPending}
        leftIcon={<UserCog className="h-3.5 w-3.5" />}
      >
        切换模式
      </Button>
    </div>
  );
}

function TransactionKindFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="scrollbar-thin flex flex-nowrap gap-2 overflow-x-auto overscroll-x-contain">
      {Object.entries(TX_KIND_LABEL).map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={[
            "shrink-0 rounded-full border px-3 py-1 text-xs",
            value === key
              ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
              : "border-[var(--border)] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
          ].join(" ")}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function WalletTransactionsContent({
  transactions,
  isLoading,
  isError,
  errorMessage,
  onRetry,
}: {
  transactions: WalletTransactionOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
}) {
  if (isLoading) {
    return (
      <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
        加载中
      </div>
    );
  }
  if (isError) {
    return (
      <div role="alert" className="p-4">
        <ErrorBlock message={errorMessage} onRetry={onRetry} />
      </div>
    );
  }
  if (transactions.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
        暂无流水
      </div>
    );
  }

  return transactions.map((tx) => (
    <div
      key={tx.id}
      className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 text-sm"
    >
      <div className="min-w-0">
        <p className="truncate">{TX_KIND_LABEL[tx.kind] ?? tx.kind}</p>
        <p className="truncate font-mono text-xs text-[var(--fg-3)]">
          {tx.ref_type ?? "-"} {tx.ref_id ?? ""}
        </p>
      </div>
      <div className="text-right tabular-nums">
        <p>
          {tx.amount.micro >= 0 ? "+" : ""}¥{formatMoney(tx.amount.rmb)}
        </p>
        <p className="text-xs text-[var(--fg-3)]">
          {new Date(tx.created_at).toLocaleString()}
        </p>
      </div>
    </div>
  ));
}

function WalletTransactionsCard({
  transactions,
  isLoading,
  isError,
  errorMessage,
  onRetry,
}: {
  transactions: WalletTransactionOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
      <div className="border-b border-[var(--border-subtle)] px-4 py-3 text-sm font-medium">
        流水
      </div>
      <div className="divide-y divide-[var(--border-subtle)]">
        <WalletTransactionsContent
          transactions={transactions}
          isLoading={isLoading}
          isError={isError}
          errorMessage={errorMessage}
          onRetry={onRetry}
        />
      </div>
    </div>
  );
}

function WalletRedemptionsCard({
  redemptions,
}: {
  redemptions: AdminRedemptionUsageOut[];
}) {
  return (
    <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)]">
      <div className="border-b border-[var(--border-subtle)] px-4 py-3 text-sm font-medium">
        最近兑换
      </div>
      <div className="divide-y divide-[var(--border-subtle)]">
        {redemptions.map((item) => (
          <div
            key={item.id}
            className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 text-sm"
          >
            <span className="min-w-0 truncate font-mono text-xs">
              {item.code_id}
            </span>
            <span className="tabular-nums">
              ¥{formatMoney(item.amount.rmb)}
            </span>
          </div>
        ))}
        {redemptions.length === 0 && (
          <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
            暂无兑换记录
          </div>
        )}
      </div>
    </div>
  );
}

export function WalletDetailSection({
  selected,
  adjustAmount,
  adjustReason,
  nextMode,
  residualMode,
  txKind,
  transactions,
  transactionsLoading,
  transactionsError,
  transactionsErrorMessage,
  adjustPending,
  modePending,
  onAdjustAmountChange,
  onAdjustReasonChange,
  onAdjust,
  onNextModeChange,
  onResidualModeChange,
  onChangeMode,
  onTxKindChange,
  onRetryTransactions,
}: {
  selected?: AdminWalletDetailOut;
  adjustAmount: string;
  adjustReason: string;
  nextMode: AccountMode;
  residualMode: ResidualMode;
  txKind: string;
  transactions: WalletTransactionOut[];
  transactionsLoading: boolean;
  transactionsError: boolean;
  transactionsErrorMessage: string;
  adjustPending: boolean;
  modePending: boolean;
  onAdjustAmountChange: (value: string) => void;
  onAdjustReasonChange: (value: string) => void;
  onAdjust: () => void;
  onNextModeChange: (mode: AccountMode) => void;
  onResidualModeChange: (mode: ResidualMode) => void;
  onChangeMode: () => void;
  onTxKindChange: (value: string) => void;
  onRetryTransactions: () => void;
}) {
  if (!selected) return null;

  return (
    <div className="grid gap-4 border-t border-[var(--border-subtle)] pt-4 xl:grid-cols-[280px_1fr]">
      <div className="space-y-3">
        <WalletSummaryCard selected={selected} />
        <WalletAdjustmentCard
          amount={adjustAmount}
          reason={adjustReason}
          isPending={adjustPending}
          onAmountChange={onAdjustAmountChange}
          onReasonChange={onAdjustReasonChange}
          onSubmit={onAdjust}
        />
        <WalletModeCard
          nextMode={nextMode}
          residualMode={residualMode}
          isPending={modePending}
          onNextModeChange={onNextModeChange}
          onResidualModeChange={onResidualModeChange}
          onSubmit={onChangeMode}
        />
      </div>
      <div className="space-y-4">
        <TransactionKindFilter value={txKind} onChange={onTxKindChange} />
        <WalletTransactionsCard
          transactions={transactions}
          isLoading={transactionsLoading}
          isError={transactionsError}
          errorMessage={transactionsErrorMessage}
          onRetry={onRetryTransactions}
        />
        <WalletRedemptionsCard redemptions={selected.redemptions ?? []} />
      </div>
    </div>
  );
}

export function WalletsCard({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex items-center gap-2 type-card-title">
        <Wallet className="h-4 w-4" />
        用户钱包
      </div>
      {children}
    </Card>
  );
}
