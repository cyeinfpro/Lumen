"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { useRef } from "react";

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
  const queryKey = canvasQueryKeys.detail(canvasId);
  return useQuery({
    queryKey,
    queryFn: async () =>
      mergeCanvasDocumentByRevision(
        client.getQueryData<CanvasDocument>(queryKey),
        await getCanvas(canvasId),
      ),
    enabled: Boolean(canvasId),
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
      return hasActiveRun || hasActiveExecution ? 2000 : false;
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
      const queueKey = nodeId || executionId;
      const previous = queueRef.current.get(queueKey) ?? Promise.resolve();
      const task = previous.catch(() => undefined).then(async () => {
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
      if (typeof BroadcastChannel !== "undefined") {
        try {
          const channel = new BroadcastChannel(`lumen:canvas:${canvasId}`);
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
