import { useEffect, useRef } from "react";
import {
  canApplyCanvasAutoFit, nextCanvasAutoFitRequest,
  type CanvasAutoFitRequest,
} from "@/lib/canvas/autoFit";
import type { CanvasViewportApi } from "./CanvasViewportTypes";

interface Options {
  canvasId: string;
  viewportApi: CanvasViewportApi | null;
  nodeCount: number;
  fullscreen: boolean;
  compact: boolean;
  activeInteractionCount: number;
  getState: () => { activeInteractionCount: number };
}

export function useCanvasAutoFit({
  canvasId, viewportApi, nodeCount, fullscreen, compact,
  activeInteractionCount, getState,
}: Options): void {
  const requestRef = useRef<CanvasAutoFitRequest | null>(null);
  useEffect(() => {
    const request = nextCanvasAutoFitRequest(requestRef.current, {
      canvasId, viewportToken: viewportApi, nodeCount, fullscreen, compact,
    });
    requestRef.current = request;
    if (!viewportApi || !canApplyCanvasAutoFit(request, activeInteractionCount)) return;
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        // Check the live store too: a gesture may begin before React's effect
        // cleanup sees its state update. Keep that real layout request pending.
        if (requestRef.current !== request
          || !canApplyCanvasAutoFit(request, getState().activeInteractionCount)) return;
        request.completed = true;
        viewportApi.fitView();
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [activeInteractionCount, canvasId, compact, fullscreen, getState, nodeCount, viewportApi]);
}
