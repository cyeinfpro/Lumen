"use client";

/**
 * 首次 render（useMediaQuery 尚未 resolve）时渲染，避免 hydration mismatch。
 * 与桌面 / 移动两套外壳视觉接近——暗色底 + 一个居中 Lumen 标记。
 */
export function ShellSkeleton() {
  return (
    <div className="fixed inset-0 flex items-center justify-center bg-[var(--bg-0)]">
      <div className="flex items-center gap-2.5 opacity-70">
        <div className="w-6 h-6 rounded-full bg-gradient-to-tr from-[var(--amber-400)] to-[var(--amber-200)] shadow-[var(--shadow-amber)]" />
        <span className="font-display italic text-[20px] text-[var(--fg-0)]">
          Lumen
        </span>
      </div>
    </div>
  );
}
