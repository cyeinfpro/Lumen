"use client";

// 两种空态：
// - kind="never"  从未生成过 → hero + CTA 去创作
// - kind="filtered" 筛选后空 → 清除筛选

import { useRouter } from "next/navigation";

export interface StreamEmptyStateProps {
  kind: "never" | "filtered";
  onClearFilters?: () => void;
}

export function StreamEmptyState({ kind, onClearFilters }: StreamEmptyStateProps) {
  const router = useRouter();

  if (kind === "filtered") {
    return (
      <div className="flex flex-col items-center justify-center text-center px-6 py-16">
        <div className="text-[15px] text-[var(--fg-1)]">
          这组筛选下还没有作品
        </div>
        <button
          type="button"
          onClick={onClearFilters}
          className="mt-4 h-11 px-4 rounded-full border border-[var(--border-amber)] text-[13px] text-[var(--amber-300)] bg-[rgba(242,169,58,0.08)] active:bg-[rgba(242,169,58,0.15)]"
        >
          清除筛选
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center text-center px-6 py-20">
      <div
        className="font-display italic text-[var(--fg-0)]"
        style={{ fontSize: 36, lineHeight: 1.05 }}
      >
        还没有作品
      </div>
      <div
        className="mt-2 text-[15px] text-[var(--fg-1)]"
        style={{ fontFamily: "var(--font-zh-body)" }}
      >
        生成后的图片会放在这里
      </div>
      <button
        type="button"
        onClick={() => router.push("/")}
        className="mt-6 h-11 px-5 rounded-full bg-[var(--amber-400)] text-[var(--bg-0)] text-[14px] font-medium shadow-amber active:scale-[0.98] transition-transform"
      >
        去工作台
      </button>
    </div>
  );
}
