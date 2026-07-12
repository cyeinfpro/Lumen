import { Spinner } from "@/components/ui/primitives/Spinner";

export default function Loading() {
  return (
    <div className="flex h-[100dvh] min-h-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="h-[calc(52px+env(safe-area-inset-top,0px))] shrink-0 border-b border-[var(--border)]" />
      <div className="flex min-h-0 flex-1 flex-col md:grid md:grid-cols-[232px_minmax(0,1fr)_320px]">
        <div className="flex h-14 gap-2 overflow-hidden border-b border-[var(--border)] p-2 md:h-auto md:flex-col md:border-b-0 md:p-3">
          {[0, 1, 2, 3].map((item) => (
            <div key={item} className="h-11 w-28 shrink-0 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-1)] md:h-[76px] md:w-full" />
          ))}
        </div>
        <main className="grid min-h-0 content-start gap-3 overflow-hidden border-x border-[var(--border)] p-3 md:p-5">
          <div className="h-8 w-52 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
          <div className="h-24 animate-pulse rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]" />
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {[0, 1, 2].map((item) => (
              <div key={item} className="aspect-video animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-1)]" />
            ))}
          </div>
          <div className="flex min-h-24 items-center justify-center gap-3 text-[var(--fg-2)]">
            <Spinner size={20} />
            <span className="type-caption">加载中</span>
          </div>
        </main>
        <div className="hidden bg-[var(--bg-1)]/40 md:block" />
      </div>
    </div>
  );
}
