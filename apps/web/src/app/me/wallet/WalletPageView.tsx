"use client";

import type { FormEvent } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  Copy,
  CreditCard,
  Gift,
  RefreshCw,
} from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { formatRmb } from "@/lib/money";
import type {
  BillingSnapshotOut,
  BillingWindowOut,
  RedemptionUsageOut,
  WalletOut,
  WalletTransactionOut,
} from "@/lib/types";

import {
  WALLET_TRANSACTION_FILTERS,
  type WalletPageModel,
  type WalletTransactionFilter,
} from "./useWalletPageModel";

type WalletActivityStats = {
  topup: number;
  spend: number;
};

const BILLING_KINDS = [
  { key: "input", label: "输入" },
  { key: "output", label: "输出" },
  { key: "cache_read", label: "缓存读取" },
  { key: "cache_creation", label: "缓存写入" },
  { key: "image", label: "图片" },
  { key: "reasoning", label: "推理" },
] as const;

const BILLING_WINDOWS = ["5h", "1d", "7d"] as const;

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

function microMoney(value?: number | null): string {
  return ((value ?? 0) / 1_000_000).toFixed(2);
}

export function ByokWalletPage() {
  return (
    <SettingsShell title="钱包" subtitle="BYOK" maxWidth="max-w-3xl">
      <Card variant="subtle" padding="lg" className="space-y-3">
        <p className="type-card-title">BYOK 账号</p>
        <p className="type-body">
          你的账号由 BYOK 自助注册流程创建，所以费用直接由你在 OpenAI/Claude
          等上游账单结算，Lumen 不维护钱包余额。
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

export function WalletPageView({
  model,
  activity24h,
}: {
  model: WalletPageModel;
  activity24h: WalletActivityStats;
}) {
  return (
    <SettingsShell title="钱包" subtitle="余额与兑换码" maxWidth="max-w-4xl">
      <div className="space-y-6">
        <WalletHeader />
        <LowBalanceNotice visible={model.lowBalance} />
        <WalletOverview
          wallet={model.wallet}
          activity24h={activity24h}
          lowBalance={model.lowBalance}
          walletState={model.walletState}
          redemptionForm={model.redemptionForm}
        />
        <BillingSnapshotSection state={model.snapshot} />
        <TransactionHistory state={model.transactions} />
        <RedemptionHistory state={model.redemptionHistory} />
      </div>
    </SettingsShell>
  );
}

function WalletHeader() {
  return (
    <header className="hidden items-start justify-between gap-4 md:flex">
      <div>
        <h1 className="type-page-title">钱包</h1>
        <p className="type-body mt-1.5">查看余额、兑换额度和流水。</p>
      </div>
      <Link
        href="/me"
        className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        返回我的
      </Link>
    </header>
  );
}

function LowBalanceNotice({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return (
    <div
      role="alert"
      className="flex items-center gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 text-sm text-[var(--danger-fg)]"
    >
      <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span>
        余额不足，4K 图或多图任务可能无法生成。请先兑换充值或联系管理员。
      </span>
    </div>
  );
}

function WalletOverview({
  wallet,
  activity24h,
  lowBalance,
  walletState,
  redemptionForm,
}: {
  wallet: WalletOut | undefined;
  activity24h: WalletActivityStats;
  lowBalance: boolean;
  walletState: WalletPageModel["walletState"];
  redemptionForm: WalletPageModel["redemptionForm"];
}) {
  return (
    <div className="grid gap-4 md:grid-cols-[1fr_1.2fr]">
      <WalletBalanceCard
        wallet={wallet}
        activity24h={activity24h}
        lowBalance={lowBalance}
        state={walletState}
      />
      <RedemptionForm state={redemptionForm} />
    </div>
  );
}

function walletBalanceText(
  wallet: WalletOut | undefined,
  isLoading: boolean,
  error: string | null,
): string {
  if (error) return "—";
  if (isLoading) return "…";
  return `¥${formatRmb(wallet?.balance?.rmb)}`;
}

function WalletBalanceCard({
  wallet,
  activity24h,
  lowBalance,
  state,
}: {
  wallet: WalletOut | undefined;
  activity24h: WalletActivityStats;
  lowBalance: boolean;
  state: WalletPageModel["walletState"];
}) {
  const balanceClass = lowBalance
    ? "type-page-title-sm font-mono tabular-nums text-[var(--danger-fg)]"
    : "type-page-title-sm font-mono tabular-nums";

  return (
    <Card
      variant="default"
      padding="lg"
      className="min-h-[180px] space-y-4"
      aria-busy={state.isLoading || state.isRefreshing}
    >
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
          <CreditCard className="h-4 w-4" aria-hidden="true" />
        </div>
        <div>
          <p className="type-caption text-[var(--fg-2)]">可用余额</p>
          <p className={balanceClass} aria-live="polite">
            {walletBalanceText(wallet, state.isLoading, state.error)}
          </p>
        </div>
      </div>
      {state.error ? (
        <InlineQueryError
          title="钱包加载失败"
          message={state.error}
          onRetry={state.retry}
          retrying={state.isRefreshing}
        />
      ) : (
        <>
          <p className="type-body-sm text-[var(--fg-2)]">
            预扣 ¥{formatRmb(wallet?.hold?.rmb)}
          </p>
          <p className="type-caption font-mono tabular-nums text-[var(--fg-2)]">
            24h 变化 +¥{activity24h.topup.toFixed(2)} / -¥
            {activity24h.spend.toFixed(2)}
          </p>
        </>
      )}
    </Card>
  );
}

function RedemptionForm({
  state,
}: {
  state: WalletPageModel["redemptionForm"];
}) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    state.submit();
  }

  return (
    <form
      onSubmit={submit}
      className="grid min-h-[180px] grid-rows-[auto_1fr_auto] gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-5"
      aria-busy={state.isPending}
    >
      <label
        htmlFor="wallet-redemption-code"
        className="flex items-center gap-2 type-overline"
      >
        <Gift className="h-3.5 w-3.5" aria-hidden="true" />
        兑换码
      </label>
      <div className="space-y-2">
        <input
          id="wallet-redemption-code"
          name="redemption-code"
          value={state.code}
          onChange={(event) => state.setCode(event.target.value)}
          placeholder="LMN-XXXX-XXXX-XXXX-XXXX"
          inputMode="text"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
          autoComplete="off"
          enterKeyHint="done"
          aria-describedby={
            state.notice ? "wallet-redemption-notice" : undefined
          }
          aria-invalid={state.notice?.kind === "error"}
          className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-base tracking-[0.06em] outline-none focus:border-[var(--accent)]/50 sm:text-lg"
        />
        <RedemptionNotice notice={state.notice} />
      </div>
      <Button
        type="submit"
        variant="primary"
        size="md"
        disabled={!state.canSubmit}
        loading={state.isPending}
        fullWidth
      >
        兑换
      </Button>
    </form>
  );
}

function RedemptionNotice({
  notice,
}: {
  notice: WalletPageModel["redemptionForm"]["notice"];
}) {
  if (!notice) return null;
  const isError = notice.kind === "error";
  return (
    <div
      id="wallet-redemption-notice"
      className={
        isError
          ? "flex items-center gap-2 type-body-sm text-[var(--danger-fg)]"
          : "flex items-center gap-2 type-body-sm text-[var(--fg-1)]"
      }
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
    >
      {isError ? (
        <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
      ) : (
        <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
      )}
      <span>{notice.message}</span>
    </div>
  );
}

function BillingSnapshotSection({
  state,
}: {
  state: WalletPageModel["snapshot"];
}) {
  if (!state.data) {
    return (
      <OptionalQueryState
        loading={state.isLoading}
        error={state.error}
        loadingText="正在加载费用构成…"
        errorTitle="费用构成加载失败"
        onRetry={state.refresh}
      />
    );
  }
  return <BillingSnapshotCard snapshot={state.data} state={state} />;
}

function BillingSnapshotCard({
  snapshot,
  state,
}: {
  snapshot: BillingSnapshotOut;
  state: WalletPageModel["snapshot"];
}) {
  return (
    <Card
      variant="subtle"
      padding="lg"
      className="space-y-4"
      aria-busy={state.isRefreshing}
    >
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
          onClick={state.refresh}
          loading={state.isRefreshing}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          刷新
        </Button>
      </div>
      <QueryErrorRegion
        error={state.error}
        title="刷新失败"
        onRetry={state.refresh}
        retrying={state.isRefreshing}
      />
      <BillingKindGrid snapshot={snapshot} />
      <BillingWindowGrid snapshot={snapshot} />
    </Card>
  );
}

function BillingKindGrid({ snapshot }: { snapshot: BillingSnapshotOut }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {BILLING_KINDS.map(({ key, label }) => (
        <div
          key={key}
          className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3"
        >
          <p className="type-caption text-[var(--fg-2)]">{label}</p>
          <p className="mt-1 text-base font-semibold tabular-nums">
            ¥{microMoney(snapshot.by_kind_30d[key])}
          </p>
        </div>
      ))}
    </div>
  );
}

function BillingWindowGrid({ snapshot }: { snapshot: BillingSnapshotOut }) {
  return (
    <div className="grid gap-3 md:grid-cols-3">
      {BILLING_WINDOWS.map((key) => (
        <BillingWindowCard key={key} label={key} window={snapshot.windows[key]} />
      ))}
    </div>
  );
}

function BillingWindowCard({
  label,
  window,
}: {
  label: string;
  window: BillingWindowOut | undefined;
}) {
  const limit = window?.limit_micro ?? 0;
  const percentage =
    limit > 0
      ? Math.min(100, Math.round(((window?.used_micro ?? 0) / limit) * 100))
      : 0;
  const limitText = limit > 0 ? `¥${microMoney(limit)}` : "不限";

  return (
    <div className="min-h-[112px] rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
      <div className="flex items-center justify-between text-xs text-[var(--fg-2)]">
        <span>{label} 限额</span>
        <span>
          ¥{microMoney(window?.used_micro)} / {limitText}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${label} 限额使用率`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percentage}
        className="mt-2 h-1.5 rounded-full bg-[var(--bg-2)]"
      >
        <div
          className="h-full rounded-full bg-[var(--accent)]"
          style={{ width: `${percentage}%` }}
        />
      </div>
      {window?.resets_at ? (
        <p className="mt-2 type-caption text-[var(--fg-2)]">
          重置 {new Date(window.resets_at).toLocaleString()}
        </p>
      ) : null}
    </div>
  );
}

function TransactionHistory({
  state,
}: {
  state: WalletPageModel["transactions"];
}) {
  return (
    <Card
      variant="subtle"
      padding="none"
      className="overflow-hidden"
      aria-busy={state.isLoading || state.isRefreshing}
    >
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="type-card-title">流水</p>
        <Button
          variant="ghost"
          size="sm"
          onClick={state.refresh}
          loading={state.isRefreshing}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          刷新
        </Button>
      </div>
      <TransactionFilters
        value={state.filter}
        onChange={state.setFilter}
      />
      <TransactionList state={state} />
      <LoadMore
        visible={state.hasNextPage}
        loading={state.isFetchingNextPage}
        onLoad={state.loadMore}
      />
    </Card>
  );
}

function TransactionFilters({
  value,
  onChange,
}: {
  value: WalletTransactionFilter;
  onChange: (value: WalletTransactionFilter) => void;
}) {
  return (
    <div
      role="group"
      aria-label="流水类型"
      className="scrollbar-thin flex gap-2 overflow-x-auto overscroll-x-contain border-b border-[var(--border-subtle)] px-4 py-3 [scrollbar-width:none]"
    >
      {WALLET_TRANSACTION_FILTERS.map((item) => (
        <button
          key={item.key}
          type="button"
          aria-pressed={value === item.key}
          onClick={() => onChange(item.key)}
          className={[
            "min-h-11 shrink-0 rounded-full border px-3 text-xs md:min-h-9",
            value === item.key
              ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
              : "border-[var(--border)] text-[var(--fg-2)]",
          ].join(" ")}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

function TransactionList({
  state,
}: {
  state: WalletPageModel["transactions"];
}) {
  const empty = state.items.length === 0;
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      <QueryErrorRegion
        error={state.error}
        title="流水加载失败"
        onRetry={state.refresh}
        retrying={state.isRefreshing}
        className="m-4"
      />
      {state.items.map((transaction) => (
        <TransactionRow key={transaction.id} transaction={transaction} />
      ))}
      <CollectionState
        error={state.error}
        loading={state.isLoading}
        empty={empty}
        loadingMessage="正在加载流水…"
        emptyMessage="暂无流水"
      />
    </div>
  );
}

function TransactionRow({
  transaction,
}: {
  transaction: WalletTransactionOut;
}) {
  const positive = transaction.amount.micro >= 0;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3">
      <div className="min-w-0">
        <p className="truncate type-body-sm text-[var(--fg-0)]">
          {formatKind(transaction.kind)}
        </p>
        <p className="type-caption text-[var(--fg-2)]">
          {new Date(transaction.created_at).toLocaleString()}
        </p>
      </div>
      <div className="max-w-[46vw] break-words text-right tabular-nums md:max-w-none">
        <p className={positive ? "text-success" : "text-[var(--fg-0)]"}>
          {positive ? "+" : ""}¥{formatRmb(transaction.amount.rmb)}
        </p>
        <p className="type-caption text-[var(--fg-2)]">
          余额 ¥{formatRmb(transaction.balance_after.rmb)}
        </p>
      </div>
    </div>
  );
}

function RedemptionHistory({
  state,
}: {
  state: WalletPageModel["redemptionHistory"];
}) {
  return (
    <Card
      variant="subtle"
      padding="none"
      className="overflow-hidden"
      aria-busy={state.isLoading || state.isFetchingNextPage}
    >
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="type-card-title">我的兑换历史</p>
        <Button
          variant="ghost"
          size="sm"
          onClick={state.copy}
          leftIcon={<Copy className="h-3.5 w-3.5" />}
        >
          复制记录
        </Button>
      </div>
      <RedemptionList state={state} />
      <LoadMore
        visible={state.hasNextPage}
        loading={state.isFetchingNextPage}
        onLoad={state.loadMore}
      />
    </Card>
  );
}

function RedemptionList({
  state,
}: {
  state: WalletPageModel["redemptionHistory"];
}) {
  const empty = state.items.length === 0;
  return (
    <div className="divide-y divide-[var(--border-subtle)]">
      <QueryErrorRegion
        error={state.error}
        title="兑换记录加载失败"
        onRetry={state.retry}
        retrying={state.isLoading}
        className="m-4"
      />
      {state.items.map((item) => (
        <RedemptionRow key={item.id} item={item} />
      ))}
      <CollectionState
        error={state.error}
        loading={state.isLoading}
        empty={empty}
        loadingMessage="正在加载兑换记录…"
        emptyMessage="暂无兑换记录"
      />
    </div>
  );
}

function RedemptionRow({ item }: { item: RedemptionUsageOut }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3">
      <div className="min-w-0">
        <p className="type-body-sm text-[var(--fg-0)]">兑换码充值</p>
        <p className="type-caption text-[var(--fg-2)]">
          {new Date(item.redeemed_at).toLocaleString()}
        </p>
      </div>
      <div className="text-right tabular-nums">
        <p className="text-success">+¥{formatRmb(item.amount.rmb)}</p>
        <p className="max-w-[44vw] truncate type-caption text-[var(--fg-2)] md:max-w-none">
          {item.code_id}
        </p>
      </div>
    </div>
  );
}

function OptionalQueryState({
  loading,
  error,
  loadingText,
  errorTitle,
  onRetry,
}: {
  loading: boolean;
  error: string | null;
  loadingText: string;
  errorTitle: string;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <InlineQueryError
        title={errorTitle}
        message={error}
        onRetry={onRetry}
        retrying={loading}
      />
    );
  }
  if (!loading) return null;
  return (
    <div
      role="status"
      className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 px-4 py-8 text-center type-body-sm text-[var(--fg-2)]"
    >
      {loadingText}
    </div>
  );
}

function InlineQueryError({
  title,
  message,
  onRetry,
  retrying,
  className = "",
}: {
  title: string;
  message: string;
  onRetry: () => void;
  retrying: boolean;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={[
        "flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-3",
        className,
      ].join(" ")}
    >
      <div className="min-w-0">
        <p className="type-body-sm text-[var(--danger-fg)]">{title}</p>
        <p className="mt-1 break-words type-caption text-[var(--fg-2)]">
          {message}
        </p>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onRetry}
        loading={retrying}
        leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
      >
        重试
      </Button>
    </div>
  );
}

function QueryErrorRegion({
  error,
  title,
  onRetry,
  retrying,
  className,
}: {
  error: string | null;
  title: string;
  onRetry: () => void;
  retrying: boolean;
  className?: string;
}) {
  if (!error) return null;
  return (
    <InlineQueryError
      title={title}
      message={error}
      onRetry={onRetry}
      retrying={retrying}
      className={className}
    />
  );
}

function CollectionState({
  error,
  loading,
  empty,
  loadingMessage,
  emptyMessage,
}: {
  error: string | null;
  loading: boolean;
  empty: boolean;
  loadingMessage: string;
  emptyMessage: string;
}) {
  if (error || !empty) return null;
  return (
    <CollectionStatus message={loading ? loadingMessage : emptyMessage} />
  );
}

function CollectionStatus({ message }: { message: string }) {
  return (
    <div
      role="status"
      className="px-4 py-8 text-center type-body-sm text-[var(--fg-2)]"
    >
      {message}
    </div>
  );
}

function LoadMore({
  visible,
  loading,
  onLoad,
}: {
  visible: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  if (!visible) return null;
  return (
    <div className="border-t border-[var(--border-subtle)] px-4 py-3 text-center">
      <Button
        variant="outline"
        size="sm"
        loading={loading}
        onClick={onLoad}
      >
        加载更多
      </Button>
    </div>
  );
}
