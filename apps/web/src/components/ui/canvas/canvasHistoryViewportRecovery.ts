import type { Edge, ReactFlowInstance } from "@xyflow/react";
import type { MutableRefObject, RefObject } from "react";
import type { CanvasGraph } from "@/lib/canvas/types";
import { historyViewportRecoveryTargets } from "@/lib/canvas/historyViewport";
import {
  canvasFlowNodeDimensions, fitCanvasViewport, flowViewportBounds,
  type CanvasViewportPreferences,
} from "./CanvasViewportModel";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";

interface RecoveryOptions {
  instance: ReactFlowInstance<CanvasFlowNode, Edge>;
  viewportRef: RefObject<HTMLDivElement | null>;
  preferences: MutableRefObject<CanvasViewportPreferences>;
  before: CanvasGraph;
  after: CanvasGraph;
  isCurrent: () => boolean;
}

/** Repair only a newly empty geometry-history view. History clears selection,
 * which closes the inspector and legitimately changes the flow's width.
 * New graph/selection, gestures, camera movement and browser resize cancel it.
 */
export function recoverCanvasHistoryViewport({
  instance, viewportRef, preferences, before, after, isCurrent,
}: RecoveryOptions): void {
  const bounds = flowViewportBounds(viewportRef.current);
  const camera = instance.getViewport();
  if (!bounds || !Number.isFinite(camera.zoom) || camera.zoom <= 0) return;
  const flowNodes = instance.getNodes();
  // Relative-parent coordinates need a separate absolute-geometry contract.
  if (flowNodes.some((node) => node.parentId)) return;
  const rendered = new Map(flowNodes.filter((node) => !node.hidden).map((node) => [node.id, node]));
  const rectangles = (graph: CanvasGraph) => graph.nodes.flatMap((node) => {
    const flow = rendered.get(node.id);
    if (!flow) return [];
    const size = canvasFlowNodeDimensions(node);
    return [{
      id: node.id, x: node.position.x, y: node.position.y,
      width: flow.measured?.width ?? flow.width ?? size.width,
      height: flow.measured?.height ?? flow.height ?? size.height,
    }];
  });
  const beforeRects = rectangles(before), afterRects = rectangles(after);
  const targetsFor = (width: number, height: number) =>
    historyViewportRecoveryTargets(beforeRects, afterRects, {
      x: -camera.x / camera.zoom, y: -camera.y / camera.zoom,
      width: width / camera.zoom, height: height / camera.zoom,
    });
  if (targetsFor(bounds.width, bounds.height).length === 0) return;
  const browserSize = { width: window.innerWidth, height: window.innerHeight };
  const expected = new Map(after.nodes.map((node) => [node.id, node.position]));
  const deadline = performance.now() + 500;
  const attempt = () => {
    if (!viewportRef.current || !isCurrent()
      || window.innerWidth !== browserSize.width || window.innerHeight !== browserSize.height) return;
    const current = instance.getViewport();
    const currentBounds = flowViewportBounds(viewportRef.current);
    if (!currentBounds || current.x !== camera.x || current.y !== camera.y || current.zoom !== camera.zoom) return;
    const targets = targetsFor(currentBounds.width, currentBounds.height);
    if (targets.length === 0) return;
    const nodes = targets.map((id) => instance.getNode(id))
      .filter((node): node is CanvasFlowNode => Boolean(node));
    if (nodes.length !== targets.length || nodes.some((node) => {
      const position = expected.get(node.id);
      return !position || node.position.x !== position.x || node.position.y !== position.y;
    })) {
      // Controlled ReactFlow nodes may commit after the store's history update.
      if (performance.now() < deadline) window.requestAnimationFrame(attempt);
      return;
    }
    fitCanvasViewport(instance, preferences.current, nodes, 0.26, 1.2, 0);
  };
  window.requestAnimationFrame(() => window.requestAnimationFrame(attempt));
}
