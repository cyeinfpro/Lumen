"use client";
import { useEffect, useLayoutEffect, useRef } from "react";
import { getCanvasPlanRun } from "@/lib/api/canvasPlans";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import type { CanvasDocument } from "@/lib/canvas/types";
import type { CanvasPlanRunDetail } from "@/lib/canvas/runPlanTypes";

// Piggyback the shared Canvas SSE/snapshot stream, never create a second stream
// or hidden-tab polling loop. Coalesce bursts before loading the selected run.
export function useCanvasPlanRunRefresh(canvasId: string, runId: string | undefined, document: CanvasDocument,
  enabled: boolean, onRun: (run: CanvasPlanRunDetail) => void) {
  const callback = useRef(onRun);
  useLayoutEffect(() => { callback.current = onRun; }, [onRun]);
  const stamp = JSON.stringify([
    document.active_runs.map(run => [run.id, run.last_event_seq, run.status]),
    document.recent_executions.filter(execution => execution.run_id === runId).map(execution =>
      [execution.id, execution.status, execution.updated_at]),
  ]);
  useEffect(() => {
    if (!enabled || !runId || window.document.visibilityState !== "visible") return;
    const controller = new AbortController(), identity = getPrivateIdentitySnapshot();
    const timer = window.setTimeout(() => {
      void getCanvasPlanRun(canvasId, runId, controller.signal).then(run => {
        if (!controller.signal.aborted && isPrivateIdentitySnapshotCurrent(identity)) callback.current(run);
      }).catch(() => { /* The explicit query button remains available. */ });
    }, 100);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [canvasId, runId, stamp, enabled]);
}
