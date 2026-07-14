import {
  canvasGraphReadyToSave,
  cloneCanvasGraph,
  MAX_CANVAS_GRAPH_BYTES,
} from "#canvas-graph";
import { CANVAS_NODE_SPECS } from "#canvas-registry";
import type {
  CanvasDocumentSettings,
  CanvasEdgeDefinition,
  CanvasEdgeDetailsUpdate,
  CanvasEdgeRole,
  CanvasGraph,
  CanvasHistoryEntry,
  CanvasNodeAppearanceUpdate,
  CanvasNodeDefinition,
  CanvasNodeType,
  CanvasOperation,
  CanvasPosition,
  CanvasSaveState,
  CanvasSize,
} from "#canvas-types";

const HISTORY_LIMIT = 100;
const CANVAS_COORDINATE_LIMIT = 10_000_000;
export const CANVAS_HISTORY_GRAPH_BYTE_BUDGET = MAX_CANVAS_GRAPH_BYTES;
const CANVAS_EDGE_ROLES = new Set<CanvasEdgeRole>([
  "reference",
  "subject",
  "product",
  "style",
  "edit_target",
  "background",
  "other",
]);

export type MergeableCanvasHistoryEntry = CanvasHistoryEntry & {
  mergeKey?: string;
  graphBytes?: number;
};

type CanvasSaveStatus = {
  saveState: CanvasSaveState;
  saveMessage: string | null;
};

export function dirtySaveStatus(state: CanvasSaveStatus): CanvasSaveStatus {
  return state.saveState === "conflict"
    ? {
        saveState: "conflict",
        saveMessage: state.saveMessage,
      }
    : {
        saveState: "dirty",
        saveMessage: null,
      };
}

export function canvasConfigEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  return jsonValueEqual(left, right);
}

function jsonValueEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => jsonValueEqual(value, right[index]))
    );
  }
  if (
    !left ||
    !right ||
    typeof left !== "object" ||
    typeof right !== "object"
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(rightRecord, key) &&
        jsonValueEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

export function cloneCanonicalCanvasGraph(graph: CanvasGraph): CanvasGraph {
  const cloned = cloneCanvasGraph(graph);
  return {
    ...cloned,
    nodes: cloned.nodes.map((node) => ({
      ...node,
      size: canonicalCanvasSize(node.size, node.type),
    })),
  };
}

export function createHistoryEntry(
  graph: CanvasGraph,
  label: string,
  mergeKey?: string,
): MergeableCanvasHistoryEntry {
  const cloned = cloneCanvasGraph(graph);
  return {
    graph: cloned,
    label,
    mergeKey,
    graphBytes: canvasGraphByteLength(cloned),
  };
}

export function trimHistoryEntries(
  entries: CanvasHistoryEntry[],
  keep: "newest" | "oldest",
): CanvasHistoryEntry[] {
  const candidates =
    keep === "newest"
      ? [...entries].reverse().slice(0, HISTORY_LIMIT)
      : entries.slice(0, HISTORY_LIMIT);
  const retained: CanvasHistoryEntry[] = [];
  let retainedBytes = 0;
  for (const entry of candidates) {
    const mergeable = entry as MergeableCanvasHistoryEntry;
    const entryBytes =
      mergeable.graphBytes ?? canvasGraphByteLength(entry.graph);
    if (
      entryBytes > CANVAS_HISTORY_GRAPH_BYTE_BUDGET - retainedBytes
    ) {
      break;
    }
    const retainedEntry: MergeableCanvasHistoryEntry =
      mergeable.graphBytes === entryBytes
        ? mergeable
        : { ...mergeable, graphBytes: entryBytes };
    retained.push(retainedEntry);
    retainedBytes += entryBytes;
  }
  return keep === "newest" ? retained.reverse() : retained;
}

function canvasGraphByteLength(graph: CanvasGraph): number {
  try {
    return new TextEncoder().encode(JSON.stringify(graph)).byteLength;
  } catch {
    return CANVAS_HISTORY_GRAPH_BYTE_BUDGET + 1;
  }
}

export function nodeWithAppearance(
  node: CanvasNodeDefinition,
  appearance: CanvasNodeAppearanceUpdate,
): CanvasNodeDefinition {
  const title =
    hasOwn(appearance, "title") && typeof appearance.title === "string"
      ? appearance.title.trim().slice(0, 255) || node.title
      : node.title;
  const parentGroupId =
    hasOwn(appearance, "parent_group_id") &&
    appearance.parent_group_id !== undefined
      ? appearance.parent_group_id
      : node.parent_group_id;
  const ui =
    appearance.ui === undefined
      ? node.ui
      : { ...node.ui, ...appearance.ui };
  return {
    ...node,
    title,
    parent_group_id: parentGroupId,
    ui,
  };
}

export function nodeMetaOperation(
  before: CanvasNodeDefinition,
  after: CanvasNodeDefinition,
): Extract<CanvasOperation, { op: "update_node_meta" }> | null {
  const operation: Extract<CanvasOperation, { op: "update_node_meta" }> = {
    op: "update_node_meta",
    operation_schema_version: 1,
    node_id: after.id,
  };
  let changed = false;
  if (before.title !== after.title) {
    operation.title = after.title;
    changed = true;
  }
  if (
    (before.parent_group_id ?? null) !== (after.parent_group_id ?? null)
  ) {
    operation.parent_group_id = after.parent_group_id ?? null;
    changed = true;
  }
  if (!jsonValueEqual(before.ui, after.ui)) {
    operation.ui = { ...after.ui };
    changed = true;
  }
  return changed ? operation : null;
}

export function canvasSizeEqual(
  left: CanvasSize | null | undefined,
  right: CanvasSize | null | undefined,
  nodeType: CanvasNodeType,
): boolean {
  const canonicalLeft = canonicalCanvasSize(left, nodeType);
  const canonicalRight = canonicalCanvasSize(right, nodeType);
  return (
    canonicalLeft.width === canonicalRight.width &&
    canonicalLeft.height === canonicalRight.height
  );
}

export function canonicalCanvasSize(
  size: CanvasSize | null | undefined,
  nodeType: CanvasNodeType,
): CanvasSize {
  if (size) return { ...size };
  return {
    width: CANVAS_NODE_SPECS[nodeType].width,
    height: nodeType === "frame" ? 220 : 180,
  };
}

export function validCanvasSize(size: CanvasSize): boolean {
  return (
    Number.isFinite(size.width) &&
    Number.isFinite(size.height) &&
    size.width >= 40 &&
    size.width <= 10_000 &&
    size.height >= 40 &&
    size.height <= 10_000
  );
}

export function validCanvasPosition(position: CanvasPosition): boolean {
  return (
    Number.isFinite(position.x) &&
    Number.isFinite(position.y) &&
    position.x >= -CANVAS_COORDINATE_LIMIT &&
    position.x <= CANVAS_COORDINATE_LIMIT &&
    position.y >= -CANVAS_COORDINATE_LIMIT &&
    position.y <= CANVAS_COORDINATE_LIMIT
  );
}

export function validGraphForStoreCommit(graph: CanvasGraph): boolean {
  const nodeTypesById = new Map(
    graph.nodes.map((node) => [node.id, node.type]),
  );
  return (
    canvasGraphReadyToSave(graph) &&
    graph.nodes.every(
      (node) =>
        validCanvasPosition(node.position) &&
        validCanvasSize(canonicalCanvasSize(node.size, node.type)),
    ) &&
    graph.edges.every((edge) =>
      validCanvasEdgeMetadata(edge, nodeTypesById.get(edge.source_node_id)),
    )
  );
}

export function validCanvasEdgeMetadata(
  edge: CanvasEdgeDefinition,
  sourceType: CanvasNodeType | undefined,
): boolean {
  if (!validEdgeRole(edge.role) || !validEdgeOrder(edge.order)) return false;
  if (
    edge.role != null &&
    edge.data_type !== "image" &&
    edge.data_type !== "mask"
  ) {
    return false;
  }
  if (edge.binding_mode === "follow_active") {
    return (
      edge.pinned_execution_id == null &&
      edge.pinned_output_index == null
    );
  }
  return (
    edge.binding_mode === "pinned" &&
    (sourceType === "image_generate" || sourceType === "video_generate") &&
    validPinnedBinding(
      edge.pinned_execution_id,
      edge.pinned_output_index,
    )
  );
}

export function edgeWithDetails(
  edge: CanvasEdgeDefinition,
  details: CanvasEdgeDetailsUpdate,
): CanvasEdgeDefinition | null {
  const role = hasOwn(details, "role") ? details.role : edge.role;
  if (!validEdgeRole(role)) return null;
  const binding = resolveEdgeBinding(edge, details);
  if (!binding) return null;
  const order = hasOwn(details, "order") ? details.order : edge.order;
  if (!validEdgeOrder(order)) return null;
  return {
    ...edge,
    ...binding,
    role,
    order,
  };
}

function resolveEdgeBinding(
  edge: CanvasEdgeDefinition,
  details: CanvasEdgeDetailsUpdate,
): Pick<
  CanvasEdgeDefinition,
  "binding_mode" | "pinned_execution_id" | "pinned_output_index"
> | null {
  const bindingMode = hasOwn(details, "binding_mode")
    ? details.binding_mode
    : edge.binding_mode;
  if (
    bindingMode !== "follow_active" &&
    bindingMode !== "pinned"
  ) {
    return null;
  }
  const pinnedExecutionId = hasOwn(details, "pinned_execution_id")
    ? details.pinned_execution_id
    : edge.pinned_execution_id;
  const pinnedOutputIndex = hasOwn(details, "pinned_output_index")
    ? details.pinned_output_index
    : edge.pinned_output_index;
  if (bindingMode === "follow_active") {
    if (
      (hasOwn(details, "pinned_execution_id") &&
        pinnedExecutionId != null) ||
      (hasOwn(details, "pinned_output_index") &&
        pinnedOutputIndex != null)
    ) {
      return null;
    }
    return {
      binding_mode: bindingMode,
      pinned_execution_id: null,
      pinned_output_index: null,
    };
  }
  if (!validPinnedBinding(pinnedExecutionId, pinnedOutputIndex)) return null;
  return {
    binding_mode: bindingMode,
    pinned_execution_id: pinnedExecutionId,
    pinned_output_index: pinnedOutputIndex,
  };
}

function validPinnedBinding(
  executionId: string | null | undefined,
  outputIndex: number | null | undefined,
): executionId is string {
  return (
    typeof executionId === "string" &&
    executionId.length > 0 &&
    executionId.length <= 36 &&
    Number.isSafeInteger(outputIndex) &&
    (outputIndex ?? -1) >= 0
  );
}

function validEdgeRole(
  role: unknown,
): role is CanvasEdgeRole | null | undefined {
  return (
    role == null ||
    (typeof role === "string" &&
      CANVAS_EDGE_ROLES.has(role as CanvasEdgeRole))
  );
}

function validEdgeOrder(order: number | null | undefined): boolean {
  return order == null || (Number.isSafeInteger(order) && order >= 0);
}

export function canvasEdgeDetailsEqual(
  left: CanvasEdgeDefinition,
  right: CanvasEdgeDefinition,
): boolean {
  return (
    left.binding_mode === right.binding_mode &&
    (left.pinned_execution_id ?? null) ===
      (right.pinned_execution_id ?? null) &&
    (left.pinned_output_index ?? null) ===
      (right.pinned_output_index ?? null) &&
    (left.role ?? null) === (right.role ?? null) &&
    (left.order ?? null) === (right.order ?? null)
  );
}

export function edgeDetailsOperation(
  before: CanvasEdgeDefinition,
  after: CanvasEdgeDefinition,
): Extract<CanvasOperation, { op: "update_edge" }> {
  const operation = edgeBindingOperation(after);
  if ((before.role ?? null) !== (after.role ?? null)) {
    operation.role = after.role ?? null;
  }
  if ((before.order ?? null) !== (after.order ?? null)) {
    operation.order = after.order ?? null;
  }
  return operation;
}

export function validDocumentSettings(
  settings: CanvasDocumentSettings,
): boolean {
  return (
    typeof settings.snap_to_grid === "boolean" &&
    Number.isInteger(settings.grid_size) &&
    settings.grid_size >= 1 &&
    settings.grid_size <= 256
  );
}

export function documentSettingsEqual(
  left: CanvasDocumentSettings,
  right: CanvasDocumentSettings,
): boolean {
  return (
    left.snap_to_grid === right.snap_to_grid &&
    left.grid_size === right.grid_size
  );
}

function hasOwn<T extends object>(value: T, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

export function coalescePendingOperations(
  pending: CanvasOperation[],
  incoming: CanvasOperation[],
  protectedPrefixCount = 0,
): CanvasOperation[] {
  if (incoming.length !== 1 || incoming[0].op !== "update_node_config") {
    return [...pending, ...incoming];
  }
  const protectedCount = Math.min(
    Math.max(0, protectedPrefixCount),
    pending.length,
  );
  if (pending.length <= protectedCount) {
    return [...pending, incoming[0]];
  }
  const previous = pending.at(-1);
  if (
    previous?.op !== "update_node_config" ||
    previous.node_id !== incoming[0].node_id
  ) {
    return [...pending, incoming[0]];
  }
  return [...pending.slice(0, -1), incoming[0]];
}

export function operationsBetween(
  current: CanvasGraph,
  next: CanvasGraph,
): CanvasOperation[] {
  const currentNodes = new Map(current.nodes.map((node) => [node.id, node]));
  const nextNodes = new Map(next.nodes.map((node) => [node.id, node]));
  const currentEdges = new Map(current.edges.map((edge) => [edge.id, edge]));
  const nextEdgeIds = new Set(next.edges.map((edge) => edge.id));
  const removedNodeIds = current.nodes
    .filter((node) => !nextNodes.has(node.id))
    .map((node) => node.id);
  return [
    ...removedEntityOperations(current, nextEdgeIds, removedNodeIds),
    ...changedNodeOperations(currentNodes, next),
    ...changedEdgeOperations(currentEdges, next),
    ...(documentSettingsEqual(current.settings, next.settings)
      ? []
      : [
          {
            op: "update_document_settings" as const,
            operation_schema_version: 1 as const,
            settings: { ...next.settings },
          },
        ]),
  ];
}

function removedEntityOperations(
  current: CanvasGraph,
  nextEdgeIds: ReadonlySet<string>,
  removedNodeIds: string[],
): CanvasOperation[] {
  const removedNodes = new Set(removedNodeIds);
  const associatedEdgeIds = current.edges
    .filter(
      (edge) =>
        removedNodes.has(edge.source_node_id) ||
        removedNodes.has(edge.target_node_id),
    )
    .map((edge) => edge.id);
  const associatedEdges = new Set(associatedEdgeIds);
  const independentlyRemovedEdgeIds = current.edges
    .filter(
      (edge) =>
        !associatedEdges.has(edge.id) &&
        !nextEdgeIds.has(edge.id),
    )
    .map((edge) => edge.id);
  const operations: CanvasOperation[] = [];
  if (removedNodeIds.length > 0) {
    operations.push({
      op: "remove_nodes",
      operation_schema_version: 1,
      node_ids: removedNodeIds,
      edge_ids: associatedEdgeIds,
    });
  }
  if (independentlyRemovedEdgeIds.length > 0) {
    operations.push({
      op: "remove_edges",
      operation_schema_version: 1,
      edge_ids: independentlyRemovedEdgeIds,
    });
  }
  return operations;
}

function changedNodeOperations(
  currentNodes: Map<string, CanvasGraph["nodes"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  const movedItems: Extract<
    CanvasOperation,
    { op: "move_nodes" }
  >["items"] = [];
  for (const node of next.nodes) {
    const before = currentNodes.get(node.id);
    if (!before) {
      operations.push({ op: "add_node", operation_schema_version: 1, node });
      continue;
    }
    const metaOperation = nodeMetaOperation(before, node);
    if (metaOperation) operations.push(metaOperation);
    if (!canvasConfigEqual(before.config, node.config)) {
      operations.push({
        op: "update_node_config",
        operation_schema_version: 1,
        node_id: node.id,
        config: node.config,
      });
    }
    const nextSize = canonicalCanvasSize(node.size, node.type);
    if (!canvasSizeEqual(before.size, node.size, node.type)) {
      operations.push({
        op: "resize_node",
        operation_schema_version: 1,
        node_id: node.id,
        size: nextSize,
      });
    }
    if (
      validCanvasPosition(node.position) &&
      (before.position.x !== node.position.x ||
        before.position.y !== node.position.y)
    ) {
      movedItems.push({
        node_id: node.id,
        x: node.position.x,
        y: node.position.y,
      });
    }
  }
  if (movedItems.length > 0) {
    operations.push({
      op: "move_nodes",
      operation_schema_version: 1,
      items: movedItems,
    });
  }
  return operations;
}

export function uniqueExistingIds(
  ids: string[],
  existingIds: string[],
): string[] {
  const existing = new Set(existingIds);
  return [...new Set(ids)].filter((id) => existing.has(id));
}

export function sameIds(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function changedEdgeOperations(
  currentEdges: ReadonlyMap<string, CanvasGraph["edges"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  for (const edge of next.edges) {
    const before = currentEdges.get(edge.id);
    if (!before) {
      operations.push({ op: "add_edge", operation_schema_version: 1, edge });
    } else if (!canvasEdgeDetailsEqual(before, edge)) {
      operations.push(edgeDetailsOperation(before, edge));
    }
  }
  return operations;
}

function edgeBindingOperation(
  edge: CanvasGraph["edges"][number],
): Extract<CanvasOperation, { op: "update_edge" }> {
  return {
    op: "update_edge",
    operation_schema_version: 1,
    edge_id: edge.id,
    binding_mode: edge.binding_mode,
    pinned_execution_id: edge.pinned_execution_id ?? null,
    pinned_output_index: edge.pinned_output_index ?? null,
  };
}

export function normalizeEdgeOrders(graph: CanvasGraph): CanvasGraph {
  const groups = new Map<string, CanvasGraph["edges"]>();
  for (const edge of graph.edges) {
    const key = `${edge.target_node_id}\0${edge.target_handle}`;
    groups.set(key, [...(groups.get(key) ?? []), edge]);
  }
  const normalized = new Map<string, number>();
  for (const edges of groups.values()) {
    edges
      .sort(
        (left, right) =>
          (left.order ?? 0) - (right.order ?? 0) ||
          left.id.localeCompare(right.id),
      )
      .forEach((edge, order) => normalized.set(edge.id, order));
  }
  return {
    ...graph,
    edges: graph.edges.map((edge) => ({
      ...edge,
      order: normalized.get(edge.id) ?? 0,
    })),
  };
}

export function incompatibleVideoEdgeIds(
  graph: CanvasGraph,
  nodeId: string,
  mode: string,
): string[] {
  const blocked =
    mode === "t2v"
      ? new Set(["first_frame", "reference_images", "reference_videos"])
      : mode === "i2v"
        ? new Set(["reference_images", "reference_videos"])
        : new Set(["first_frame"]);
  return graph.edges
    .filter(
      (edge) =>
        edge.target_node_id === nodeId && blocked.has(edge.target_handle),
    )
    .map((edge) => edge.id);
}
