export default function ShareLoading() {
  return (
    <div className="flex min-h-[100dvh] w-full flex-1 flex-col bg-[linear-gradient(180deg,var(--bg-0)_0%,#0b0b0d_44%,var(--bg-0)_100%)] text-neutral-200">
      <header className="sticky top-0 z-10 border-b border-white/8 bg-[var(--bg-0)]/88 pt-[env(safe-area-inset-top)] backdrop-blur-xl">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 safe-x md:px-8">
          <div className="h-6 w-28 animate-pulse rounded bg-white/5" />
          <div className="h-9 w-28 animate-pulse rounded-full bg-white/5" />
        </div>
      </header>
      <main className="flex flex-1 flex-col items-center justify-start px-4 py-6 safe-x md:px-8 md:py-10">
        <div className="mx-auto flex w-full max-w-[min(94vw,1320px)] flex-col gap-5 md:gap-7">
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <div className="space-y-2">
              <div className="h-7 w-32 animate-lumen-shimmer rounded-full bg-[linear-gradient(90deg,rgba(255,255,255,0.05),rgba(255,255,255,0.12),rgba(255,255,255,0.05))] bg-[length:220%_100%]" />
              <div className="h-4 w-64 max-w-full animate-lumen-shimmer rounded bg-[linear-gradient(90deg,rgba(255,255,255,0.045),rgba(255,255,255,0.10),rgba(255,255,255,0.045))] bg-[length:220%_100%]" />
            </div>
            <div className="flex flex-wrap gap-2 md:justify-end">
              <div className="h-10 w-36 animate-lumen-shimmer rounded-lg border border-white/10 bg-[linear-gradient(90deg,rgba(255,255,255,0.04),rgba(255,255,255,0.10),rgba(255,255,255,0.04))] bg-[length:220%_100%]" />
              <div className="h-10 w-24 animate-lumen-shimmer rounded-lg border border-white/10 bg-[linear-gradient(90deg,rgba(255,255,255,0.04),rgba(255,255,255,0.10),rgba(255,255,255,0.04))] bg-[length:220%_100%]" />
            </div>
          </div>
          <div className="columns-2 gap-2 sm:columns-3 md:columns-4 md:gap-3 xl:columns-5">
            {Array.from({ length: 14 }).map((_, index) => (
              <div
                key={index}
                className="mb-2 break-inside-avoid overflow-hidden rounded-lg border border-white/10 bg-white/[0.04] md:mb-3"
              >
                <div
                  className="animate-lumen-shimmer bg-[linear-gradient(110deg,rgba(255,255,255,0.04),rgba(255,255,255,0.11),rgba(255,255,255,0.04))] bg-[length:220%_100%]"
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
            <div className="h-12 animate-lumen-shimmer rounded-lg border border-white/10 bg-[linear-gradient(90deg,rgba(255,255,255,0.04),rgba(255,255,255,0.10),rgba(255,255,255,0.04))] bg-[length:220%_100%]" />
            <div className="h-11 animate-lumen-shimmer rounded-lg bg-[linear-gradient(90deg,rgba(242,169,58,0.20),rgba(242,169,58,0.36),rgba(242,169,58,0.20))] bg-[length:220%_100%] md:w-28" />
          </div>
        </div>
      </main>
    </div>
  );
}
