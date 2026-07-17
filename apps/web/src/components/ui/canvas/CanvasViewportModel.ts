import {
  type AriaLabelConfig,
  type Edge,
  type ReactFlowInstance,
} from "@xyflow/react";

import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import { deliveryOutputsForNode } from "@/lib/canvas/runtime";
import type {
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasOutput,
  CanvasPosition,
  CanvasSize,
  CanvasToolMode,
} from "@/lib/canvas/types";
import { DURATION } from "@/lib/motion";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";

const DESKTOP_MIN_ZOOM = 0.15;
const COMPACT_MIN_ZOOM = 0.08;
const VIEWPORT_ANIMATION_DURATION = Math.round(DURATION.panel * 1_000);

export const CANVAS_MAX_ZOOM = 2.4;

export interface CanvasViewportPreferences {
  isMobile: boolean;
  reducedMotion: boolean;
  selectedNodeIds: readonly string[];
}

export interface CanvasNodeGeometry {
  position: CanvasPosition;
  size: CanvasSize;
}

export interface CompatibleTarget {
  key: string;
  nodeId: string;
  nodeTitle: string;
  nodeType: string;
  handleId: string;
  handleLabel: string;
  x: number;
  y: number;
}

export interface ConnectionCompatibility {
  handlesByNode: Map<string, string[]>;
  targets: CompatibleTarget[];
}

export const CANVAS_ARIA_LABEL_CONFIG = {
  "node.a11yDescription.default": "按回车键选择节点，使用方向键移动节点。",
  "node.a11yDescription.keyboardDisabled": "这是画布中的一个节点。",
  "node.a11yDescription.ariaLiveMessage": ({ direction, x, y }) =>
    `节点已向${canvasDirectionLabel(direction)}移动，当前位置横坐标 ${Math.round(x)}，纵坐标 ${Math.round(y)}。`,
  "edge.a11yDescription.default":
    "按回车键选择连接，按退格键或删除键移除连接。",
  "controls.ariaLabel": "画布视图控制",
  "controls.zoomIn.ariaLabel": "放大画布",
  "controls.zoomOut.ariaLabel": "缩小画布",
  "controls.fitView.ariaLabel": "适应全部节点",
  "controls.interactive.ariaLabel": "切换画布交互",
  "minimap.ariaLabel": "画布缩略导航",
  "handle.ariaLabel": "节点连接端口",
} satisfies Partial<AriaLabelConfig>;

export function viewportAnimationDuration(
  reducedMotion: boolean,
  duration = VIEWPORT_ANIMATION_DURATION,
): number {
  return reducedMotion ? 0 : duration;
}

export function shouldShowMiniMap(
  isMobile: boolean,
  miniMapVisible: boolean,
  nodeCount: number,
): boolean {
  return !isMobile && miniMapVisible && nodeCount > 0;
}

export function canvasPanOnDrag(
  isMobile: boolean,
  toolMode: CanvasToolMode,
): boolean | number[] {
  return isMobile ? toolMode === "hand" : [1];
}

export function canvasNodesConnectable(
  isMobile: boolean,
  toolMode: CanvasToolMode,
): boolean {
  return !isMobile || toolMode === "connect";
}

export function canvasClickConnectionEnabled(
  isMobile: boolean,
  toolMode: CanvasToolMode,
): boolean {
  return isMobile ? toolMode === "connect" : toolMode === "select";
}

export function canvasGridGap(gridSize: number): number {
  return gridSize;
}

export function fitCanvasViewport(
  instance: ReactFlowInstance<CanvasFlowNode, Edge>,
  preferences: CanvasViewportPreferences,
  nodes = instance.getNodes(),
  padding = 0.18,
  maxZoom = 1.12,
  duration = VIEWPORT_ANIMATION_DURATION,
) {
  if (nodes.length === 0) {
    void instance.zoomTo(1, {
      duration: viewportAnimationDuration(preferences.reducedMotion, duration),
    });
    return;
  }
  void instance.fitView({
    nodes,
    padding,
    minZoom: preferences.isMobile ? COMPACT_MIN_ZOOM : DESKTOP_MIN_ZOOM,
    maxZoom,
    duration: viewportAnimationDuration(preferences.reducedMotion, duration),
  });
}

export function focusCanvasNode(
  instance: ReactFlowInstance<CanvasFlowNode, Edge>,
  nodeId: string,
  preferences: CanvasViewportPreferences,
) {
  const node = instance.getInternalNode(nodeId);
  if (!node) return;
  const dimensions = canvasFlowNodeDimensions(node.data.definition);
  const width =
    node.measured.width ?? node.width ?? node.initialWidth ?? dimensions.width;
  const height =
    node.measured.height ??
    node.height ??
    node.initialHeight ??
    dimensions.height;
  const minimumFocusZoom = preferences.isMobile ? 1 : 0.9;
  void instance.setCenter(
    node.internals.positionAbsolute.x + width / 2,
    node.internals.positionAbsolute.y + height / 2,
    {
      zoom: Math.min(1.15, Math.max(instance.getZoom(), minimumFocusZoom)),
      duration: viewportAnimationDuration(preferences.reducedMotion),
    },
  );
}

export function flowViewportBounds(
  viewport: HTMLDivElement | null,
): DOMRect | null {
  const flow = viewport?.querySelector<HTMLElement>(".react-flow");
  return (
    flow?.getBoundingClientRect() ?? viewport?.getBoundingClientRect() ?? null
  );
}

export function pointerClientPosition(
  event: MouseEvent | TouchEvent,
): { x: number; y: number } | null {
  if ("changedTouches" in event) {
    const touch = event.changedTouches[0] ?? event.touches[0];
    return touch ? { x: touch.clientX, y: touch.clientY } : null;
  }
  return { x: event.clientX, y: event.clientY };
}

export function canvasEdgeAriaLabel(
  nodesById: Map<string, CanvasNodeDefinition>,
  edge: CanvasGraph["edges"][number],
): string {
  const sourceNode = nodesById.get(edge.source_node_id);
  const targetNode = nodesById.get(edge.target_node_id);
  const sourcePort = sourceNode
    ? CANVAS_NODE_SPECS[sourceNode.type].outputs.find(
        (port) => port.id === edge.source_handle,
      )
    : null;
  const targetPort = targetNode
    ? CANVAS_NODE_SPECS[targetNode.type].inputs.find(
        (port) => port.id === edge.target_handle,
      )
    : null;
  const sourceLabel = sourceNode
    ? `${sourceNode.title}（${CANVAS_NODE_SPECS[sourceNode.type].label}）`
    : "未知来源节点";
  const targetLabel = targetNode
    ? `${targetNode.title}（${CANVAS_NODE_SPECS[targetNode.type].label}）`
    : "未知目标节点";
  return `${sourceLabel}的输出端口“${sourcePort?.label ?? edge.source_handle}”连接到${targetLabel}的输入端口“${targetPort?.label ?? edge.target_handle}”`;
}

export function canvasFlowNodeDimensions(node: CanvasNodeDefinition) {
  const width = node.size?.width ?? CANVAS_NODE_SPECS[node.type].width;
  const height = node.size?.height ?? (node.type === "frame" ? 220 : 180);
  const collapsedFrameHeight =
    node.type === "frame" && node.ui?.collapsed === true ? 44 : height;
  return {
    width,
    height: collapsedFrameHeight,
    styleHeight: node.type === "frame" ? collapsedFrameHeight : undefined,
  };
}

export function canvasNodeDeliveryOutputs(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
  activeOutputs: Map<string, CanvasOutput>,
  recentExecutions: CanvasNodeExecution[],
): CanvasOutput[] {
  if (node.type !== "delivery") return [];
  return deliveryOutputsForNode(
    graph,
    node.id,
    activeOutputs,
    recentExecutions,
  );
}

export function omitCanvasNodeMeasurements(
  current: Record<string, { width: number; height: number }>,
  nodeIds: readonly string[],
) {
  let next = current;
  for (const nodeId of nodeIds) {
    if (!(nodeId in next)) continue;
    if (next === current) next = { ...current };
    delete next[nodeId];
  }
  return next;
}

function canvasDirectionLabel(direction: string): string {
  return (
    {
      left: "左",
      right: "右",
      up: "上",
      down: "下",
    }[direction] ?? direction
  );
}
