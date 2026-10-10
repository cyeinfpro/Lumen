// Only a completed, stationary primary touch may correct a compatibility click.
// These are gesture association bounds, not performance acceptance thresholds.
const MAX_TAP_MS = 700;
const MAX_COMPATIBILITY_CLICK_MS = 500;
const MAX_TAP_MOVEMENT_PX = 8;
const MAX_RETARGET_DISTANCE_PX = 24;

export interface CanvasTouchPoint {
  pointerId: number;
  x: number;
  y: number;
  now: number;
}
interface TouchIntent extends CanvasTouchPoint {
  nodeId: string;
  endedAt: number | null;
}
function distance(a: { x: number; y: number }, b: { x: number; y: number }) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}
function validPoint(point: { x: number; y: number; now: number }) {
  return [point.x, point.y, point.now].every(Number.isFinite);
}

/** Per-viewport state; contains no DOM references, timers or subscriptions. */
export class CanvasTouchPreviewIntent {
  private pointers = new Set<number>();
  private intent: TouchIntent | null = null;

  down(point: CanvasTouchPoint & { pointerType: string; isPrimary: boolean; nodeId: string | null }) {
    if (point.pointerType !== "touch") {
      this.clear();
      return;
    }
    this.pointers.add(point.pointerId);
    this.intent = this.pointers.size === 1 && point.isPrimary && point.nodeId && validPoint(point)
      ? { ...point, nodeId: point.nodeId, endedAt: null } : null;
  }

  move(point: CanvasTouchPoint) {
    if (this.intent?.pointerId !== point.pointerId) return;
    if (!validPoint(point) || distance(this.intent, point) > MAX_TAP_MOVEMENT_PX) this.intent = null;
  }

  up(point: CanvasTouchPoint) {
    this.pointers.delete(point.pointerId);
    const intent = this.intent;
    if (!intent || intent.pointerId !== point.pointerId) return;
    if (!validPoint(point) || distance(intent, point) > MAX_TAP_MOVEMENT_PX
      || point.now < intent.now || point.now - intent.now > MAX_TAP_MS
      || this.pointers.size !== 0) {
      this.intent = null;
      return;
    }
    this.intent = { ...intent, x: point.x, y: point.y, endedAt: point.now };
  }

  clear() {
    this.pointers.clear();
    this.intent = null;
  }

  consume(click: { x: number; y: number; now: number; detail: number; preview: boolean }): string | null {
    const intent = this.intent;
    this.intent = null;
    if (!intent || intent.endedAt === null || this.pointers.size !== 0
      || !click.preview || click.detail <= 0 || !validPoint(click)
      || click.now < intent.endedAt || click.now - intent.endedAt > MAX_COMPATIBILITY_CLICK_MS
      || distance(intent, click) > MAX_RETARGET_DISTANCE_PX) return null;
    return intent.nodeId;
  }
}
