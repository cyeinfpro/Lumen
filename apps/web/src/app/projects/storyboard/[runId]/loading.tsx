import { Spinner } from "@/components/ui/primitives/Spinner";

export default function Loading() {
  return (
    <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="grid place-items-center gap-3">
        <Spinner size={20} />
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
          加载分镜项目
        </p>
      </div>
    </div>
  );
}
