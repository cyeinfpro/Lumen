import { Spinner } from "@/components/ui/primitives";

export default function AgentLoading() {
  return (
    <div className="flex min-h-[100dvh] items-center justify-center bg-[var(--bg-0)]">
      <span role="status" className="flex items-center gap-2 type-body-sm text-[var(--fg-2)]">
        <Spinner size={20} /> 加载中
      </span>
    </div>
  );
}
