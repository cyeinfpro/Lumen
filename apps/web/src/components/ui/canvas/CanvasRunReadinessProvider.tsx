"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import type { CanvasDocument } from "@/lib/canvas/types";
import { fetchVideoOptions } from "@/lib/video/requestLifecycle";
import {
  canvasActiveNodeIds,
  canvasUncertainNodeIds,
  canvasRunScopeMatches,
  createCanvasRunSnapshot,
  loadCanvasRunDiagnostics,
  projectCanvasRunDisabledReasons,
} from "./canvasRunReadiness";
import { useCanvasStore } from "./CanvasStoreProvider";

const PREFLIGHT_DEBOUNCE_MS = 250;

type CanvasRunReadiness = ReturnType<typeof useReadinessState>;
const CanvasRunReadinessContext = createContext<CanvasRunReadiness | null>(null);

export function CanvasRunReadinessProvider({
  document,
  children,
}: {
  document: CanvasDocument;
  children: ReactNode;
}) {
  const value = useReadinessState(document);
  return (
    <CanvasRunReadinessContext.Provider value={value}>
      {children}
    </CanvasRunReadinessContext.Provider>
  );
}

export function useCanvasRunReadiness() {
  const value = useContext(CanvasRunReadinessContext);
  if (!value) throw new Error("CanvasRunReadinessProvider is missing");
  return value;
}

function useReadinessState(document: CanvasDocument) {
  const queryClient = useQueryClient();
  const graph = useCanvasStore((state) => state.graph);
  const revision = useCanvasStore((state) => state.revision);
  const saveState = useCanvasStore((state) => state.saveState);
  const snapshot = useMemo(
    () => createCanvasRunSnapshot(document.id, revision, graph),
    [document.id, graph, revision],
  );
  const scopeKey = JSON.stringify([
    snapshot.workflowId, snapshot.revision, snapshot.graphKey,
  ]);
  const [settledScopeKey, setSettledScopeKey] = useState<string | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(
      () => setSettledScopeKey(scopeKey),
      PREFLIGHT_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [scopeKey]);

  const query = useQuery({
    queryKey: ["canvas-run-readiness", document.id, revision, snapshot.graphKey],
    enabled: settledScopeKey === scopeKey,
    queryFn: ({ signal }) => loadCanvasRunDiagnostics(
      snapshot,
      () => queryClient.fetchQuery({
        queryKey: ["video-options"],
        queryFn: ({ signal: optionsSignal }) => fetchPreflightVideoOptions(optionsSignal),
        staleTime: 60_000,
        retry: false,
      }),
      signal,
    ),
    staleTime: 30_000,
    // Keep the current revision cached, without retaining every typed draft.
    gcTime: 0,
    retry: 1,
  });
  const [submittingNodeIds, setSubmittingNodeIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const setNodeSubmitting = useCallback((nodeId: string, submitting: boolean) => {
    setSubmittingNodeIds((current) => {
      const next = new Set(current);
      if (submitting) next.add(nodeId);
      else next.delete(nodeId);
      return next;
    });
  }, []);
  const runningNodeIds = useMemo(
    () => new Set([
      ...submittingNodeIds,
      ...canvasActiveNodeIds(document),
    ]),
    [document, submittingNodeIds],
  );
  const uncertainNodeIds = useMemo(() => canvasUncertainNodeIds(document), [document]);
  const disabledReasons = useMemo(
    () => projectCanvasRunDisabledReasons({
      snapshot,
      diagnostics: query.data,
      failed: query.isError,
      runningNodeIds,
      uncertainNodeIds,
      saveState,
    }),
    [query.data, query.isError, runningNodeIds, uncertainNodeIds, saveState, snapshot],
  );
  const current = canvasRunScopeMatches(snapshot, query.data);
  return {
    disabledReasons,
    runningNodeIds,
    setNodeSubmitting,
    videoOptions: current ? query.data?.videoOptions : undefined,
    videoOptionsLoading: snapshot.videoNodeIds.size > 0 && !current && !query.isError,
    videoOptionsError: query.isError
      ? "视频能力检查暂不可用，可重试，或运行时重新检查"
      : null,
    videoOptionsRetrying: query.isFetching && query.isError,
    retry: () => query.refetch(),
  };
}

async function fetchPreflightVideoOptions(signal: AbortSignal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  const timeout = window.setTimeout(abort, 10_000);
  try {
    return await fetchVideoOptions(controller.signal);
  } finally {
    signal.removeEventListener("abort", abort);
    window.clearTimeout(timeout);
  }
}
