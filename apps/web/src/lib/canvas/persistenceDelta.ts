import {
  canvasOperationGroupSizesAreValid,
  hasCanvasGraphEnvelopeBounds,
  isCanvasDraft,
  isFiniteNumber,
  isNonEmptyString,
  isNonNegativeInteger,
  jsonValueEqual,
  stripEmergencyFullTag,
  type CanvasDraft,
  type CanvasEmergencyDeltaDraft,
  type CanvasEmergencyDraft,
  type PersistedCanvasSaveBatch,
  type StoredCanvasEmergencyEntry,
} from "./persistenceValidation";
import type {
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasOperation,
} from "./types";

export function hasValidDraftEnvelopeForDelta(
  draft: CanvasEmergencyDraft,
): boolean {
  const validIdentity =
    isNonEmptyString(draft.canvas_id) &&
    isNonEmptyString(draft.client_id) &&
    isNonNegativeInteger(draft.base_revision);
  const validTimestamp =
    isFiniteNumber(draft.updated_at) && draft.updated_at >= 0;
  return (
    validIdentity &&
    validTimestamp &&
    Array.isArray(draft.operations) &&
    canvasOperationGroupSizesAreValid(
      draft.operation_group_sizes,
      draft.operations.length,
    ) &&
    hasCanvasGraphEnvelopeBounds(draft.graph)
  );
}

export function commonOperationPrefixLength(
  left: readonly CanvasOperation[],
  right: readonly CanvasOperation[],
): number {
  const limit = Math.min(left.length, right.length);
  let index = 0;
  while (index < limit && jsonValueEqual(left[index], right[index])) {
    index += 1;
  }
  return index;
}

export function advanceDraftWithSaveBatch(
  baseDraft: CanvasDraft,
  saveBatch: PersistedCanvasSaveBatch | null,
): CanvasDraft {
  const canAdvance =
    saveBatch !== null &&
    saveBatch.base_revision === baseDraft.base_revision &&
    saveBatch.operations.length > baseDraft.operations.length &&
    commonOperationPrefixLength(baseDraft.operations, saveBatch.operations) ===
      baseDraft.operations.length;
  if (!canAdvance || !saveBatch) return baseDraft;
  const tailOps = saveBatch.operations.slice(baseDraft.operations.length);
  const candidate: CanvasDraft = {
    ...baseDraft,
    graph: applyCanvasOperationsToGraph(baseDraft.graph, tailOps),
    operations: [...saveBatch.operations],
    operation_group_sizes: undefined,
    updated_at: Math.max(baseDraft.updated_at, saveBatch.updated_at),
  };
  return isCanvasDraft(candidate, baseDraft.canvas_id, baseDraft.client_id)
    ? candidate
    : baseDraft;
}

function applyDeltaToBaseDraft(
  idbDraft: CanvasDraft,
  batchMerged: CanvasDraft,
  delta: CanvasEmergencyDeltaDraft,
): CanvasDraft {
  const compatible =
    delta.base_revision === idbDraft.base_revision &&
    delta.prefix_op_count <= idbDraft.operations.length &&
    delta.updated_at >= batchMerged.updated_at;
  if (!compatible) return batchMerged;
  const candidate: CanvasDraft = {
    key: idbDraft.key,
    canvas_id: idbDraft.canvas_id,
    client_id: idbDraft.client_id,
    base_revision: idbDraft.base_revision,
    graph: applyCanvasOperationsToGraph(
      idbDraft.graph,
      delta.graph_delta_operations,
    ),
    operations: [
      ...idbDraft.operations.slice(0, delta.prefix_op_count),
      ...delta.delta_operations,
    ],
    ...(delta.operation_group_sizes !== undefined
      ? { operation_group_sizes: [...delta.operation_group_sizes] }
      : {}),
    updated_at: Math.max(batchMerged.updated_at, delta.updated_at),
  };
  return isCanvasDraft(candidate, idbDraft.canvas_id, idbDraft.client_id)
    ? candidate
    : batchMerged;
}

export function reconcileRecoveredCanvasDraft(
  idbDraft: CanvasDraft,
  saveBatch: PersistedCanvasSaveBatch | null,
  emergencyEntry: StoredCanvasEmergencyEntry | null,
): CanvasDraft {
  const batchMerged = advanceDraftWithSaveBatch(idbDraft, saveBatch);
  if (!emergencyEntry) return batchMerged;
  if (emergencyEntry.kind === "full") {
    return emergencyEntry.updated_at > batchMerged.updated_at
      ? { ...stripEmergencyFullTag(emergencyEntry), key: idbDraft.key }
      : batchMerged;
  }
  if (emergencyEntry.kind === "delta") {
    return applyDeltaToBaseDraft(idbDraft, batchMerged, emergencyEntry);
  }
  return batchMerged;
}

export function diffCanvasGraphsFast(
  baseGraph: CanvasGraph,
  nextGraph: CanvasGraph,
): CanvasOperation[] {
  if (Object.is(baseGraph, nextGraph)) return [];
  const operations: CanvasOperation[] = [];
  appendRemovedGraphOps(baseGraph, nextGraph, operations);
  appendChangedNodeGraphOps(baseGraph, nextGraph, operations);
  appendChangedEdgeGraphOps(baseGraph, nextGraph, operations);
  if (
    !Object.is(baseGraph.settings, nextGraph.settings) &&
    !jsonValueEqual(baseGraph.settings, nextGraph.settings)
  ) {
    operations.push({
      op: "update_document_settings",
      operation_schema_version: 1,
      settings: { ...nextGraph.settings },
    });
  }
  return operations;
}

function appendRemovedGraphOps(
  baseGraph: CanvasGraph,
  nextGraph: CanvasGraph,
  operations: CanvasOperation[],
): void {
  const nextNodeIds = new Set(nextGraph.nodes.map((node) => node.id));
  const nextEdgeIds = new Set(nextGraph.edges.map((edge) => edge.id));
  const removedNodeIds = baseGraph.nodes
    .filter((node) => !nextNodeIds.has(node.id))
    .map((node) => node.id);
  const removedNodeSet = new Set(removedNodeIds);
  const associatedEdgeIds = baseGraph.edges
    .filter(
      (edge) =>
        removedNodeSet.has(edge.source_node_id) ||
        removedNodeSet.has(edge.target_node_id),
    )
    .map((edge) => edge.id);
  const associatedEdgeSet = new Set(associatedEdgeIds);
  const removedEdgeIds = baseGraph.edges
    .filter(
      (edge) => !associatedEdgeSet.has(edge.id) && !nextEdgeIds.has(edge.id),
    )
    .map((edge) => edge.id);
  if (removedNodeIds.length > 0) {
    operations.push({
      op: "remove_nodes",
      operation_schema_version: 1,
      node_ids: removedNodeIds,
      edge_ids: associatedEdgeIds,
    });
  }
  if (removedEdgeIds.length > 0) {
    operations.push({
      op: "remove_edges",
      operation_schema_version: 1,
      edge_ids: removedEdgeIds,
    });
  }
}

function appendChangedNodeGraphOps(
  baseGraph: CanvasGraph,
  nextGraph: CanvasGraph,
  operations: CanvasOperation[],
): void {
  const baseNodes = new Map(baseGraph.nodes.map((node) => [node.id, node]));
  const movedItems: Array<{ node_id: string; x: number; y: number }> = [];
  for (const node of nextGraph.nodes) {
    const before = baseNodes.get(node.id);
    if (!before) {
      operations.push({ op: "add_node", operation_schema_version: 1, node });
      continue;
    }
    if (!Object.is(before, node)) {
      appendSingleNodeDiffOps(before, node, operations, movedItems);
    }
  }
  if (movedItems.length > 0) {
    operations.push({
      op: "move_nodes",
      operation_schema_version: 1,
      items: movedItems,
    });
  }
}

function nodeMetadataDiffers(
  before: CanvasNodeDefinition,
  after: CanvasNodeDefinition,
): boolean {
  return (
    before.title !== after.title ||
    (before.parent_group_id ?? null) !== (after.parent_group_id ?? null) ||
    !jsonValueEqual(before.ui, after.ui)
  );
}

function appendSingleNodeDiffOps(
  before: CanvasNodeDefinition,
  after: CanvasNodeDefinition,
  operations: CanvasOperation[],
  movedItems: Array<{ node_id: string; x: number; y: number }>,
): void {
  if (nodeMetadataDiffers(before, after)) {
    operations.push({
      op: "update_node_meta",
      operation_schema_version: 1,
      node_id: after.id,
      title: after.title,
      parent_group_id: after.parent_group_id ?? null,
      ui: { ...after.ui },
    });
  }
  if (
    !Object.is(before.config, after.config) &&
    !jsonValueEqual(before.config, after.config)
  ) {
    operations.push({
      op: "update_node_config",
      operation_schema_version: 1,
      node_id: after.id,
      config: after.config,
    });
  }
  if (after.size && !jsonValueEqual(before.size ?? null, after.size)) {
    operations.push({
      op: "resize_node",
      operation_schema_version: 1,
      node_id: after.id,
      size: { ...after.size },
    });
  }
  if (
    before.position.x !== after.position.x ||
    before.position.y !== after.position.y
  ) {
    movedItems.push({
      node_id: after.id,
      x: after.position.x,
      y: after.position.y,
    });
  }
}

function appendChangedEdgeGraphOps(
  baseGraph: CanvasGraph,
  nextGraph: CanvasGraph,
  operations: CanvasOperation[],
): void {
  const baseEdges = new Map(baseGraph.edges.map((edge) => [edge.id, edge]));
  for (const edge of nextGraph.edges) {
    const before = baseEdges.get(edge.id);
    if (!before) {
      operations.push({ op: "add_edge", operation_schema_version: 1, edge });
      continue;
    }
    if (Object.is(before, edge) || jsonValueEqual(before, edge)) continue;
    operations.push({
      op: "update_edge",
      operation_schema_version: 1,
      edge_id: edge.id,
      binding_mode: edge.binding_mode,
      pinned_execution_id: edge.pinned_execution_id ?? null,
      pinned_output_index: edge.pinned_output_index ?? null,
      role: edge.role ?? null,
      order: edge.order ?? null,
    });
  }
}

type EntityMutationState = {
  nodes: CanvasNodeDefinition[];
  edges: CanvasEdgeDefinition[];
  edgesTouched: boolean;
};

type EntityCanvasOp = Exclude<
  CanvasOperation,
  { op: "update_document_settings" }
>;

const CANVAS_ENTITY_OP_APPLIERS: {
  [K in EntityCanvasOp["op"]]: (
    state: EntityMutationState,
    op: Extract<EntityCanvasOp, { op: K }>,
  ) => EntityMutationState;
} = {
  add_node: (state, op) => {
    const cloned = structuredClone(op.node);
    const index = state.nodes.findIndex((item) => item.id === cloned.id);
    const nodes =
      index >= 0
        ? state.nodes.map((item, i) => (i === index ? cloned : item))
        : [...state.nodes, cloned];
    return { ...state, nodes };
  },
  update_node_config: (state, op) => ({
    ...state,
    nodes: state.nodes.map((node) =>
      node.id === op.node_id
        ? { ...node, config: structuredClone(op.config) }
        : node,
    ),
  }),
  update_node_meta: (state, op) => ({
    ...state,
    nodes: state.nodes.map((node) =>
      node.id === op.node_id ? applyNodeMetaPatch(node, op) : node,
    ),
  }),
  move_nodes: (state, op) => {
    const moves = new Map(op.items.map((item) => [item.node_id, item]));
    return {
      ...state,
      nodes: state.nodes.map((node) => {
        const move = moves.get(node.id);
        return move ? { ...node, position: { x: move.x, y: move.y } } : node;
      }),
    };
  },
  resize_node: (state, op) => ({
    ...state,
    nodes: state.nodes.map((node) =>
      node.id === op.node_id ? { ...node, size: { ...op.size } } : node,
    ),
  }),
  remove_nodes: (state, op) => {
    const removedNodes = new Set(op.node_ids);
    const removedEdges = new Set(op.edge_ids);
    return {
      nodes: state.nodes.filter((node) => !removedNodes.has(node.id)),
      edges: state.edges.filter(
        (edge) =>
          !removedEdges.has(edge.id) &&
          !removedNodes.has(edge.source_node_id) &&
          !removedNodes.has(edge.target_node_id),
      ),
      edgesTouched: true,
    };
  },
  add_edge: (state, op) => {
    const cloned = structuredClone(op.edge);
    const index = state.edges.findIndex((item) => item.id === cloned.id);
    const edges =
      index >= 0
        ? state.edges.map((item, i) => (i === index ? cloned : item))
        : [...state.edges, cloned];
    return { ...state, edges, edgesTouched: true };
  },
  update_edge: (state, op) => ({
    ...state,
    edges: state.edges.map((edge) =>
      edge.id === op.edge_id ? applyEdgePatch(edge, op) : edge,
    ),
    edgesTouched: true,
  }),
  remove_edges: (state, op) => {
    const removed = new Set(op.edge_ids);
    return {
      ...state,
      edges: state.edges.filter((edge) => !removed.has(edge.id)),
      edgesTouched: true,
    };
  },
};

export function applyCanvasOperationsToGraph(
  baseGraph: CanvasGraph,
  operations: readonly CanvasOperation[],
): CanvasGraph {
  let state: EntityMutationState = {
    nodes: baseGraph.nodes.map((node) => structuredClone(node)),
    edges: baseGraph.edges.map((edge) => structuredClone(edge)),
    edgesTouched: false,
  };
  let settings = { ...baseGraph.settings };

  for (const op of operations) {
    if (op.op === "update_document_settings") {
      settings = { ...op.settings };
      continue;
    }
    const applier = CANVAS_ENTITY_OP_APPLIERS[op.op] as (
      current: EntityMutationState,
      operation: EntityCanvasOp,
    ) => EntityMutationState;
    state = applier(state, op);
  }

  return {
    ...baseGraph,
    nodes: state.nodes,
    edges: state.edgesTouched
      ? normalizeRecoveredEdgeOrders(state.edges)
      : state.edges,
    frames: structuredClone(baseGraph.frames),
    settings,
  };
}

function applyNodeMetaPatch(
  node: CanvasNodeDefinition,
  op: Extract<CanvasOperation, { op: "update_node_meta" }>,
): CanvasNodeDefinition {
  return {
    ...node,
    ...(op.title !== undefined ? { title: op.title } : {}),
    ...(op.parent_group_id !== undefined
      ? { parent_group_id: op.parent_group_id }
      : {}),
    ...(op.ui !== undefined ? { ui: { ...node.ui, ...op.ui } } : {}),
  };
}

function applyEdgePatch(
  edge: CanvasEdgeDefinition,
  op: Extract<CanvasOperation, { op: "update_edge" }>,
): CanvasEdgeDefinition {
  return {
    ...edge,
    ...(op.binding_mode !== undefined ? { binding_mode: op.binding_mode } : {}),
    ...(op.pinned_execution_id !== undefined
      ? { pinned_execution_id: op.pinned_execution_id }
      : {}),
    ...(op.pinned_output_index !== undefined
      ? { pinned_output_index: op.pinned_output_index }
      : {}),
    ...(op.role !== undefined ? { role: op.role } : {}),
    ...(op.order !== undefined ? { order: op.order } : {}),
  };
}

function normalizeRecoveredEdgeOrders(
  edges: CanvasEdgeDefinition[],
): CanvasEdgeDefinition[] {
  const groups = new Map<string, CanvasEdgeDefinition[]>();
  for (const edge of edges) {
    const key = `${edge.target_node_id}\u0000${edge.target_handle}`;
    const existing = groups.get(key);
    if (existing) existing.push(edge);
    else groups.set(key, [edge]);
  }
  const normalizedOrderById = new Map<string, number>();
  for (const group of groups.values()) {
    if (group.length <= 1 && group[0]?.order == null) continue;
    group
      .slice()
      .sort(
        (left, right) =>
          (left.order ?? 0) - (right.order ?? 0) ||
          left.id.localeCompare(right.id),
      )
      .forEach((edge, index) => normalizedOrderById.set(edge.id, index));
  }
  return edges.map((edge) =>
    normalizedOrderById.has(edge.id)
      ? { ...edge, order: normalizedOrderById.get(edge.id)! }
      : edge,
  );
}
