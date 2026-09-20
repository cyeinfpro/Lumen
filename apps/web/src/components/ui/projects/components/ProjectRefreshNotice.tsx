"use client";

interface ProjectRefreshNoticeProps {
  error: unknown;
  refreshing: boolean;
  onRetry: () => void;
}

export function ProjectRefreshNotice({
  error,
  refreshing,
  onRetry,
}: ProjectRefreshNoticeProps) {
  if (!error) return null;
  return (
    <div
      role="status"
      className="mx-3 mt-3 flex flex-wrap items-center justify-between gap-2 border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 type-body-sm text-[var(--fg-1)] md:mx-6"
    >
      <span>暂时无法同步最新状态，已保留当前内容。</span>
      <button
        type="button"
        disabled={refreshing}
        onClick={onRetry}
        aria-label="重新同步项目状态"
        className="min-h-11 cursor-pointer px-3 type-caption text-accent underline-offset-4 hover:underline disabled:cursor-wait disabled:opacity-50"
      >
        {refreshing ? "正在同步…" : "重新同步"}
      </button>
    </div>
  );
}
