"use client";

import { LumenMark } from "@/components/ui/brand/LumenMark";

/**
 * 首次 render（useMediaQuery 尚未 resolve）时渲染，避免 hydration mismatch。
 * 结构与真实 App Shell 对齐，避免从独立品牌页跳到工作台。
 */
export function ShellSkeleton() {
  return (
    <div
      className="fixed inset-0 flex min-h-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]"
      role="status"
      aria-label="正在加载 Lumen"
    >
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/92 px-4 md:px-6">
        <div className="flex items-center gap-2">
          <LumenMark className="text-[var(--accent)]" />
          <span className="hidden text-[15px] font-semibold sm:inline">Lumen</span>
        </div>
        <div className="flex items-center gap-2">
          <SkeletonLine className="hidden h-8 w-56 md:block" />
          <SkeletonLine className="h-9 w-9 rounded-full" />
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-16 shrink-0 border-r border-[var(--border-subtle)] bg-[var(--bg-1)] p-3 min-[1120px]:block min-[1440px]:w-64 min-[1440px]:p-4">
          <div className="grid justify-center gap-3 min-[1440px]:hidden">
            <SkeletonLine className="h-9 w-9" />
            <SkeletonLine className="h-9 w-9" />
            <SkeletonLine className="h-9 w-9" />
          </div>
          <div className="hidden min-[1440px]:block">
            <SkeletonLine className="h-10 w-full" />
            <SkeletonLine className="mt-4 h-9 w-full" />
            <div className="mt-6 grid gap-2">
              <SkeletonLine className="h-8 w-[88%]" />
              <SkeletonLine className="h-8 w-[72%]" />
              <SkeletonLine className="h-8 w-[82%]" />
              <SkeletonLine className="h-8 w-[64%]" />
            </div>
          </div>
        </aside>

        <main className="relative min-h-0 flex-1 overflow-hidden px-4 pt-6 md:px-8">
          <div className="mx-auto w-full max-w-[var(--content-text)] space-y-7">
            <div className="ml-auto w-[68%] space-y-2">
              <SkeletonLine className="h-4 w-full" />
              <SkeletonLine className="ml-auto h-4 w-[74%]" />
            </div>
            <div className="space-y-2">
              <SkeletonLine className="h-4 w-[92%]" />
              <SkeletonLine className="h-4 w-[84%]" />
              <SkeletonLine className="h-4 w-[62%]" />
            </div>
          </div>

          <div className="absolute inset-x-4 bottom-5 mx-auto h-14 max-w-[var(--content-composer)] rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)] shadow-[var(--shadow-2)]">
            <div className="flex h-full items-center gap-3 px-3">
              <SkeletonLine className="h-9 w-9 rounded-[var(--radius-control)]" />
              <SkeletonLine className="h-4 flex-1" />
              <SkeletonLine className="h-9 w-9 rounded-full" />
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

function SkeletonLine({ className }: { className: string }) {
  return (
    <span
      aria-hidden
      className={`block animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)] ${className}`}
    />
  );
}
