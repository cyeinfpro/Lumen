import { Button } from "@/components/ui/primitives";

export function CollectionState({
  error,
  loading,
  empty,
  loadingMessage,
  emptyMessage,
}: {
  error: string | null;
  loading: boolean;
  empty: boolean;
  loadingMessage: string;
  emptyMessage: string;
}) {
  if (error || !empty) return null;
  return (
    <CollectionStatus message={loading ? loadingMessage : emptyMessage} />
  );
}

function CollectionStatus({ message }: { message: string }) {
  return (
    <div
      role="status"
      className="px-4 py-10 text-center type-body-sm text-[var(--fg-2)]"
    >
      {message}
    </div>
  );
}

export function LoadMore({
  visible,
  loading,
  onLoad,
}: {
  visible: boolean;
  loading: boolean;
  onLoad: () => void;
}) {
  if (!visible) return null;
  return (
    <div className="border-t border-[var(--border-subtle)] px-4 py-3 text-center">
      <Button
        variant="outline"
        size="sm"
        loading={loading}
        onClick={onLoad}
      >
        加载更多
      </Button>
    </div>
  );
}
