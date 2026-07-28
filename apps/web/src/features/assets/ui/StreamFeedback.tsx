"use client";

import {
  AlertTriangle,
  FilterX,
  ImagePlus,
  Images,
  RefreshCw,
  SearchX,
  WandSparkles,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

const SKELETON_RATIOS = [
  "3/4", "4/3", "1/1", "3/4", "4/5", "16/9",
  "3/4", "4/3", "1/1",
];

export function StreamLoadingState({ columns = 2 }: { columns?: number }) {
  const columnCount = Math.max(1, Math.floor(columns));
  const gap = columnCount > 2 ? 14 : 8;
  const skeletonColumns = Array.from(
    { length: columnCount },
    () => [] as Array<{ ratio: string; index: number }>,
  );
  SKELETON_RATIOS.forEach((ratio, index) => {
    skeletonColumns[index % columnCount].push({ ratio, index });
  });

  return (
    <div className="px-2 py-3 md:px-0 md:py-4" aria-label="正在加载图库">
      <div
        className="grid"
        style={{
          gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))`,
          gap,
        }}
      >
        {skeletonColumns.map((col, colIndex) => (
          <div
            key={colIndex}
            className="flex min-w-0 flex-col"
            style={{ gap }}
          >
            {col.map(({ ratio, index }) => (
              <div
                key={index}
                className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] shadow-[var(--shadow-1)]"
                style={{ animationDelay: `${index * 60}ms` }}
              >
                <div
                  className="animate-shimmer bg-[var(--bg-2)]"
                  style={{ aspectRatio: ratio }}
                />
                <div className="space-y-2 p-2.5">
                  <div className="h-3 w-5/6 rounded-full bg-[var(--bg-2)]" />
                  <div className="h-3 w-2/3 rounded-full bg-[var(--bg-2)]" />
                  <div className="flex gap-1.5">
                    <div className="h-[18px] w-10 rounded bg-[var(--bg-2)]" />
                    <div className="h-[18px] w-8 rounded bg-[var(--bg-2)]" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function StreamErrorState({
  message,
  onRetry,
}: {
  message?: string;
  onRetry: () => void;
}) {
  const detail = normalizeStreamError(message);
  return (
    <StreamStatePanel
      icon={<AlertTriangle className="h-5 w-5" />}
      tone="warning"
      eyebrow="素材库暂不可用"
      title="图库没有成功载入"
      description="当前素材列表请求失败。筛选、搜索和已加载内容不会被清空，可以先重试，也可以回到创作页继续生成。"
      primaryAction={
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[box-shadow,transform] hover:shadow-[var(--shadow-amber)] active:scale-[0.98] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <RefreshCw className="h-4 w-4" />
          重新加载
        </button>
      }
      secondaryAction={
        <Link
          href="/"
          className="inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-4 text-sm font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          回到创作
        </Link>
      }
    >
      <div className="grid gap-2 text-left text-xs leading-5 text-[var(--fg-2)] sm:grid-cols-3">
        <StateFact label="状态" value={detail.label} />
        <StateFact label="筛选" value="保留当前条件" />
        <StateFact label="建议" value="重试后再刷新页面" />
      </div>
      {detail.diagnostic ? (
        <p className="mt-3 truncate rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/58 px-3 py-2 text-left font-mono text-[11px] text-[var(--fg-2)]">
          诊断信息：{detail.diagnostic}
        </p>
      ) : null}
    </StreamStatePanel>
  );
}

export function StreamNoResultsState({
  searchValue,
  onClear,
}: {
  searchValue?: string;
  onClear: () => void;
}) {
  const label = searchValue?.trim()
    ? `没有找到包含"${searchValue.trim()}"的作品`
    : "当前筛选下暂无作品";

  return (
    <StreamStatePanel
      icon={<SearchX className="h-5 w-5" />}
      tone="neutral"
      eyebrow="没有匹配结果"
      title={label}
      description="当前搜索或筛选条件太窄。清除条件后会回到完整素材流，也可以换一个提示词关键词继续搜索。"
      primaryAction={
        <button
          type="button"
          onClick={onClear}
          className="inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] px-4 text-sm font-medium text-[var(--warning-fg)] transition-colors hover:bg-warning-soft focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <FilterX className="h-4 w-4" />
          清除条件
        </button>
      }
    >
      <div className="grid gap-2 text-left text-xs leading-5 text-[var(--fg-2)] sm:grid-cols-3">
        <StateFact label="搜索" value={searchValue?.trim() || "未输入关键词"} />
        <StateFact label="操作" value="清除筛选" />
        <StateFact label="结果" value="恢复全部作品" />
      </div>
    </StreamStatePanel>
  );
}

export function StreamNeverState() {
  const router = useRouter();
  return (
    <StreamStatePanel
      icon={<Images className="h-5 w-5" />}
      tone="accent"
      eyebrow="素材流为空"
      title="还没有作品进入素材库"
      description="生成完成后的图片会自动沉淀到这里。之后可以按比例、参考图、Fast 模式筛选，也可以批量选择并创建分享链接。"
      primaryAction={
        <button
          type="button"
          onClick={() => router.push("/")}
          className="inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[box-shadow,transform] hover:shadow-[var(--shadow-amber)] active:scale-[0.98] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <WandSparkles className="h-4 w-4" />
          去创作
        </button>
      }
      secondaryAction={
        <Link
          href="/projects"
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-4 text-sm font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
        >
          <ImagePlus className="h-4 w-4" />
          看项目
        </Link>
      }
    >
      <div className="grid gap-2 text-left text-xs leading-5 text-[var(--fg-2)] sm:grid-cols-3">
        <StateFact label="生成" value="创作页出图" />
        <StateFact label="管理" value="素材页筛选" />
        <StateFact label="复用" value="批量分享与定位" />
      </div>
    </StreamStatePanel>
  );
}

function StreamStatePanel({
  icon,
  tone,
  eyebrow,
  title,
  description,
  primaryAction,
  secondaryAction,
  children,
}: {
  icon: ReactNode;
  tone: "accent" | "neutral" | "warning";
  eyebrow: string;
  title: string;
  description: string;
  primaryAction: ReactNode;
  secondaryAction?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="grid min-h-[min(420px,62dvh)] place-items-center px-3 py-8 text-center animate-fade-in md:min-h-[520px] md:px-0 md:py-10">
      <div className="w-full max-w-3xl rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/84 p-4 text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-sm sm:p-5 md:p-6">
        <div className="mx-auto flex max-w-2xl flex-col items-center">
          <span className={stateIconClass(tone)}>
            {icon}
          </span>
          <p className="mt-4 type-page-kicker">{eyebrow}</p>
          <h2 className="mt-2 text-[22px] font-semibold leading-tight tracking-tight text-[var(--fg-0)] md:text-[26px]">
            {title}
          </h2>
          <p className="mt-3 max-w-xl text-sm leading-6 text-[var(--fg-1)]">
            {description}
          </p>
          <div className="mt-5 flex w-full flex-col justify-center gap-2 sm:w-auto sm:flex-row">
            {primaryAction}
            {secondaryAction}
          </div>
        </div>
        {children ? (
          <div className="mt-5 border-t border-[var(--border-subtle)] pt-4">
            {children}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function StateFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/48 px-3 py-2">
      <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
        {label}
      </p>
      <p className="mt-1 truncate text-[12px] text-[var(--fg-1)]">{value}</p>
    </div>
  );
}

function stateIconClass(tone: "accent" | "neutral" | "warning") {
  if (tone === "warning") {
    return "flex h-12 w-12 items-center justify-center rounded-[var(--radius-card)] border border-warning-border bg-warning-soft text-[var(--warning-fg)] shadow-[var(--shadow-1)]";
  }
  if (tone === "accent") {
    return "flex h-12 w-12 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)] shadow-[var(--shadow-1)]";
  }
  return "flex h-12 w-12 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-1)] shadow-[var(--shadow-1)]";
}

function normalizeStreamError(message?: string): { label: string; diagnostic?: string } {
  const raw = message?.trim();
  if (!raw) return { label: "请求失败" };
  if (/internal server error/i.test(raw)) {
    return { label: "服务端错误", diagnostic: "服务端返回 500 错误" };
  }
  if (/unauthori[sz]ed|forbidden|401|403/i.test(raw)) {
    return { label: "登录状态异常", diagnostic: "需要重新确认登录状态或访问权限" };
  }
  if (/network|fetch|timeout/i.test(raw)) {
    return { label: "网络连接异常", diagnostic: "请求超时或网络连接失败" };
  }
  return { label: "请求失败", diagnostic: raw };
}
