import type { CanvasDocument } from "./types";

export type CanvasRemoteSyncDecision =
  | "ignore"
  | "defer"
  | "replace"
  | "conflict";

export function decideCanvasRemoteSync(
  remoteRevision: number,
  state: {
    revision: number;
    pendingOperationCount: number;
    inFlightOperationCount: number;
    activeInteractionCount: number;
    editingNodeId: string | null;
  },
): CanvasRemoteSyncDecision {
  if (remoteRevision <= state.revision) return "ignore";
  if (state.inFlightOperationCount > 0) return "defer";
  return state.pendingOperationCount === 0 &&
    state.activeInteractionCount === 0 &&
    state.editingNodeId === null
    ? "replace"
    : "conflict";
}

export function mergeCanvasDocumentByRevision(
  current: CanvasDocument | undefined,
  incoming: CanvasDocument,
): CanvasDocument {
  if (!current || incoming.revision >= current.revision) return incoming;
  return current;
}

export function mergeCanvasPatchResult(
  current: CanvasDocument | undefined,
  incoming: CanvasDocument,
  input: { title?: string; description?: string },
): CanvasDocument {
  const merged = mergeCanvasDocumentByRevision(current, incoming);
  if (!current || merged === incoming) return merged;
  return {
    ...current,
    ...input,
    updated_at:
      incoming.updated_at > current.updated_at
        ? incoming.updated_at
        : current.updated_at,
  };
}
