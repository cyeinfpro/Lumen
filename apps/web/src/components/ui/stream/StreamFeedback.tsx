"use client";

import { AlertTriangle, RefreshCw, SearchX, WandSparkles } from "lucide-react";
import { useRouter } from "next/navigation";

const SKELETON_RATIOS = [
  "3/4", "4/3", "1/1", "3/4", "4/5", "16/9",
  "3/4", "4/3", "1/1",
];

export function StreamLoadingState({ columns = 2 }: { columns?: number }) {
  const columnCount = Math.max(1, Math.floor(columns));
  const gap = columnCount > 2 ? 14 : 10;
  const skeletonColumns = Array.from(
    { length: columnCount },
    () => [] as Array<{ ratio: string; index: number }>,
  );
  SKELETON_RATIOS.forEach((ratio, index) => {
    skeletonColumns[index % columnCount].push({ ratio, index });
  });

  return (
    <div className="px-3 py-4 md:px-0" aria-label="正在加载图库">
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
                className="overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-1)] shadow-[var(--shadow-1)]"
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
  return (
    <div className="flex flex-col items-center justify-center px-6 py-20 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border-amber)] bg-[rgba(242,169,58,0.1)] text-[var(--amber-300)]">
        <AlertTriangle className="h-5 w-5" />
      </div>
      <div className="mt-4 text-[15px] font-medium text-[var(--fg-0)]">
        图库暂时没有载入
      </div>
      <div className="mt-2 max-w-[30rem] text-[13px] leading-relaxed text-[var(--fg-2)]">
        {message || "可能是网络或登录状态过期，刷新后会保留当前筛选。"}
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="mt-6 inline-flex min-h-11 cursor-pointer items-center gap-2 rounded-full bg-[var(--amber-400)] px-4 text-[13px] font-medium text-[var(--bg-0)] shadow-amber transition-opacity hover:opacity-90 focus-visible:outline-none"
      >
        <RefreshCw className="h-4 w-4" />
        重新加载
      </button>
    </div>
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
    <div className="flex flex-col items-center justify-center px-6 py-20 text-center animate-fade-in">
      <div className="flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]">
        <SearchX className="h-5 w-5" />
      </div>
      <div className="mt-4 text-[15px] font-medium text-[var(--fg-0)]">
        {label}
      </div>
      <div className="mt-2 max-w-[28rem] text-[13px] leading-relaxed text-[var(--fg-2)]">
        清除条件查看全部作品，或继续下滑加载更早内容。
      </div>
      <button
        type="button"
        onClick={onClear}
        className="mt-6 inline-flex min-h-11 cursor-pointer items-center rounded-full border border-[var(--border-amber)] bg-[rgba(242,169,58,0.08)] px-4 text-[13px] text-[var(--amber-300)] transition-colors hover:bg-[rgba(242,169,58,0.14)] focus-visible:outline-none"
      >
        清除条件
      </button>
    </div>
  );
}

export function StreamNeverState() {
  const router = useRouter();
  return (
    <div className="flex flex-col items-center justify-center px-6 py-24 text-center animate-fade-in">
      <div className="flex h-14 w-14 items-center justify-center rounded-full border border-[var(--border-amber)] bg-[rgba(242,169,58,0.1)] text-[var(--amber-300)] shadow-amber">
        <WandSparkles className="h-6 w-6" />
      </div>
      <div
        className="mt-6 font-display italic text-[var(--fg-0)]"
        style={{ fontSize: 36, lineHeight: 1.05 }}
      >
        还没有作品
      </div>
      <div className="mt-3 max-w-[26rem] text-[15px] leading-7 text-[var(--fg-2)]">
        生成后的图片会放在这里。
      </div>
      <button
        type="button"
        onClick={() => router.push("/")}
        className="mt-8 inline-flex h-11 cursor-pointer items-center gap-2 rounded-full bg-[var(--amber-400)] px-5 text-[14px] font-medium text-[var(--bg-0)] shadow-amber transition-opacity hover:opacity-90 focus-visible:outline-none"
      >
        <WandSparkles className="h-4 w-4" />
        去工作台
      </button>
    </div>
  );
}
