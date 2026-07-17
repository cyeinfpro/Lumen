export default function ShareLoading() {
  return (
    <div className="page-shell">
      <header className="sticky top-0 z-10 border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/96 pt-[env(safe-area-inset-top)] backdrop-blur-xl">
        <div className="safe-x-page-wide mx-auto flex min-h-14 max-w-6xl items-center justify-between">
          <div className="h-6 w-28 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
          <div className="h-10 w-28 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
        </div>
      </header>
      <main className="page-scroll">
        <div className="page-frame flex flex-col gap-4 md:gap-7" data-width="media">
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <div className="space-y-2">
              <div className="h-7 w-32 animate-lumen-shimmer rounded-full bg-[linear-gradient(90deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]" />
              <div className="h-4 w-64 max-w-full animate-lumen-shimmer rounded bg-[linear-gradient(90deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]" />
            </div>
            <div className="flex flex-wrap gap-2 md:justify-end">
              <div className="h-11 w-36 animate-lumen-shimmer rounded-[var(--radius-card)] border border-[var(--border)] bg-[linear-gradient(90deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]" />
              <div className="h-11 w-24 animate-lumen-shimmer rounded-[var(--radius-card)] border border-[var(--border)] bg-[linear-gradient(90deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]" />
            </div>
          </div>
          <div className="columns-2 gap-1.5 min-[390px]:gap-2 sm:columns-3 md:columns-4 md:gap-3 xl:columns-5">
            {Array.from({ length: 14 }).map((_, index) => (
              <div
                key={index}
                className="mb-1.5 break-inside-avoid overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/70 min-[390px]:mb-2 md:mb-3"
              >
                <div
                  className="animate-lumen-shimmer bg-[linear-gradient(110deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]"
                  style={{
                    aspectRatio:
                      index % 5 === 0
                        ? "4 / 5"
                        : index % 4 === 0
                          ? "9 / 16"
                          : index % 3 === 0
                            ? "16 / 10"
                            : "1 / 1",
                  }}
                />
              </div>
            ))}
          </div>
          <div className="grid w-full max-w-4xl gap-3 self-center md:grid-cols-[minmax(0,1fr)_auto]">
            <div className="h-12 animate-lumen-shimmer rounded-[var(--radius-card)] border border-[var(--border)] bg-[linear-gradient(90deg,var(--bg-2),var(--bg-3),var(--bg-2))] bg-[length:220%_100%]" />
            <div className="h-11 animate-lumen-shimmer rounded-[var(--radius-card)] bg-[linear-gradient(90deg,rgba(242,169,58,0.20),rgba(242,169,58,0.36),rgba(242,169,58,0.20))] bg-[length:220%_100%] md:w-28" />
          </div>
        </div>
      </main>
    </div>
  );
}
