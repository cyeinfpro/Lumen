import {
  MAX_CANVAS_COORDINATE,
  MAX_CANVAS_EDGES,
  MAX_CANVAS_FRAMES,
  MAX_CANVAS_NODE_CONFIG_BYTES,
  MAX_CANVAS_NODES,
  canvasGraphReadyToSave,
  canvasJsonByteLength,
  normalizeCanvasGraph,
  validateCanvasConnections,
} from "#canvas-graph";
import { isCanvasExecutableNodeType } from "#canvas-registry";
import type {
  CanvasGraph,
  CanvasOperation,
} from "./types";

export const EMERGENCY_DRAFT_STORAGE_KEY = "lumen:canvas-emergency-drafts:v1";
export const EMERGENCY_DRAFT_VERSION_V1 = 1;
export const EMERGENCY_DRAFT_VERSION_V2 = 2;
export const MAX_EMERGENCY_DRAFTS = 4;
export const MAX_EMERGENCY_POINTER_BYTES = 1024;
export const MAX_EMERGENCY_DELTA_BYTES = 64 * 1024;
export const MAX_EMERGENCY_DRAFT_LENGTH = 512 * 1024;
export const MAX_EMERGENCY_DRAFT_STORAGE_LENGTH = 2 * 1024 * 1024;

const CANVAS_EDGE_ROLES = new Set([
  "reference",
  "subject",
  "product",
  "style",
  "edit_target",
  "background",
  "other",
]);

export interface CanvasDraft {
  key: string;
  canvas_id: string;
  client_id: string;
  base_revision: number;
  graph: CanvasGraph;
  operations: CanvasOperation[];
  operation_group_sizes?: number[];
  updated_at: number;
}

export type CanvasEmergencyDraft = Omit<CanvasDraft, "key">;

export interface CanvasEmergencyPointer {
  kind: "pointer";
  canvas_id: string;
  client_id: string;
  dirty_revision: number;
  updated_at: number;
  has_pending_idb: boolean;
  op_count: number;
}

export interface CanvasEmergencyDeltaDraft {
  kind: "delta";
  canvas_id: string;
  client_id: string;
  base_revision: number;
  prefix_op_count: number;
  delta_operations: CanvasOperation[];
  graph_delta_operations: CanvasOperation[];
  operation_group_sizes?: number[];
  updated_at: number;
}

export type StoredEmergencyFullDraft = CanvasEmergencyDraft & { kind: "full" };
export type StoredCanvasEmergencyEntry =
  | CanvasEmergencyPointer
  | CanvasEmergencyDeltaDraft
  | StoredEmergencyFullDraft;

export interface PersistedCanvasSaveBatch {
  key: string;
  canvas_id: string;
  client_id: string;
  base_revision: number;
  mutation_id: string;
  operations: CanvasOperation[];
  updated_at: number;
}

export interface CanvasDraftSnapshot {
  graph: CanvasGraph;
  revision: number;
  pendingOperations: readonly CanvasOperation[];
  pendingOperationGroupSizes: readonly number[];
}

export function canvasDraftKey(canvasId: string, clientId: string): string {
  return `${canvasId}:${clientId}`;
}

export function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export function stripEmergencyFullTag(
  entry: StoredEmergencyFullDraft,
): CanvasEmergencyDraft {
  const { kind: _kind, ...rest } = entry;
  void _kind;
  return rest;
}

export function jsonValueEqual(left: unknown, right: unknown): boolean {
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

export function emergencyDraftIdentity(
  draft: Pick<CanvasEmergencyDraft, "canvas_id" | "client_id">,
): string {
  return `${draft.canvas_id}\u0000${draft.client_id}`;
}

export function readCanvasEmergencyEntries(
  storage: Storage,
): StoredCanvasEmergencyEntry[] | null {
  try {
    const raw = storage.getItem(EMERGENCY_DRAFT_STORAGE_KEY);
    if (!raw || raw.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!isRecord(parsed)) return null;
    const rawItems = extractRawEmergencyItems(parsed);
    if (!rawItems) return null;
    const validated: StoredCanvasEmergencyEntry[] = [];
    for (const item of rawItems) {
      const normalized = normalizeStoredEmergencyEntry(item);
      if (!normalized) return null;
      validated.push(normalized);
    }
    return deduplicateEmergencyEntries(validated);
  } catch {
    return null;
  }
}

function extractRawEmergencyItems(
  parsed: Record<string, unknown>,
): unknown[] | null {
  if (
    parsed.version === EMERGENCY_DRAFT_VERSION_V1 &&
    Array.isArray(parsed.drafts)
  ) {
    return parsed.drafts;
  }
  if (
    parsed.version === EMERGENCY_DRAFT_VERSION_V2 &&
    Array.isArray(parsed.entries)
  ) {
    return parsed.entries;
  }
  return null;
}

function normalizeStoredEmergencyEntry(
  value: unknown,
): StoredCanvasEmergencyEntry | null {
  if (!isRecord(value)) return null;
  if (value.kind === "pointer") {
    return isCanvasEmergencyPointer(value) &&
      utf8ByteLength(JSON.stringify(value)) < MAX_EMERGENCY_POINTER_BYTES
      ? value
      : null;
  }
  if (value.kind === "delta") {
    return isCanvasEmergencyDeltaDraft(value) &&
      utf8ByteLength(JSON.stringify(value)) <= MAX_EMERGENCY_DELTA_BYTES
      ? value
      : null;
  }
  if (value.kind === undefined || value.kind === "full") {
    const candidate =
      value.kind === "full"
        ? stripEmergencyFullTag(value as StoredEmergencyFullDraft)
        : value;
    const valid =
      isCanvasEmergencyDraft(candidate) &&
      JSON.stringify(candidate).length <= MAX_EMERGENCY_DRAFT_LENGTH;
    return valid ? { ...candidate, kind: "full" } : null;
  }
  return null;
}

function deduplicateEmergencyEntries(
  entries: StoredCanvasEmergencyEntry[],
): StoredCanvasEmergencyEntry[] {
  const unique = new Map<string, StoredCanvasEmergencyEntry>();
  const sorted = entries
    .slice()
    .sort((left, right) => right.updated_at - left.updated_at);
  for (const entry of sorted) {
    const id = emergencyDraftIdentity(entry);
    if (unique.size >= MAX_EMERGENCY_DRAFTS || unique.has(id)) continue;
    unique.set(id, entry);
  }
  return [...unique.values()];
}

export function serializeCanvasEmergencyEntries(
  entries: readonly StoredCanvasEmergencyEntry[],
): string {
  return JSON.stringify({
    version: EMERGENCY_DRAFT_VERSION_V2,
    entries,
  });
}

export function isCanvasEmergencyPointer(
  value: unknown,
): value is CanvasEmergencyPointer {
  if (!isRecord(value) || value.kind !== "pointer") return false;
  const validIdentity =
    isNonEmptyString(value.canvas_id) && isNonEmptyString(value.client_id);
  const validCounters =
    isNonNegativeInteger(value.dirty_revision) &&
    isNonNegativeInteger(value.op_count);
  return (
    validIdentity &&
    validCounters &&
    typeof value.has_pending_idb === "boolean" &&
    isFiniteNumber(value.updated_at) &&
    value.updated_at >= 0
  );
}

function isPositiveIntegerArrayOrUndefined(value: unknown): boolean {
  return (
    value === undefined ||
    (Array.isArray(value) &&
      value.every(
        (size) => Number.isSafeInteger(size) && (size as number) > 0,
      ))
  );
}

function isCanvasOperationArray(value: unknown): value is CanvasOperation[] {
  return Array.isArray(value) && value.every(isCanvasOperation);
}

export function isCanvasEmergencyDeltaDraft(
  value: unknown,
): value is CanvasEmergencyDeltaDraft {
  if (!isRecord(value) || value.kind !== "delta") return false;
  const validHeader =
    isNonEmptyString(value.canvas_id) &&
    isNonEmptyString(value.client_id) &&
    isNonNegativeInteger(value.base_revision) &&
    isNonNegativeInteger(value.prefix_op_count);
  return (
    validHeader &&
    isCanvasOperationArray(value.delta_operations) &&
    isCanvasOperationArray(value.graph_delta_operations) &&
    isPositiveIntegerArrayOrUndefined(value.operation_group_sizes) &&
    isFiniteNumber(value.updated_at) &&
    value.updated_at >= 0
  );
}

export function isCanvasEmergencyDraft(
  value: unknown,
): value is CanvasEmergencyDraft {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.canvas_id) &&
    isNonEmptyString(value.client_id) &&
    isNonNegativeInteger(value.base_revision) &&
    isCanvasGraph(value.graph) &&
    Array.isArray(value.operations) &&
    value.operations.every(isCanvasOperation) &&
    canvasOperationGroupSizesAreValid(
      value.operation_group_sizes,
      value.operations.length,
    ) &&
    isFiniteNumber(value.updated_at) &&
    value.updated_at >= 0
  );
}

export function isCanvasDraft(
  value: unknown,
  canvasId?: string,
  clientId?: string,
): value is CanvasDraft {
  if (!isRecord(value) || !isCanvasEmergencyDraft(value)) return false;
  const draft = value as CanvasEmergencyDraft & Record<string, unknown>;
  return (
    draft.key === canvasDraftKey(draft.canvas_id, draft.client_id) &&
    (canvasId === undefined || draft.canvas_id === canvasId) &&
    (clientId === undefined || draft.client_id === clientId)
  );
}

export function isCanvasSaveBatch(
  value: unknown,
  canvasId?: string,
  clientId?: string,
): value is PersistedCanvasSaveBatch {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.canvas_id) &&
    isNonEmptyString(value.client_id) &&
    value.key === canvasDraftKey(value.canvas_id, value.client_id) &&
    isNonNegativeInteger(value.base_revision) &&
    isNonEmptyString(value.mutation_id) &&
    Array.isArray(value.operations) &&
    value.operations.every(isCanvasOperation) &&
    isFiniteNumber(value.updated_at) &&
    value.updated_at >= 0 &&
    (canvasId === undefined || value.canvas_id === canvasId) &&
    (clientId === undefined || value.client_id === clientId)
  );
}

export function hasCanvasGraphEnvelopeBounds(graph: CanvasGraph): boolean {
  return (
    hasCanvasGraphShape(graph) &&
    graph.nodes.length <= MAX_CANVAS_NODES &&
    graph.edges.length <= MAX_CANVAS_EDGES &&
    graph.frames.length <= MAX_CANVAS_FRAMES
  );
}

function extractNodeConfigFromOp(
  op: CanvasOperation,
): Record<string, unknown> | null {
  if (op.op === "add_node") return op.node.config;
  if (op.op === "update_node_config") return op.config;
  return null;
}

export function deltaChangedNodesWithinConfigBudget(
  operations: readonly CanvasOperation[],
): boolean {
  for (const op of operations) {
    const config = extractNodeConfigFromOp(op);
    if (!config) continue;
    const bytes = canvasJsonByteLength(config);
    if (bytes === null || bytes > MAX_CANVAS_NODE_CONFIG_BYTES) return false;
  }
  return true;
}

function isCanvasGraph(value: unknown): value is CanvasGraph {
  if (!hasCanvasGraphShape(value)) return false;
  const normalized = normalizeCanvasGraph(value);
  return (
    normalized.nodes.length === value.nodes.length &&
    normalized.edges.length === value.edges.length &&
    canvasNodeIdsAreUnique(value) &&
    canvasEdgesHaveValidMetadata(value) &&
    canvasEdgeOrdersAreValid(value) &&
    canvasGraphReadyToSave(value) &&
    validateCanvasConnections(
      { ...value, edges: [] },
      value.edges,
      { allowLegacyCardinality: true },
    ).valid
  );
}

function hasCanvasGraphShape(value: unknown): value is CanvasGraph {
  if (!isRecord(value) || value.schema_version !== 1) return false;
  return (
    Array.isArray(value.nodes) &&
    value.nodes.every(isCanvasNode) &&
    Array.isArray(value.edges) &&
    value.edges.every(isCanvasEdge) &&
    Array.isArray(value.frames) &&
    isCanvasSettings(value.settings)
  );
}

function isCanvasNode(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.type) &&
    value.schema_version === 1 &&
    typeof value.title === "string" &&
    value.title.length <= 255 &&
    isCanvasPosition(value.position) &&
    isOptionalCanvasSize(value.size) &&
    isRecord(value.config) &&
    isCanvasNodeUi(value.ui)
  );
}

function isCanvasEdge(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.source_node_id) &&
    isNonEmptyString(value.source_handle) &&
    isNonEmptyString(value.target_node_id) &&
    isNonEmptyString(value.target_handle) &&
    isCanvasDataType(value.data_type) &&
    isCanvasBindingMode(value.binding_mode)
  );
}

export function isCanvasOperation(value: unknown): value is CanvasOperation {
  if (!isCanvasOperationRecord(value)) return false;
  return CANVAS_OPERATION_VALIDATORS[value.op]?.(value) === true;
}

export function canvasOperationGroupSizesAreValid(
  value: unknown,
  operationCount: number,
): boolean {
  return (
    value === undefined ||
    (Array.isArray(value) &&
      value.every(
        (size) => Number.isSafeInteger(size) && (size as number) > 0,
      ) &&
      value.reduce((total, size) => total + Number(size), 0) === operationCount)
  );
}

type CanvasOperationRecord = Record<string, unknown> & { op: string };
type CanvasOperationValidator = (value: CanvasOperationRecord) => boolean;

const CANVAS_OPERATION_VALIDATORS: Record<string, CanvasOperationValidator> = {
  add_node: (value) => isCanvasNode(value.node),
  update_node_config: (value) =>
    isNonEmptyString(value.node_id) && isRecord(value.config),
  update_node_meta: (value) => isNonEmptyString(value.node_id),
  move_nodes: (value) => isCanvasMoveItems(value.items),
  resize_node: (value) =>
    isNonEmptyString(value.node_id) && isCanvasSize(value.size),
  remove_nodes: (value) =>
    isStringArray(value.node_ids) && isStringArray(value.edge_ids),
  add_edge: (value) => isCanvasEdge(value.edge),
  update_edge: (value) => isNonEmptyString(value.edge_id),
  remove_edges: (value) => isStringArray(value.edge_ids),
  update_document_settings: (value) => isCanvasSettings(value.settings),
};

function isCanvasOperationRecord(
  value: unknown,
): value is CanvasOperationRecord {
  return (
    isRecord(value) &&
    value.operation_schema_version === 1 &&
    isNonEmptyString(value.op)
  );
}

function isCanvasMoveItems(value: unknown): boolean {
  return Array.isArray(value) && value.every(isCanvasMoveItem);
}

function isCanvasMoveItem(value: unknown): boolean {
  return (
    isRecord(value) &&
    isNonEmptyString(value.node_id) &&
    isFiniteNumber(value.x) &&
    isFiniteNumber(value.y)
  );
}

function isCanvasSize(
  value: unknown,
): value is { width: number; height: number } {
  return (
    isRecord(value) &&
    isFiniteNumber(value.width) &&
    isFiniteNumber(value.height)
  );
}

function isOptionalCanvasSize(value: unknown): boolean {
  return value == null || isValidCanvasSize(value);
}

function isValidCanvasSize(value: unknown): boolean {
  return (
    isCanvasSize(value) &&
    value.width >= 40 &&
    value.width <= 10_000 &&
    value.height >= 40 &&
    value.height <= 10_000
  );
}

function isCanvasPosition(value: unknown): boolean {
  return (
    isRecord(value) &&
    isFiniteNumber(value.x) &&
    Math.abs(value.x) <= MAX_CANVAS_COORDINATE &&
    isFiniteNumber(value.y) &&
    Math.abs(value.y) <= MAX_CANVAS_COORDINATE
  );
}

function isCanvasSettings(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.snap_to_grid === "boolean" &&
    Number.isSafeInteger(value.grid_size) &&
    (value.grid_size as number) >= 1 &&
    (value.grid_size as number) <= 256
  );
}

function isCanvasNodeUi(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    (value.collapsed === undefined ||
      typeof value.collapsed === "boolean") &&
    (value.color_tag == null ||
      (typeof value.color_tag === "string" &&
        value.color_tag.length <= 32)) &&
    (value.preset_id == null ||
      (typeof value.preset_id === "string" &&
        value.preset_id.length <= 128))
  );
}

function isCanvasDataType(value: unknown): boolean {
  return (
    value === "text" ||
    value === "image" ||
    value === "video" ||
    value === "mask"
  );
}

function isCanvasBindingMode(value: unknown): boolean {
  return value === "follow_active" || value === "pinned";
}

function canvasNodeIdsAreUnique(graph: CanvasGraph): boolean {
  return new Set(graph.nodes.map((node) => node.id)).size === graph.nodes.length;
}

function canvasEdgesHaveValidMetadata(graph: CanvasGraph): boolean {
  const nodeTypesById = new Map(
    graph.nodes.map((node) => [node.id, node.type]),
  );
  return graph.edges.every((edge) =>
    canvasEdgeMetadataIsValid(
      edge,
      nodeTypesById.get(edge.source_node_id),
    ),
  );
}

function canvasEdgeMetadataIsValid(
  edge: CanvasGraph["edges"][number],
  sourceType: CanvasGraph["nodes"][number]["type"] | undefined,
): boolean {
  return (
    canvasEdgeRoleIsValid(edge) &&
    canvasEdgeOrderIsValid(edge.order) &&
    canvasEdgeBindingIsValid(edge, sourceType)
  );
}

function canvasEdgeRoleIsValid(
  edge: CanvasGraph["edges"][number],
): boolean {
  return (
    edge.role == null ||
    (CANVAS_EDGE_ROLES.has(edge.role) &&
      (edge.data_type === "image" || edge.data_type === "mask"))
  );
}

function canvasEdgeOrderIsValid(order: number | null | undefined): boolean {
  return order == null || (Number.isSafeInteger(order) && order >= 0);
}

function canvasEdgeBindingIsValid(
  edge: CanvasGraph["edges"][number],
  sourceType: CanvasGraph["nodes"][number]["type"] | undefined,
): boolean {
  if (edge.binding_mode === "follow_active") {
    return (
      edge.pinned_execution_id == null &&
      edge.pinned_output_index == null
    );
  }
  return (
    sourceType !== undefined &&
    isCanvasExecutableNodeType(sourceType) &&
    typeof edge.pinned_execution_id === "string" &&
    edge.pinned_execution_id.length > 0 &&
    edge.pinned_execution_id.length <= 36 &&
    Number.isSafeInteger(edge.pinned_output_index) &&
    (edge.pinned_output_index ?? -1) >= 0
  );
}

function canvasEdgeOrdersAreValid(graph: CanvasGraph): boolean {
  const incoming = new Map<string, number[]>();
  for (const edge of graph.edges) {
    const key = `${edge.target_node_id}\u0000${edge.target_handle}`;
    const orders = incoming.get(key) ?? [];
    orders.push(edge.order ?? -1);
    incoming.set(key, orders);
  }
  return [...incoming.values()].every(
    (orders) =>
      orders.length <= 1 ||
      orders
        .slice()
        .sort((left, right) => left - right)
        .every((order, index) => order === index),
  );
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
