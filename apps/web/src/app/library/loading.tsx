import { StreamLoadingState } from "@/components/ui/stream";

export default function Loading() {
  return (
    <main className="mx-auto w-full max-w-7xl px-2 pb-[calc(env(safe-area-inset-bottom,0px)+1rem)] pt-3 md:px-6 md:pt-6">
      <div className="mb-3 h-11 w-36 animate-shimmer rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
      <StreamLoadingState columns={2} />
    </main>
  );
}
