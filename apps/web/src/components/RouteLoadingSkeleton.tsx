export function RouteLoadingSkeleton({
  title = "加载中",
}: {
  title?: string;
}) {
  return (
    <main className="min-h-[60dvh] px-4 py-6 sm:px-6 lg:px-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="type-caption text-[var(--fg-muted)]">{title}</p>
            <div className="mt-3 h-8 w-48 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)]" />
          </div>
          <div className="h-10 w-24 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-2)]" />
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
