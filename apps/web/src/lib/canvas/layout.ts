import type {
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasPosition,
  CanvasSize,
} from "./types";
import { CANVAS_NODE_SPECS } from "#canvas-registry";
import { MAX_CANVAS_COORDINATE } from "#canvas-graph";

export type CanvasAlignment =
  | "left"
  | "horizontal-center"
  | "right"
  | "top"
  | "vertical-center"
  | "bottom";
export type CanvasDistributionAxis = "horizontal" | "vertical";
export type CanvasLayoutDirection = "right" | "down";

export interface CanvasLayoutMove {
  nodeId: string;
  position: CanvasPosition;
}

export interface CanvasDagLayoutOptions {
  direction?: CanvasLayoutDirection;
  rankGap?: number;
  nodeGap?: number;
  origin?: CanvasPosition;
  defaultNodeSize?: CanvasSize;
}

export function alignNodes(
  nodes: readonly CanvasNodeDefinition[],
  alignment: CanvasAlignment,
): CanvasLayoutMove[] {
  assertUniqueNodeIds(nodes);
  if (nodes.length === 0) return [];
  const bounds = selectionBounds(nodes);
  return validateLayoutMoves(
    nodes.map((node) => ({
      nodeId: node.id,
      position: alignedPosition(node, alignment, bounds),
    })),
  );
}

export function distributeNodes(
  nodes: readonly CanvasNodeDefinition[],
  axis: CanvasDistributionAxis,
): CanvasLayoutMove[] {
  assertUniqueNodeIds(nodes);
  if (nodes.length < 3) return validateLayoutMoves(currentPositions(nodes));
  const sorted = [...nodes].sort(distributionComparator(axis));
  const positions =
    axis === "horizontal"
      ? horizontalDistribution(sorted)
      : verticalDistribution(sorted);
  return validateLayoutMoves(
    nodes.map((node) => ({
      nodeId: node.id,
      position: positions.get(node.id) ?? { ...node.position },
    })),
  );
}

export function autoLayoutDag(
  graph: CanvasGraph,
  options: CanvasDagLayoutOptions = {},
): CanvasLayoutMove[] {
  assertUniqueNodeIds(graph.nodes);
  if (graph.nodes.length === 0) return [];
  const resolved = resolveDagOptions(graph.nodes, options);
  const dag = buildDag(graph);
  const topologicalOrder = stableTopologicalOrder(
    graph.nodes,
    dag.outgoing,
    dag.indegree,
    resolved.direction,
  );
  const ranks = longestPathRanks(topologicalOrder, dag.outgoing);
  const layers = orderedLayers(
    graph.nodes,
    ranks,
    dag.predecessors,
    resolved.direction,
  );
  const positions = positionLayers(layers, resolved);
  return validateLayoutMoves(
    [...positions]
      .sort(([leftId], [rightId]) => compareStrings(leftId, rightId))
      .map(([nodeId, position]) => ({ nodeId, position })),
  );
}

export function layoutDag(
  graph: CanvasGraph,
  options: CanvasDagLayoutOptions = {},
): CanvasLayoutMove[] {
  return autoLayoutDag(graph, options);
}

interface CanvasBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
  centerX: number;
  centerY: number;
}

interface ResolvedDagLayoutOptions {
  direction: CanvasLayoutDirection;
  rankGap: number;
  nodeGap: number;
  origin: CanvasPosition;
  defaultNodeSize: CanvasSize;
}

interface DagIndex {
  outgoing: Map<string, Set<string>>;
  predecessors: Map<string, Set<string>>;
  indegree: Map<string, number>;
}

function selectionBounds(
  nodes: readonly CanvasNodeDefinition[],
): CanvasBounds {
  const left = Math.min(...nodes.map((node) => node.position.x));
  const top = Math.min(...nodes.map((node) => node.position.y));
  const right = Math.max(
    ...nodes.map((node) => node.position.x + nodeSize(node).width),
  );
  const bottom = Math.max(
    ...nodes.map((node) => node.position.y + nodeSize(node).height),
  );
  return {
    left,
    top,
    right,
    bottom,
    centerX: (left + right) / 2,
    centerY: (top + bottom) / 2,
  };
}

function alignedPosition(
  node: CanvasNodeDefinition,
  alignment: CanvasAlignment,
  bounds: CanvasBounds,
): CanvasPosition {
  const size = nodeSize(node);
  if (alignment === "left") return { x: bounds.left, y: node.position.y };
  if (alignment === "right") {
    return { x: bounds.right - size.width, y: node.position.y };
  }
  if (alignment === "horizontal-center") {
    return { x: bounds.centerX - size.width / 2, y: node.position.y };
  }
  if (alignment === "top") return { x: node.position.x, y: bounds.top };
  if (alignment === "bottom") {
    return { x: node.position.x, y: bounds.bottom - size.height };
  }
  return { x: node.position.x, y: bounds.centerY - size.height / 2 };
}

function horizontalDistribution(
  nodes: CanvasNodeDefinition[],
): Map<string, CanvasPosition> {
  const first = nodes[0];
  const last = nodes.at(-1);
  if (!first || !last) return new Map();
  const span =
    last.position.x + nodeSize(last).width - first.position.x;
  const occupied = nodes.reduce(
    (total, node) => total + nodeSize(node).width,
    0,
  );
  const gap = (span - occupied) / (nodes.length - 1);
  let x = first.position.x;
  return new Map(
    nodes.map((node) => {
      const entry = [node.id, { x, y: node.position.y }] as const;
      x += nodeSize(node).width + gap;
      return entry;
    }),
  );
}

function verticalDistribution(
  nodes: CanvasNodeDefinition[],
): Map<string, CanvasPosition> {
  const first = nodes[0];
  const last = nodes.at(-1);
  if (!first || !last) return new Map();
  const span =
    last.position.y + nodeSize(last).height - first.position.y;
  const occupied = nodes.reduce(
    (total, node) => total + nodeSize(node).height,
    0,
  );
  const gap = (span - occupied) / (nodes.length - 1);
  let y = first.position.y;
  return new Map(
    nodes.map((node) => {
      const entry = [node.id, { x: node.position.x, y }] as const;
      y += nodeSize(node).height + gap;
      return entry;
    }),
  );
}

function distributionComparator(
  axis: CanvasDistributionAxis,
): (left: CanvasNodeDefinition, right: CanvasNodeDefinition) => number {
  return (left, right) => {
    const primary =
      axis === "horizontal"
        ? left.position.x - right.position.x
        : left.position.y - right.position.y;
    if (primary !== 0) return primary;
    return compareStrings(left.id, right.id);
  };
}

function currentPositions(
  nodes: readonly CanvasNodeDefinition[],
): CanvasLayoutMove[] {
  return nodes.map((node) => ({
    nodeId: node.id,
    position: { ...node.position },
  }));
}

function resolveDagOptions(
  nodes: CanvasNodeDefinition[],
  options: CanvasDagLayoutOptions,
): ResolvedDagLayoutOptions {
  const direction = options.direction ?? "right";
  const rankGap = finiteNonNegative(options.rankGap ?? 120, "rankGap");
  const nodeGap = finiteNonNegative(options.nodeGap ?? 48, "nodeGap");
  const defaultNodeSize = {
    width: finitePositive(options.defaultNodeSize?.width ?? 280, "default width"),
    height: finitePositive(
      options.defaultNodeSize?.height ?? 180,
      "default height",
    ),
  };
  const origin = options.origin ?? {
    x: Math.min(...nodes.map((node) => node.position.x)),
    y: Math.min(...nodes.map((node) => node.position.y)),
  };
  return {
    direction,
    rankGap,
    nodeGap,
    origin: {
      x: finiteNumber(origin.x, "origin.x"),
      y: finiteNumber(origin.y, "origin.y"),
    },
    defaultNodeSize,
  };
}

function buildDag(graph: CanvasGraph): DagIndex {
  const nodeIds = new Set(graph.nodes.map((node) => node.id));
  const outgoing = new Map<string, Set<string>>();
  const predecessors = new Map<string, Set<string>>();
  const indegree = new Map<string, number>();
  for (const nodeId of nodeIds) {
    outgoing.set(nodeId, new Set());
    predecessors.set(nodeId, new Set());
    indegree.set(nodeId, 0);
  }
  for (const edge of graph.edges) {
    if (
      !nodeIds.has(edge.source_node_id) ||
      !nodeIds.has(edge.target_node_id)
    ) {
      continue;
    }
    const targets = outgoing.get(edge.source_node_id);
    if (!targets || targets.has(edge.target_node_id)) continue;
    targets.add(edge.target_node_id);
    predecessors.get(edge.target_node_id)?.add(edge.source_node_id);
    indegree.set(
      edge.target_node_id,
      (indegree.get(edge.target_node_id) ?? 0) + 1,
    );
  }
  return { outgoing, predecessors, indegree };
}

function stableTopologicalOrder(
  nodes: CanvasNodeDefinition[],
  outgoing: ReadonlyMap<string, ReadonlySet<string>>,
  initialIndegree: ReadonlyMap<string, number>,
  direction: CanvasLayoutDirection,
): string[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const indegree = new Map(initialIndegree);
  const compare = stableNodeComparator(direction);
  const ready = nodes
    .filter((node) => (indegree.get(node.id) ?? 0) === 0)
    .sort(compare);
  const result: string[] = [];
  while (ready.length > 0) {
    const node = ready.shift();
    if (!node) break;
    result.push(node.id);
    for (const targetId of [...(outgoing.get(node.id) ?? [])].sort(compareStrings)) {
      const nextDegree = (indegree.get(targetId) ?? 0) - 1;
      indegree.set(targetId, nextDegree);
      const target = nodesById.get(targetId);
      if (nextDegree === 0 && target) {
        ready.push(target);
        ready.sort(compare);
      }
    }
  }
  if (result.length !== nodes.length) {
    throw new Error("Canvas auto layout requires an acyclic graph");
  }
  return result;
}

function longestPathRanks(
  topologicalOrder: string[],
  outgoing: ReadonlyMap<string, ReadonlySet<string>>,
): Map<string, number> {
  const ranks = new Map(topologicalOrder.map((nodeId) => [nodeId, 0]));
  for (const nodeId of topologicalOrder) {
    const nextRank = (ranks.get(nodeId) ?? 0) + 1;
    for (const targetId of outgoing.get(nodeId) ?? []) {
      ranks.set(targetId, Math.max(ranks.get(targetId) ?? 0, nextRank));
    }
  }
  return ranks;
}

function orderedLayers(
  nodes: CanvasNodeDefinition[],
  ranks: ReadonlyMap<string, number>,
  predecessors: ReadonlyMap<string, ReadonlySet<string>>,
  direction: CanvasLayoutDirection,
): CanvasNodeDefinition[][] {
  const layers: CanvasNodeDefinition[][] = [];
  for (const node of nodes) {
    const rank = ranks.get(node.id) ?? 0;
    (layers[rank] ??= []).push(node);
  }
  const baseCompare = stableNodeComparator(direction);
  const order = new Map<string, number>();
  layers.forEach((layer, rank) => {
    layer.sort((left, right) => {
      const leftBarycenter = predecessorBarycenter(left.id, predecessors, order);
      const rightBarycenter = predecessorBarycenter(right.id, predecessors, order);
      if (
        rank > 0 &&
        leftBarycenter !== null &&
        rightBarycenter !== null &&
        leftBarycenter !== rightBarycenter
      ) {
        return leftBarycenter - rightBarycenter;
      }
      if (rank > 0 && leftBarycenter !== null && rightBarycenter === null) {
        return -1;
      }
      if (rank > 0 && leftBarycenter === null && rightBarycenter !== null) {
        return 1;
      }
      return baseCompare(left, right);
    });
    layer.forEach((node, index) => order.set(node.id, index));
  });
  return layers;
}

function predecessorBarycenter(
  nodeId: string,
  predecessors: ReadonlyMap<string, ReadonlySet<string>>,
  order: ReadonlyMap<string, number>,
): number | null {
  const values = [...(predecessors.get(nodeId) ?? [])]
    .map((sourceId) => order.get(sourceId))
    .filter((value): value is number => value !== undefined);
  if (values.length === 0) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function positionLayers(
  layers: CanvasNodeDefinition[][],
  options: ResolvedDagLayoutOptions,
): Map<string, CanvasPosition> {
  return options.direction === "right"
    ? positionRightwardLayers(layers, options)
    : positionDownwardLayers(layers, options);
}

function positionRightwardLayers(
  layers: CanvasNodeDefinition[][],
  options: ResolvedDagLayoutOptions,
): Map<string, CanvasPosition> {
  const positions = new Map<string, CanvasPosition>();
  const widths = layers.map((layer) =>
    Math.max(...layer.map((node) => layoutNodeSize(node, options).width)),
  );
  const heights = layers.map((layer) =>
    stackedSize(layer, options, "vertical"),
  );
  const maximumHeight = Math.max(...heights);
  let x = options.origin.x;
  layers.forEach((layer, rank) => {
    let y = options.origin.y + (maximumHeight - heights[rank]) / 2;
    for (const node of layer) {
      const size = layoutNodeSize(node, options);
      positions.set(node.id, {
        x: x + (widths[rank] - size.width) / 2,
        y,
      });
      y += size.height + options.nodeGap;
    }
    x += widths[rank] + options.rankGap;
  });
  return positions;
}

function positionDownwardLayers(
  layers: CanvasNodeDefinition[][],
  options: ResolvedDagLayoutOptions,
): Map<string, CanvasPosition> {
  const positions = new Map<string, CanvasPosition>();
  const heights = layers.map((layer) =>
    Math.max(...layer.map((node) => layoutNodeSize(node, options).height)),
  );
  const widths = layers.map((layer) =>
    stackedSize(layer, options, "horizontal"),
  );
  const maximumWidth = Math.max(...widths);
  let y = options.origin.y;
  layers.forEach((layer, rank) => {
    let x = options.origin.x + (maximumWidth - widths[rank]) / 2;
    for (const node of layer) {
      const size = layoutNodeSize(node, options);
      positions.set(node.id, {
        x,
        y: y + (heights[rank] - size.height) / 2,
      });
      x += size.width + options.nodeGap;
    }
    y += heights[rank] + options.rankGap;
  });
  return positions;
}

function stackedSize(
  layer: CanvasNodeDefinition[],
  options: ResolvedDagLayoutOptions,
  axis: CanvasDistributionAxis,
): number {
  const occupied = layer.reduce((total, node) => {
    const size = layoutNodeSize(node, options);
    return total + (axis === "horizontal" ? size.width : size.height);
  }, 0);
  return occupied + Math.max(0, layer.length - 1) * options.nodeGap;
}

function stableNodeComparator(
  direction: CanvasLayoutDirection,
): (left: CanvasNodeDefinition, right: CanvasNodeDefinition) => number {
  return (left, right) => {
    const primary =
      direction === "right"
        ? left.position.y - right.position.y
        : left.position.x - right.position.x;
    if (primary !== 0) return primary;
    const secondary =
      direction === "right"
        ? left.position.x - right.position.x
        : left.position.y - right.position.y;
    return secondary || compareStrings(left.id, right.id);
  };
}

function nodeSize(node: CanvasNodeDefinition): CanvasSize {
  return {
    width: positiveOrFallback(
      node.size?.width,
      CANVAS_NODE_SPECS[node.type].width,
    ),
    height: positiveOrFallback(
      node.size?.height,
      node.type === "frame" ? 220 : 180,
    ),
  };
}

function layoutNodeSize(
  node: CanvasNodeDefinition,
  options: ResolvedDagLayoutOptions,
): CanvasSize {
  return {
    width: positiveOrFallback(node.size?.width, options.defaultNodeSize.width),
    height: positiveOrFallback(
      node.size?.height,
      options.defaultNodeSize.height,
    ),
  };
}

function positiveOrFallback(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : fallback;
}

function finiteNonNegative(value: number, label: string): number {
  const normalized = finiteNumber(value, label);
  if (normalized < 0) throw new Error(`${label} must be non-negative`);
  return normalized;
}

function finitePositive(value: number, label: string): number {
  const normalized = finiteNumber(value, label);
  if (normalized <= 0) throw new Error(`${label} must be positive`);
  return normalized;
}

function finiteNumber(value: number, label: string): number {
  if (!Number.isFinite(value)) throw new Error(`${label} must be finite`);
  return value;
}

function assertUniqueNodeIds(
  nodes: readonly CanvasNodeDefinition[],
): void {
  const nodeIds = new Set<string>();
  for (const node of nodes) {
    if (nodeIds.has(node.id)) {
      throw new Error(`Canvas layout contains duplicate node ID: ${node.id}`);
    }
    nodeIds.add(node.id);
  }
}

function validateLayoutMoves(
  moves: CanvasLayoutMove[],
): CanvasLayoutMove[] {
  const nodeIds = new Set<string>();
  for (const move of moves) {
    if (nodeIds.has(move.nodeId)) {
      throw new Error(
        `Canvas layout produced duplicate node ID: ${move.nodeId}`,
      );
    }
    nodeIds.add(move.nodeId);
    if (
      !Number.isFinite(move.position.x) ||
      Math.abs(move.position.x) > MAX_CANVAS_COORDINATE ||
      !Number.isFinite(move.position.y) ||
      Math.abs(move.position.y) > MAX_CANVAS_COORDINATE
    ) {
      throw new Error(
        `Canvas layout position for ${move.nodeId} is outside canvas bounds`,
      );
    }
  }
  return moves;
}

function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
