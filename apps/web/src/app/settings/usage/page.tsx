"use client";

// Lumen V1 使用统计（无配额限制；支持选择展示窗口）。

import { useState } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { format } from "date-fns";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  CalendarDays,
  Database,
  Image as ImageIcon,
  MessageSquare,
  RefreshCw,
  Sparkles,
  Zap,
} from "lucide-react";

import { apiFetch } from "@/lib/apiClient";
import type { UsageOut } from "@/lib/types";

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
  const q = useQuery<UsageOut>({
    queryKey: ["me", "usage", { days }],
    queryFn: () => getUsage(days),
    placeholderData: (previous) => previous,
    staleTime: 30_000,
  });
  const selectedPeriod =
    USAGE_PERIODS.find((period) => period.value === days) ?? USAGE_PERIODS[1];

  return (
    <motion.div
      initial={false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200"
    >
      <div className="max-w-6xl mx-auto px-4 md:px-8 py-6 md:py-10 safe-x mobile-compact">
        <header className="mb-6 md:mb-8 flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">
              用量统计
            </h1>
            <p className="text-sm text-[var(--fg-1)] mt-1.5">
              过去 {selectedPeriod.label} 的使用记录（内测期无配额限制）
            </p>
            {q.data && (
              <p className="text-xs text-neutral-500 mt-1 font-mono tabular-nums">
                {formatDay(q.data.range_start)} —{" "}
                {formatDay(q.data.range_end)}
              </p>
            )}
          </div>
          <div className="flex flex-col sm:flex-row sm:items-center gap-3">
            <UsageRangePicker value={days} onChange={setDays} pending={q.isFetching} />
            <Link
              href="/me"
              className="inline-flex items-center gap-1.5 text-sm text-neutral-400 hover:text-neutral-100 transition-colors"
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
          <UsageView data={q.data} />
        ) : null}
      </div>
    </motion.div>
  );
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
      className="inline-flex items-center gap-1 rounded-xl border border-white/10 bg-white/[0.04] p-1"
      aria-label="用量时间范围"
    >
      <CalendarDays className="ml-2 h-3.5 w-3.5 text-neutral-500" />
      {USAGE_PERIODS.map((period) => {
        const active = value === period.value;
        return (
          <button
            key={period.value}
            type="button"
            onClick={() => onChange(period.value)}
            disabled={pending && active}
            className={
              "h-8 rounded-lg px-2.5 text-xs transition-colors " +
              (active
                ? "bg-[var(--color-lumen-amber)] text-black"
                : "text-neutral-400 hover:bg-white/8 hover:text-neutral-100")
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
              <div className="text-2xl md:text-3xl font-semibold font-mono tabular-nums text-neutral-100">
                {formatThousands(data.total_tokens_in)}
              </div>
              <div className="text-xs text-neutral-500 mt-0.5">输入</div>
            </div>
            <div className="h-px w-full md:h-8 md:w-px bg-white/8" />
            <div>
              <div className="text-2xl md:text-3xl font-semibold font-mono tabular-nums text-neutral-100">
                {formatThousands(data.total_tokens_out)}
              </div>
              <div className="text-xs text-neutral-500 mt-0.5">输出</div>
            </div>
          </div>
        </SecondaryCard>

        <SecondaryCard
          label="存储占用"
          icon={<Database className="w-4 h-4" />}
          delay={0.2}
        >
          <div className="text-2xl md:text-3xl font-semibold font-mono tabular-nums text-neutral-100">
            {formatBytes(data.storage_bytes)}
          </div>
          <div className="text-xs text-neutral-500 mt-0.5 font-mono tabular-nums">
            {formatThousands(data.storage_bytes)} bytes
          </div>
        </SecondaryCard>
      </div>
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
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay, ease: "easeOut" }}
      className="group rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 transition-all hover:-translate-y-0.5 hover:border-white/20 hover:bg-[var(--bg-1)]/80 hover:shadow-[0_12px_30px_-16px_rgba(0,0,0,0.6)]"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-[var(--fg-1)]">
          {label}
        </span>
        {icon && (
          <span className="w-7 h-7 rounded-lg bg-[var(--color-lumen-amber)]/12 border border-[var(--color-lumen-amber)]/20 text-[var(--color-lumen-amber)] flex items-center justify-center group-hover:bg-[var(--color-lumen-amber)]/20 transition-colors">
            {icon}
          </span>
        )}
      </div>
      <div className="text-2xl md:text-3xl font-semibold font-mono tabular-nums text-neutral-100 mt-3 whitespace-nowrap overflow-hidden text-ellipsis">
        {value}
      </div>
      {sublabel && (
        <div className="text-xs text-neutral-500 mt-1">{sublabel}</div>
      )}
      {ratio != null && (
        <div className="mt-3 h-1 rounded-full bg-white/5 overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${Math.max(0, Math.min(100, ratio))}%` }}
            transition={{ duration: 0.5, delay: delay + 0.15 }}
            className="h-full rounded-full bg-gradient-to-r from-[var(--color-lumen-amber)]/80 to-[var(--color-lumen-amber)]"
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
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay, ease: "easeOut" }}
      className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 transition-all hover:-translate-y-0.5 hover:border-white/20 hover:bg-[var(--bg-1)]/80 hover:shadow-[0_12px_30px_-16px_rgba(0,0,0,0.6)]"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs uppercase tracking-wider text-[var(--fg-1)]">
          {label}
        </div>
        {icon && (
          <span className="w-7 h-7 rounded-lg bg-white/5 border border-white/10 text-neutral-400 flex items-center justify-center">
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
            className="h-28 rounded-2xl bg-white/5 animate-pulse"
            style={{ animationDelay: `${i * 80}ms` }}
          />
        ))}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {SECONDARY_SKELETON_KEYS.map((key, i) => (
          <div
            key={key}
            className="h-32 rounded-2xl bg-white/5 animate-pulse"
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
    <div className="rounded-2xl border border-red-500/30 bg-red-500/5 p-6 flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-start gap-3 min-w-0">
        <AlertCircle className="w-5 h-5 text-red-300 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm text-red-200">加载失败</p>
          <p className="text-xs text-neutral-400 mt-1 break-words">{message}</p>
        </div>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex items-center justify-center gap-1.5 h-11 sm:h-9 w-full sm:w-auto px-4 rounded-xl bg-white/10 hover:bg-white/15 border border-white/15 text-sm transition-colors"
      >
        <RefreshCw className="w-3.5 h-3.5" /> 重试
      </button>
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
  return n.toLocaleString("en-US");
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
