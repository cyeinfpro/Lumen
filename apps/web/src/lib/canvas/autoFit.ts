export interface CanvasAutoFitTrigger {
  canvasId: string;
  viewportToken: object | null;
  nodeCount: number;
  fullscreen: boolean;
  compact: boolean;
}
export interface CanvasAutoFitRequest {
  trigger: CanvasAutoFitTrigger;
  completed: boolean;
}

/** Gestures only defer a real layout request; ending one is not a new request. */
export function nextCanvasAutoFitRequest(
  previous: CanvasAutoFitRequest | null,
  trigger: CanvasAutoFitTrigger,
): CanvasAutoFitRequest {
  const old = previous?.trigger;
  if (old && old.canvasId === trigger.canvasId
    && old.viewportToken === trigger.viewportToken
    && old.nodeCount === trigger.nodeCount
    && old.fullscreen === trigger.fullscreen && old.compact === trigger.compact) {
    return previous;
  }
  return { trigger, completed: false };
}

export function canApplyCanvasAutoFit(
  request: CanvasAutoFitRequest,
  activeInteractionCount: number,
): boolean {
  return !request.completed && request.trigger.viewportToken !== null
    && request.trigger.nodeCount <= 200 && activeInteractionCount === 0;
}
