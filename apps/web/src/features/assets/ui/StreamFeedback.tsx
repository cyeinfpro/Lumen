"use client";

import { AlertTriangle, FilterX, Images, RefreshCw, SearchX, WandSparkles } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/primitives";
import { mediaLoadError } from "../model/mediaLoadError";

const SKELETON_RATIOS = ["3/4", "4/3", "1/1", "3/4", "4/5", "16/9", "3/4", "4/3", "1/1"];

export function StreamLoadingState({ columns = 2 }: { columns?: number }) {
  const columnCount = Math.max(1, Math.floor(columns));
  const gap = columnCount > 2 ? 14 : 8;
  const skeletonColumns = Array.from({ length: columnCount }, () => [] as string[]);
  SKELETON_RATIOS.forEach((ratio, index) => skeletonColumns[index % columnCount].push(ratio));
  return (
    <div className="px-2 py-3 md:px-0 md:py-4" aria-label="图库加载中" role="status">
      <div className="grid" style={{ gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))`, gap }}>
        {skeletonColumns.map((column, columnIndex) => (
          <div key={columnIndex} className="flex min-w-0 flex-col" style={{ gap }}>
            {column.map((ratio, index) => (
              <div key={index} className="overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-1)]">
                <div className="animate-shimmer bg-[var(--bg-2)]" style={{ aspectRatio: ratio }} />
                <div className="space-y-2 p-2.5">
                  <div className="h-3 w-5/6 rounded-full bg-[var(--bg-2)]" />
                  <div className="h-3 w-2/3 rounded-full bg-[var(--bg-2)]" />
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function StreamErrorState({ error, onRetry, compact = false }: {
  error?: unknown;
  onRetry: () => void;
  compact?: boolean;
}) {
  const detail = mediaLoadError(error);
  return (
    <section role="alert" className={compact ? "flex flex-wrap items-center gap-3 border-y border-[var(--border)] py-3" : "flex min-h-60 flex-col items-center justify-center gap-3 py-8 text-center"}>
      <AlertTriangle className="h-5 w-5 shrink-0 text-[var(--warning-fg)]" aria-hidden />
      <div className="min-w-0">
        <h2 className="type-card-title text-[var(--fg-0)]">{detail.title}</h2>
        <p className="mt-1 break-words type-body-sm text-[var(--fg-2)]">{detail.detail}</p>
      </div>
      {detail.retryable ? (
        <Button variant="outline" onClick={onRetry} leftIcon={<RefreshCw className="h-4 w-4" />}>重新加载</Button>
      ) : (
        <Link href={detail.title === "登录已失效" ? "/login" : "/"} className="inline-flex min-h-11 items-center type-body-sm text-[var(--fg-0)] underline">
          {detail.title === "登录已失效" ? "登录" : "回到创作"}
        </Link>
      )}
    </section>
  );
}

export function StreamNoResultsState({ searchValue, onClear }: { searchValue?: string; onClear: () => void }) {
  return (
    <StreamStatePanel icon={<SearchX className="h-5 w-5" />} title="没有匹配作品" description={searchValue?.trim() ? `搜索：${searchValue.trim()}` : "当前筛选下没有作品。"}>
      <Button variant="outline" onClick={onClear} leftIcon={<FilterX className="h-4 w-4" />}>清除条件</Button>
    </StreamStatePanel>
  );
}

export function StreamNeverState() {
  const router = useRouter();
  return (
    <StreamStatePanel icon={<Images className="h-5 w-5" />} title="暂无作品" description="还没有生成完成的图片。">
      <Button onClick={() => router.push("/")} leftIcon={<WandSparkles className="h-4 w-4" />}>去创作</Button>
    </StreamStatePanel>
  );
}

function StreamStatePanel({ icon, title, description, children }: { icon: ReactNode; title: string; description: string; children: ReactNode }) {
  return (
    <section className="flex min-h-60 flex-col items-center justify-center gap-3 px-3 py-8 text-center">
      <span className="text-[var(--fg-2)]" aria-hidden>{icon}</span>
      <h2 className="type-card-title text-[var(--fg-0)]">{title}</h2>
      <p className="max-w-xl break-words type-body-sm text-[var(--fg-2)]">{description}</p>
      {children}
    </section>
  );
}
