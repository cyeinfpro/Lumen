export function RouteLoadingSkeleton({
  title = "加载中",
}: {
  title?: string;
}) {
  return (
    <main
      role="status"
      aria-busy="true"
      aria-label={title}
      className="safe-x-page min-h-[100dvh] pb-[var(--mobile-content-bottom)] pt-[calc(env(safe-area-inset-top,0px)+1rem)] sm:py-6 lg:px-8"
    >
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-muted)]">{title}</p>
            <div className="mt-3 h-8 w-[min(12rem,70vw)] animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)]" />
          </div>
          <div className="h-11 w-full animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)] sm:h-10 sm:w-24" />
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          <div className="h-32 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]" />
          <div className="h-32 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]" />
          <div className="h-32 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]" />
        </div>
        <div className="h-80 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]" />
      </div>
    </main>
  );
}
