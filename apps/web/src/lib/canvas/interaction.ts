import type { CanvasNodeType, CanvasPosition } from "./types";

export interface CanvasNodePositionChange {
  id: string;
  position?: CanvasPosition;
  dragging?: boolean;
}

export interface CanvasNodePositionChanges {
  transient: Array<{ nodeId: string; position: CanvasPosition }>;
  settled: Array<{ nodeId: string; position: CanvasPosition }>;
}

export type CanvasTransientPositions = Record<string, CanvasPosition>;

export function blurActiveCanvasEditor(root: Document = document): void {
  const active = root.activeElement;
  if (
    active instanceof HTMLElement &&
    active.matches("[data-canvas-inline-editor]")
  ) {
    active.blur();
  }
}

export function splitCanvasNodePositionChanges(
  changes: readonly CanvasNodePositionChange[],
): CanvasNodePositionChanges {
  const transient: CanvasNodePositionChanges["transient"] = [];
  const settled: CanvasNodePositionChanges["settled"] = [];
  for (const change of changes) {
    if (!change.position) continue;
    const item = { nodeId: change.id, position: change.position };
    if (change.dragging === true) transient.push(item);
    else settled.push(item);
  }
  return { transient, settled };
}

export function updateCanvasTransientPositions(
  current: CanvasTransientPositions,
  transient: readonly { nodeId: string; position: CanvasPosition }[],
  clearedNodeIds: readonly string[],
): CanvasTransientPositions {
  let next = current;
  const writable = () => {
    if (next === current) next = { ...current };
    return next;
  };
  for (const item of transient) {
    const previous = next[item.nodeId];
    if (
      previous?.x === item.position.x &&
      previous?.y === item.position.y
    ) {
      continue;
    }
    writable()[item.nodeId] = item.position;
  }
  for (const nodeId of clearedNodeIds) {
    if (!(nodeId in next)) continue;
    delete writable()[nodeId];
  }
  return next;
}

export function canvasNodeZIndex(type: CanvasNodeType): number {
  return type === "frame" ? 0 : 1;
}

export function centeredCanvasNodePosition({
  center,
  width,
  height,
  offset = 0,
}: {
  center: CanvasPosition;
  width: number;
  height: number;
  offset?: number;
}): CanvasPosition {
  return {
    x: center.x - width / 2 + offset,
    y: center.y - height / 2 + offset,
  };
}
