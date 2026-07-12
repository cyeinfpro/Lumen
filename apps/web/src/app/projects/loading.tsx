export default function Loading() {
  return (
    <div className="flex h-[100dvh] min-h-0 flex-1 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="h-[calc(52px+env(safe-area-inset-top,0px))] shrink-0 border-b border-[var(--border)] bg-[var(--bg-0)]/92" />
      <main className="min-h-0 flex-1 overflow-hidden px-3 py-3 md:px-6">
        <div className="mx-auto grid w-full max-w-[1440px] gap-4">
          <div className="grid gap-2 border-b border-[var(--border)] pb-4">
            <div className="h-3 w-20 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
            <div className="h-7 w-48 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
            <div className="h-4 w-full max-w-md animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-1)]" />
          </div>
          <div className="grid grid-cols-3 gap-2">
            {[0, 1, 2].map((item) => (
              <div
                key={item}
                className="h-16 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]"
              />
            ))}
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {[0, 1, 2, 3].map((item) => (
              <div
                key={item}
                className="h-48 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]"
              />
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
