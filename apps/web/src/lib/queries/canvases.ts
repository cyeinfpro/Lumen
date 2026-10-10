"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { useRef } from "react";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { hasPreparingCanvasAssets } from "@/lib/canvas/assets";
import { useUserQueryScope } from "./userScope";

import {
  createCanvas,
  deleteCanvas,
  duplicateCanvas,
  executeCanvasNode,
  getCanvas,
  listCanvases,
  patchCanvas,
  selectCanvasExecutionOutput,
  type CreateCanvasInput,
  type ListCanvasesOptions,
} from "@/lib/api/canvases";
import {
  mergeCanvasDocumentByRevision,
  mergeCanvasPatchResult,
} from "@/lib/canvas/documentMerge";
import type {
  CanvasDocument,
  CanvasNodeExecution,
  CanvasNodeSelection,
  CanvasRun,
} from "@/lib/canvas/types";
import { createBroadcastChannel } from "@/shared/realtime/browser";

export const canvasQueryKeys = {
  all: ["canvas"] as const,
  list: (options: ListCanvasesOptions) => ["canvas", "list", options] as const,
  detail: (id: string) => ["canvas", "detail", id] as const,
};

export function useCanvasesQuery(options: ListCanvasesOptions = {}) {
  return useQuery({
    queryKey: canvasQueryKeys.list(options),
    queryFn: () => listCanvases(options),
  });
}

export function useCanvasQuery(canvasId: string) {
  const client = useQueryClient();
  const userScope = useUserQueryScope();
  const queryKey = canvasQueryKeys.detail(canvasId);
  const requestGeneration = useRef(0);
  return useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const identity = getPrivateIdentitySnapshot();
      // A cold route must wait for the existing /auth/me bootstrap. Starting
      // before identity activation would turn its first valid response stale.
      if (!identity.userId) throw new DOMException("Canvas identity is not ready", "AbortError");
      const generation = ++requestGeneration.current;
      const incoming = await getCanvas(canvasId, signal);
      signal.throwIfAborted();
      if (generation !== requestGeneration.current || !isPrivateIdentitySnapshotCurrent(identity)) {
        throw new DOMException("Stale canvas snapshot", "AbortError");
      }
      return mergeCanvasDocumentByRevision(
        client.getQueryData<CanvasDocument>(queryKey), incoming,
      );
    },
    enabled: Boolean(canvasId) && userScope.enabled,
    refetchInterval(query) {
      const data = query.state.data;
      const hasActiveRun = data?.active_runs.some((run) =>
        ["planning", "queued", "running", "reconciling", "canceling"].includes(
          run.status,
        ),
      );
      const hasActiveExecution = data?.recent_executions.some((execution) =>
        ["pending", "ready", "queued", "running", "reconciling", "canceling"].includes(
          execution.status,
        ),
      );
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return false;
      return hasActiveRun || hasActiveExecution ? 2000
        : hasPreparingCanvasAssets(data?.assets) ? 4000 : false;
    },
  });
}

export function useCreateCanvasMutation(
  options?: UseMutationOptions<CanvasDocument, Error, CreateCanvasInput>,
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createCanvas,
    ...options,
    onSuccess(data, variables, context, mutation) {
      void client.invalidateQueries({ queryKey: canvasQueryKeys.all });
      options?.onSuccess?.(data, variables, context, mutation);
    },
  });
}

export function usePatchCanvasMutation(canvasId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { title?: string; description?: string }) =>
      patchCanvas(canvasId, input),
    onSuccess(data, input) {
      client.setQueryData<CanvasDocument>(
        canvasQueryKeys.detail(canvasId),
        (current) => mergeCanvasPatchResult(current, data, input),
      );
      void client.invalidateQueries({ queryKey: canvasQueryKeys.all });
    },
  });
}

export function useDeleteCanvasMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: deleteCanvas,
    onSuccess() {
      void client.invalidateQueries({ queryKey: canvasQueryKeys.all });
    },
  });
}

export function useDuplicateCanvasMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: duplicateCanvas,
    onSuccess() {
      void client.invalidateQueries({ queryKey: canvasQueryKeys.all });
    },
  });
}

export function useExecuteCanvasNodeMutation(canvasId: string) {
  const client = useQueryClient();
  return useMutation<
    { run?: CanvasRun; execution?: CanvasNodeExecution },
    Error,
    { nodeId: string; revision: number }
  >({
    mutationFn: ({ nodeId, revision }) =>
      executeCanvasNode(canvasId, nodeId, revision),
    onSettled() {
      void client.invalidateQueries({ queryKey: canvasQueryKeys.detail(canvasId) });
    },
  });
}

export function useSelectCanvasOutputMutation(canvasId: string) {
  const client = useQueryClient();
  const queueRef = useRef(new Map<string, Promise<void>>());
  const revisionRef = useRef(new Map<string, number>());
  const acknowledgedIdentityRef = useRef(new WeakMap<CanvasNodeSelection, ReturnType<typeof getPrivateIdentitySnapshot>>());
  return useMutation<
    CanvasNodeSelection,
    Error,
    {
      nodeId: string;
      executionId: string;
      outputIndex: number;
      selectionRevision?: number;
    }
  >({
    mutationFn: async ({
      nodeId,
      executionId,
      outputIndex,
      selectionRevision,
    }) => {
      const identity = getPrivateIdentitySnapshot();
      if (!identity.userId) throw new DOMException("Canvas identity is not ready", "AbortError");
      const queueKey = JSON.stringify([identity.userId, identity.epoch, canvasId, nodeId || executionId]);
      const previous = queueRef.current.get(queueKey) ?? Promise.resolve();
      const task = previous.catch(() => undefined).then(async () => {
        if (!isPrivateIdentitySnapshotCurrent(identity)) throw new DOMException("Stale selection identity", "AbortError");
        const requestedRevision = normalizeCanvasSelectionRevision(
          selectionRevision,
        );
        const knownRevision = revisionRef.current.get(queueKey) ?? 0;
        const revision = Math.max(knownRevision, requestedRevision);
        try {
          const selection = await selectCanvasExecutionOutput(
            canvasId,
            executionId,
            outputIndex,
            revision,
          );
          if (!isPrivateIdentitySnapshotCurrent(identity)) throw new DOMException("Stale selection identity", "AbortError");
          acknowledgedIdentityRef.current.set(selection, identity);
          revisionRef.current.set(
            queueKey,
            selection.revision ?? revision + 1,
          );
          return selection;
        } catch (error) {
          revisionRef.current.delete(queueKey);
          throw error;
        }
      });
      const tail = task.then(
        () => undefined,
        () => undefined,
      );
      queueRef.current.set(queueKey, tail);
      try {
        return await task;
      } finally {
        if (queueRef.current.get(queueKey) === tail) {
          queueRef.current.delete(queueKey);
        }
      }
    },
    onSuccess(selection) {
      const identity = acknowledgedIdentityRef.current.get(selection);
      if (!identity || !isPrivateIdentitySnapshotCurrent(identity)) return;
      // Selection changes invalidate saved input freshness before the next GET.
      // Keep task status, billing, graph and selection CAS behavior untouched.
      client.setQueryData<CanvasDocument>(canvasQueryKeys.detail(canvasId), (current) => current ? {
        ...current, execution_freshness: undefined, stale_node_ids: undefined,
      } : current);
      if (typeof BroadcastChannel !== "undefined") {
        try {
          const channel = createBroadcastChannel(`lumen:canvas:${canvasId}`);
          channel.postMessage({
            type: "canvas.selection.changed",
            revision: selection.revision,
          });
          channel.close();
        } catch {
          // Query invalidation below still refreshes this tab.
        }
      }
    },
    onSettled() {
      void client.invalidateQueries({ queryKey: canvasQueryKeys.detail(canvasId) });
    },
  });
}

function normalizeCanvasSelectionRevision(value: number | undefined): number {
  return typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0
    ? value
    : 0;
}
