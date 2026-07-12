import { AlertCircle, Inbox } from "lucide-react";

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
    <div className="flex flex-col items-center gap-3 py-14 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-2)]">
        <Inbox className="h-5 w-5 text-[var(--fg-2)]" />
      </div>
      <div>
        <p className="text-sm text-[var(--fg-0)]">{title}</p>
        {description && (
          <p className="mt-1 text-xs text-[var(--fg-2)]">{description}</p>
        )}
      </div>
      {cta}
    </div>
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
    <div className="flex items-center justify-between gap-4 rounded-[var(--radius-dialog)] border border-danger-border bg-danger-soft p-6">
      <div className="flex items-start gap-3">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-danger" />
        <div>
          <p className="type-body-sm text-danger">加载失败</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">{message}</p>
        </div>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="h-11 shrink-0 rounded-[var(--radius-control)] border border-[var(--border-strong)] bg-[var(--bg-2)] px-3 text-sm transition-colors hover:bg-[var(--bg-3)] md:h-8 md:text-xs"
        >
          重试
        </button>
      )}
    </div>
  );
}
