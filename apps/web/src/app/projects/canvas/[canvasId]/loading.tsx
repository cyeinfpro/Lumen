export default function CanvasLoading() {
  return (
    <div className="grid h-[100dvh] grid-rows-[56px_minmax(0,1fr)] bg-[var(--bg-0)]">
      <div className="border-b border-[var(--border)] bg-[var(--bg-1)]" />
      <div className="animate-pulse bg-[var(--surface-canvas)]" />
    </div>
  );
}
