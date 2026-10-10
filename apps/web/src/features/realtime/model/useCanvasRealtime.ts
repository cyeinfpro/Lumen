"use client";

import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getCanvasRunDetail, getCanvasRunEventBatch } from "@/lib/api/canvasRunUpdates";
import { CanvasRunRealtimeCoordinator, mergeCanvasRunDetail, parseCanvasRunNotice } from "@/lib/canvas/runRealtime";
import type { CanvasDocument } from "@/lib/canvas/types";
import { canvasQueryKeys } from "@/lib/queries/canvases";

export function useCanvasRealtime(isCurrent: () => boolean) {
  const client = useQueryClient();
  const coordinator = useRef<CanvasRunRealtimeCoordinator | null>(null);
  useEffect(() => {
    const runtime = new CanvasRunRealtimeCoordinator({
      isCurrent,
      current: (canvasId) => client.getQueryData<CanvasDocument>(canvasQueryKeys.detail(canvasId)),
      interested: (canvasId) => document.visibilityState === "visible" &&
        (client.getQueryCache().find({ queryKey: canvasQueryKeys.detail(canvasId), exact: true })?.getObserversCount() ?? 0) > 0,
      batch: getCanvasRunEventBatch,
      detail: getCanvasRunDetail,
      apply(canvasId, detail) {
        if (!isCurrent()) return;
        client.setQueryData<CanvasDocument>(canvasQueryKeys.detail(canvasId),
          (current) => current ? mergeCanvasRunDetail(current, detail) : current);
      },
      async snapshot(canvasId, signal) {
        signal.throwIfAborted();
        if (!isCurrent()) throw new DOMException("Stale canvas scope", "AbortError");
        // The existing query owns request/identity fencing and aborts prior reads.
        await client.refetchQueries({ queryKey: canvasQueryKeys.detail(canvasId), exact: true, type: "active" },
          { throwOnError: true, cancelRefetch: true });
        signal.throwIfAborted();
      },
    });
    coordinator.current = runtime;
    const resume = () => {
      if (document.visibilityState !== "visible" || !isCurrent()) return;
      // Hidden pages do not poll. Refresh on resume so missed notices and asset
      // preparation can recover even when the last snapshot had no active run.
      void client.invalidateQueries({ queryKey: canvasQueryKeys.all, refetchType: "active" });
    };
    document.addEventListener("visibilitychange", resume);
    return () => {
      document.removeEventListener("visibilitychange", resume);
      runtime.dispose();
      if (coordinator.current === runtime) coordinator.current = null;
    };
  }, [client, isCurrent]);
  return useCallback((payload: Record<string, unknown>) => {
    if (!isCurrent()) return;
    const notice = parseCanvasRunNotice(payload);
    if (!notice) return;
    const queryKey = canvasQueryKeys.detail(notice.canvas_id);
    const observed = (client.getQueryCache().find({ queryKey, exact: true })?.getObserversCount() ?? 0) > 0;
    if (document.visibilityState !== "visible" || !observed) {
      // Keep the cache stale without fetching background/closed canvases. The
      // existing mount refresh and resume handler fetch only when relevant.
      void client.invalidateQueries({ queryKey, exact: true, refetchType: "none" });
      return;
    }
    void coordinator.current?.receive(payload);
  }, [client, isCurrent]);
}
