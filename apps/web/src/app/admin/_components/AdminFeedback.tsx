import { Inbox } from "lucide-react";

import { EmptyState, ErrorState } from "@/components/ui/primitives";

export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  const keys = Array.from(
    { length: rows },
    (_, index) => `admin-list-skeleton-${index + 1}`,
  );

  return (
    <div className="space-y-3 p-4">
      {keys.map((key, index) => (
        <div
          key={key}
          className="flex animate-pulse items-center gap-3"
          style={{ animationDelay: `${index * 60}ms` }}
        >
          <div className="h-4 w-1/3 rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
          <div className="h-4 w-16 rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
          <div className="h-4 flex-1 rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
          <div className="h-4 w-20 rounded-[var(--radius-control)] bg-[var(--bg-2)]" />
        </div>
      ))}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  cta,
}: {
  title: string;
  description?: string;
  cta?: React.ReactNode;
}) {
  return (
    <EmptyState
      icon={<Inbox className="h-5 w-5" aria-hidden="true" />}
      title={title}
      description={description}
      action={cta}
    />
  );
}

export function ErrorBlock({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <ErrorState
      title="加载失败"
      detail={message}
      onRetry={onRetry}
    />
  );
}
