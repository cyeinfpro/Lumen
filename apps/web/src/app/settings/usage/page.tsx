"use client";

// Lumen V1 使用统计（无配额限制；支持选择展示窗口）。

import { useState } from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { format } from "date-fns";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  CalendarDays,
  CreditCard,
  Database,
  Image as ImageIcon,
  Info,
  KeyRound,
  MessageSquare,
  ReceiptText,
  RefreshCw,
  Sparkles,
  Zap,
} from "lucide-react";

import {
  apiFetch,
  getMe,
  getMyBillingSnapshot,
  listMyWalletTransactions,
} from "@/lib/apiClient";
import type { BillingSnapshotOut, UsageOut, WalletTransactionOut } from "@/lib/types";
import {
  AUTH_USER_QUERY_KEY,
  isUserScopedQueryKeyForUser,
  userBillingQueryKeys,
  userScopedQueryKey,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { DURATION, EASE } from "@/lib/motion";

const PRIMARY_SKELETON_KEYS = [
  "messages",
  "generations",
  "completions",
  "pixels",
] as const;
const SECONDARY_SKELETON_KEYS = ["tokens", "storage"] as const;
const USAGE_PERIODS = [
  { label: "7 天", value: 7 },
  { label: "30 天", value: 30 },
  { label: "90 天", value: 90 },
  { label: "365 天", value: 365 },
] as const;

export default function UsagePage() {
  const [days, setDays] = useState(30);
  const { meQ, q, billingQ, txQ } = useUsagePageQueries(days);
  const selectedPeriod =
    USAGE_PERIODS.find((period) => period.value === days) ?? USAGE_PERIODS[1];

  return (
    <SettingsShell title="用量统计" subtitle={`USAGE · ${selectedPeriod.label}`}>
      <motion.div
        initial={false}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
      >
        <header className="mb-5 flex items-start justify-between gap-4 flex-wrap md:mb-8">
          <div className="hidden min-w-0 md:block">
            <h1 className="type-page-title">
              用量统计
            </h1>
            <p className="type-body mt-1.5">
              过去 {selectedPeriod.label} 的使用记录
            </p>
            {q.data && (
              <p className="type-caption text-[var(--fg-2)] mt-1 font-mono tabular-nums">
                {formatDay(q.data.range_start)} —{" "}
                {formatDay(q.data.range_end)}
              </p>
            )}
          </div>
          <div className="min-w-0 w-full md:w-auto">
            <UsageRangePicker value={days} onChange={setDays} pending={q.isFetching} />
            <Link
              href="/me"
              className="hidden min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:inline-flex"
            >
              <ArrowLeft className="w-4 h-4" />
              返回我的
            </Link>
          </div>
        </header>

        {q.isPending ? (
          <SkeletonGrid />
        ) : q.isError ? (
          <ErrorBox
            message={q.error?.message ?? "加载失败"}
            onRetry={() => void q.refetch()}
          />
        ) : q.data ? (
          <div className="space-y-6">
            <UsageView data={q.data} />
            <BillingTransparency
              accountMode={meQ.data?.account_mode ?? "wallet"}
              snapshot={billingQ.data ?? null}
              recentTransactions={txQ.data?.items ?? []}
              loading={billingQ.isLoading || txQ.isLoading}
            />
          </div>
        ) : null}
      </motion.div>
    </SettingsShell>
  );
}

function useUsagePageQueries(days: number) {
  const userScope = useUserQueryScope();
  const meQ = useQuery({
    queryKey: AUTH_USER_QUERY_KEY,
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });
  const identityReady =
    userScope.enabled && meQ.data?.id === userScope.userId;
  const walletAccount =
    identityReady && meQ.data?.account_mode === "wallet";
  const q = useQuery<UsageOut>({
    queryKey: userScopedQueryKey(userScope.userId, ["me", "usage", { days }]),
    queryFn: () => getUsage(days),
    placeholderData: (previous, previousQuery) =>
      isUserScopedQueryKeyForUser(
        previousQuery?.queryKey ?? [],
        userScope.userId,
      )
        ? previous
        : undefined,
    staleTime: 30_000,
    enabled: identityReady,
  });
  const billingQ = useQuery<BillingSnapshotOut>({
    queryKey: userBillingQueryKeys.snapshot(userScope.userId),
    queryFn: getMyBillingSnapshot,
    retry: false,
    staleTime: 30_000,
    enabled: walletAccount,
  });
  const txQ = useQuery({
    queryKey: userBillingQueryKeys.walletTransactions(userScope.userId, {
      kind: "charge",
      limit: 3,
      pagination: "list",
    }),
    queryFn: () => listMyWalletTransactions({ limit: 3, kind: "charge" }),
    enabled: walletAccount,
    retry: false,
    staleTime: 30_000,
  });

  return { meQ, q, billingQ, txQ };
}

function getUsage(days: number): Promise<UsageOut> {
  const qs = new URLSearchParams({ days: String(days) });
  return apiFetch<UsageOut>(`/me/usage?${qs.toString()}`);
}

function UsageRangePicker({
  value,
  onChange,
  pending,
}: {
  value: number;
  onChange: (days: number) => void;
  pending: boolean;
}) {
  return (
    <div
      className="grid w-full grid-cols-4 gap-1 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-1 sm:inline-flex sm:w-auto sm:items-center"
      aria-label="用量时间范围"
    >
      <CalendarDays className="hidden h-3.5 w-3.5 text-[var(--fg-2)] sm:ml-2 sm:block" />
      {USAGE_PERIODS.map((period) => {
        const active = value === period.value;
        // segmented control 内部按钮：保留原生 button，避免 Button 物理动效干扰相邻态
        return (
          <button
            key={period.value}
            type="button"
            onClick={() => onChange(period.value)}
            disabled={pending && active}
            className={
              "min-h-11 min-w-0 rounded-[var(--radius-control)] px-1.5 type-caption transition-colors sm:px-2.5 " +
              (active
                ? "bg-accent text-black"
                : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]")
            }
          >
            {period.label}
          </button>
        );
      })}
    </div>
  );
}

function UsageView({ data }: { data: UsageOut }) {
  const genRatio =
    data.generations_count > 0
      ? Math.round(
          (data.generations_succeeded / data.generations_count) * 100,
        )
      : null;
  const compRatio =
    data.completions_count > 0
      ? Math.round(
          (data.completions_succeeded / data.completions_count) * 100,
        )
      : null;

  const pixelsM = data.total_pixels_generated / 1_000_000;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="消息数"
          value={formatThousands(data.messages_count)}
          icon={<MessageSquare className="w-4 h-4" />}
          delay={0}
        />
        <StatCard
          label="生成图像"
          value={formatThousands(data.generations_count)}
          icon={<ImageIcon className="w-4 h-4" />}
          sublabel={
            genRatio != null
              ? `成功率 ${genRatio}% · ${formatThousands(
                  data.generations_succeeded,
                )} 张`
              : undefined
          }
          ratio={genRatio}
          delay={0.04}
        />
        <StatCard
          label="对话任务"
          value={formatThousands(data.completions_count)}
          icon={<Sparkles className="w-4 h-4" />}
          sublabel={
            compRatio != null
              ? `成功率 ${compRatio}% · ${formatThousands(
                  data.completions_succeeded,
                )}`
              : undefined
          }
          ratio={compRatio}
          delay={0.08}
        />
        <StatCard
          label="已生成像素"
          value={`${pixelsM.toFixed(1)}M`}
          icon={<Zap className="w-4 h-4" />}
          sublabel="px"
          delay={0.12}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SecondaryCard
          label="Tokens 消耗"
          icon={<MessageSquare className="w-4 h-4" />}
          delay={0.16}
        >
          <div className="flex flex-col md:flex-row md:items-baseline gap-4 md:gap-8">
            <div>
              <div className="type-metric text-[24px] md:text-[28px]">
                {formatThousands(data.total_tokens_in)}
              </div>
              <div className="type-caption text-[var(--fg-2)] mt-0.5">输入</div>
            </div>
            <div className="h-px w-full bg-[var(--border-subtle)] md:h-8 md:w-px" />
            <div>
              <div className="type-metric text-[24px] md:text-[28px]">
                {formatThousands(data.total_tokens_out)}
              </div>
              <div className="type-caption text-[var(--fg-2)] mt-0.5">输出</div>
            </div>
          </div>
        </SecondaryCard>

        <SecondaryCard
          label="存储占用"
          icon={<Database className="w-4 h-4" />}
          delay={0.2}
        >
          <div className="type-metric text-[24px] md:text-[28px]">
            {formatBytes(data.storage_bytes)}
          </div>
          <div className="type-caption text-[var(--fg-2)] mt-0.5 font-mono tabular-nums">
            {formatThousands(data.storage_bytes)} bytes
          </div>
        </SecondaryCard>
      </div>
    </div>
  );
}

function BillingTransparency({
  accountMode,
  snapshot,
  recentTransactions,
  loading,
}: {
  accountMode: "wallet" | "byok";
  snapshot: BillingSnapshotOut | null;
  recentTransactions: WalletTransactionOut[];
  loading: boolean;
}) {
  const imageCost = snapshot ? microToRmbText(snapshot.by_kind_30d.image) : "—";
  const outputCost = snapshot ? microToRmbText(snapshot.by_kind_30d.output) : "—";
  return (
    <section className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 type-overline text-[var(--fg-1)]">
            <Info className="w-3.5 h-3.5" />
            计费口径
          </div>
          <p className="type-body-sm mt-2 text-[var(--fg-2)]">
            提交前展示的是按当前模型、尺寸、数量和价格表计算的预估；任务完成后以实际
            token、实际图片档位和可计费成功结果结算。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {accountMode === "wallet" ? (
            <LinkButton href="/me/wallet" icon={<CreditCard className="w-3.5 h-3.5" />}>
              查看钱包流水
            </LinkButton>
          ) : (
            <LinkButton href="/settings/api-key" icon={<KeyRound className="w-3.5 h-3.5" />}>
              查看 Key 健康
            </LinkButton>
          )}
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <BillingStep
          label="预计费用"
          value="发送前"
          description="用于确认预算，价格表或参数变化会让下一次预估变化。"
        />
        <BillingStep
          label="实际扣费"
          value={accountMode === "wallet" ? "完成后结算" : "由上游 Key 结算"}
          description="失败、取消、未产生结果的部分会释放预扣或不计入平台扣费。"
        />
        <BillingStep
          label="结果详情"
          value="看流水 ref_id"
          description="钱包流水里的 ref_type/ref_id 可定位到 generation 或 completion。"
        />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
          <div className="flex items-center gap-2 text-[var(--fg-2)]">
            <ReceiptText className="w-4 h-4" />
            <span className="type-caption">近 30 天费用构成</span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <MiniMoney label="图片实际" value={imageCost} />
            <MiniMoney label="对话输出" value={outputCost} />
          </div>
        </div>
        <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-[var(--fg-2)]">
              <CreditCard className="w-4 h-4" />
              <span className="type-caption">最近实际结算</span>
            </div>
            {loading && <RefreshCw className="w-3.5 h-3.5 animate-spin text-[var(--fg-2)]" />}
          </div>
          {accountMode === "byok" ? (
            <p className="mt-3 type-body-sm text-[var(--fg-2)]">
              BYOK 账号不走平台钱包扣费；实际费用以你的上游供应商账单为准。
            </p>
          ) : recentTransactions.length > 0 ? (
            <div className="mt-3 divide-y divide-[var(--border-subtle)]">
              {recentTransactions.map((tx) => (
                <div key={tx.id} className="py-2 first:pt-0 last:pb-0">
                  <div className="flex items-center justify-between gap-3 text-sm">
                    <span className="truncate text-[var(--fg-0)]">{txLabel(tx)}</span>
                    <span className="shrink-0 font-mono tabular-nums text-[var(--fg-0)]">
                      {txAmount(tx)}
                    </span>
                  </div>
                  <p className="mt-0.5 truncate type-caption text-[var(--fg-2)]">
                    {tx.ref_type ?? "tx"}:{tx.ref_id ?? tx.id}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-3 type-body-sm text-[var(--fg-2)]">
              暂无近期扣费流水。
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function LinkButton({
  href,
  icon,
  children,
}: {
  href: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className="inline-flex min-h-11 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] px-3 type-caption text-[var(--fg-0)] hover:bg-[var(--bg-2)] sm:min-h-8"
    >
      {icon}
      {children}
    </Link>
  );
}

function BillingStep({
  label,
  value,
  description,
}: {
  label: string;
  value: string;
  description: string;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-3">
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 type-body-sm text-[var(--fg-0)]">{value}</p>
      <p className="mt-1 text-[11px] leading-relaxed text-[var(--fg-2)]">{description}</p>
    </div>
  );
}

function MiniMoney({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 font-mono text-base tabular-nums text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

function StatCard({
  label,
  value,
  icon,
  sublabel,
  ratio,
  delay = 0,
}: {
  label: string;
  value: string;
  icon?: React.ReactNode;
  sublabel?: string;
  ratio?: number | null;
  delay?: number;
}) {
  const reduceMotion = useReducedMotion();
  const progress = Math.max(0, Math.min(100, ratio ?? 0)) / 100;

  return (
    <motion.div
      initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={{
        duration: reduceMotion ? 0 : DURATION.normal,
        delay: reduceMotion ? 0 : delay,
        ease: EASE.develop,
      }}
      className="group rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 transition-[transform,border-color,background-color,box-shadow] duration-[var(--dur-quick)] [@media(hover:hover)]:hover:-translate-y-0.5 [@media(hover:hover)]:hover:border-[var(--border)] [@media(hover:hover)]:hover:bg-[var(--bg-1)]/80 [@media(hover:hover)]:hover:shadow-[var(--shadow-2)] motion-reduce:transform-none"
    >
      <div className="flex items-center justify-between">
        <span className="type-overline">
          {label}
        </span>
        {icon && (
          <span className="w-7 h-7 rounded-[var(--radius-control)] bg-accent-soft border border-accent-border text-accent flex items-center justify-center group-hover:bg-accent/20 transition-colors">
            {icon}
          </span>
        )}
      </div>
      <div className="type-metric mt-3 overflow-hidden text-ellipsis whitespace-nowrap text-[24px] md:text-[28px]">
        {value}
      </div>
      {sublabel && (
        <div className="type-caption text-[var(--fg-2)] mt-1">{sublabel}</div>
      )}
      {ratio != null && (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-[var(--bg-2)]">
          <motion.div
            initial={reduceMotion ? false : { scaleX: 0 }}
            animate={{ scaleX: progress }}
            transition={{
              duration: reduceMotion ? 0 : DURATION.sheet,
              delay: reduceMotion ? 0 : delay + DURATION.quick,
              ease: EASE.develop,
            }}
            style={{ transformOrigin: "left center" }}
            className="h-full rounded-full bg-gradient-to-r from-[var(--accent)]/80 to-[var(--accent)]"
          />
        </div>
      )}
    </motion.div>
  );
}

function SecondaryCard({
  label,
  icon,
  children,
  delay = 0,
}: {
  label: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
  delay?: number;
}) {
  const reduceMotion = useReducedMotion();

  return (
    <motion.div
      initial={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={{
        duration: reduceMotion ? 0 : DURATION.normal,
        delay: reduceMotion ? 0 : delay,
        ease: EASE.develop,
      }}
      className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 transition-[transform,border-color,background-color,box-shadow] duration-[var(--dur-quick)] [@media(hover:hover)]:hover:-translate-y-0.5 [@media(hover:hover)]:hover:border-[var(--border)] [@media(hover:hover)]:hover:bg-[var(--bg-1)]/80 [@media(hover:hover)]:hover:shadow-[var(--shadow-2)] motion-reduce:transform-none"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="type-overline">
          {label}
        </div>
        {icon && (
          <span className="w-7 h-7 rounded-[var(--radius-control)] bg-white/5 border border-[var(--border)] text-[var(--fg-2)] flex items-center justify-center">
            {icon}
          </span>
        )}
      </div>
      {children}
    </motion.div>
  );
}

function SkeletonGrid() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
        {PRIMARY_SKELETON_KEYS.map((key, i) => (
          <div
            key={key}
            className="h-28 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)]"
            style={{ animationDelay: `${i * 80}ms` }}
          />
        ))}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {SECONDARY_SKELETON_KEYS.map((key, i) => (
          <div
            key={key}
            className="h-32 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)]"
            style={{ animationDelay: `${(i + 4) * 80}ms` }}
          />
        ))}
      </div>
    </div>
  );
}

function ErrorBox({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div role="alert" className="flex flex-wrap items-center justify-between gap-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-4 sm:p-6">
      <div className="flex items-start gap-3 min-w-0">
        <AlertCircle className="w-5 h-5 text-danger shrink-0 mt-0.5" />
        <div>
          <p className="type-body-sm text-[var(--danger-fg)]">加载失败</p>
          <p className="type-caption text-[var(--fg-2)] mt-1 break-words">{message}</p>
        </div>
      </div>
      <Button
        variant="secondary"
        size="md"
        onClick={onRetry}
        leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
        className="w-full sm:w-auto"
      >
        {copy.action.retry}
      </Button>
    </div>
  );
}

// ———————————————————— helpers ————————————————————

function formatDay(iso: string): string {
  try {
    return format(new Date(iso), "yyyy-MM-dd");
  } catch {
    return iso;
  }
}

function formatThousands(n: number): string {
  const [intPart, fractionPart] = String(n).split(".");
  const formattedInt = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return fractionPart ? `${formattedInt}.${fractionPart}` : formattedInt;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  const gb = mb / 1024;
  return `${gb.toFixed(2)} GB`;
}

function microToRmbText(micro: number): string {
  return `¥${(micro / 1_000_000).toFixed(2)}`;
}

function txAmount(tx: WalletTransactionOut): string {
  return microToRmbText(Math.abs(tx.amount.micro));
}

function txLabel(tx: WalletTransactionOut): string {
  const meta = tx.meta ?? {};
  const actual = typeof meta.actual_micro === "number" ? meta.actual_micro : null;
  const preauth = typeof meta.preauth_micro === "number" ? meta.preauth_micro : null;
  if (actual != null && preauth != null) {
    return `实际 ${microToRmbText(actual)} / 预估 ${microToRmbText(preauth)}`;
  }
  if (tx.kind === "settle") return "图片生成实际扣费";
  if (tx.kind === "charge_completion") return "对话任务实际扣费";
  return "实际扣费";
}
