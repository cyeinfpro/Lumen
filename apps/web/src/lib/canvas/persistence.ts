import {
  MAX_CANVAS_COORDINATE,
  canvasGraphReadyToSave,
  normalizeCanvasGraph,
  validateCanvasConnections,
} from "#canvas-graph";
import { isCanvasExecutableNodeType } from "#canvas-registry";
import type { CanvasGraph, CanvasOperation } from "./types";

const DATABASE_NAME = "lumen-canvas";
const DATABASE_VERSION = 3;
const DRAFT_STORE = "drafts";
const SAVE_BATCH_STORE = "save-batches";
const CANVAS_ID_INDEX = "canvas_id";
const EMERGENCY_DRAFT_STORAGE_KEY = "lumen:canvas-emergency-drafts:v1";
const EMERGENCY_DRAFT_VERSION = 1;
const MAX_EMERGENCY_DRAFTS = 4;
const MAX_EMERGENCY_DRAFT_LENGTH = 512 * 1024;
const MAX_EMERGENCY_DRAFT_STORAGE_LENGTH = 2 * 1024 * 1024;
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

export interface PersistedCanvasSaveBatch {
  key: string;
  canvas_id: string;
  client_id: string;
  base_revision: number;
  mutation_id: string;
  operations: CanvasOperation[];
  updated_at: number;
}

export class SerialCanvasDraftWriter {
  private active: Promise<void> | null = null;
  private rerunRequested = false;
  private readonly write: () => Promise<void>;
  private readonly onError?: (error: unknown) => void;

  constructor(
    write: () => Promise<void>,
    onError?: (error: unknown) => void,
  ) {
    this.write = write;
    this.onError = onError;
  }

  request(): Promise<void> {
    this.rerunRequested = true;
    if (this.active) return this.active;
    const active = this.run().finally(() => {
      if (this.active === active) this.active = null;
    });
    this.active = active;
    return active;
  }

  private async run(): Promise<void> {
    while (this.rerunRequested) {
      this.rerunRequested = false;
      try {
        await this.write();
      } catch (error) {
        try {
          this.onError?.(error);
        } catch {
          // Error reporting must not block later draft writes.
        }
      }
    }
  }
}

export function canvasDraftKey(canvasId: string, clientId: string): string {
  return `${canvasId}:${clientId}`;
}

export function getCanvasEmergencyDraft(
  canvasId: string,
  clientId?: string,
): CanvasEmergencyDraft | null {
  try {
    const storage = localStorageOrNull();
    if (!storage) return null;
    const drafts = readCanvasEmergencyDrafts(storage);
    if (!drafts) return null;
    const canvasDrafts = drafts.filter(
      (draft) => draft.canvas_id === canvasId,
    );
    return (
      canvasDrafts.find((draft) => draft.client_id === clientId) ??
      canvasDrafts[0] ??
      null
    );
  } catch {
    return null;
  }
}

export function putCanvasEmergencyDraft(
  draft: CanvasEmergencyDraft,
): boolean {
  try {
    if (!isCanvasEmergencyDraft(draft)) return false;
    const storage = localStorageOrNull();
    if (!storage) return false;
    const serializedDraft = JSON.stringify(draft);
    if (serializedDraft.length > MAX_EMERGENCY_DRAFT_LENGTH) return false;

    const existing = readCanvasEmergencyDrafts(storage) ?? [];
    const retained = existing
      .filter(
        (entry) =>
          emergencyDraftIdentity(entry) !== emergencyDraftIdentity(draft),
      )
      .sort((left, right) => right.updated_at - left.updated_at)
      .slice(0, MAX_EMERGENCY_DRAFTS - 1);
    const drafts = [draft, ...retained];
    let serialized = serializeCanvasEmergencyDrafts(drafts);
    while (
      serialized.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH &&
      drafts.length > 1
    ) {
      drafts.pop();
      serialized = serializeCanvasEmergencyDrafts(drafts);
    }
    if (serialized.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH) return false;
    storage.setItem(EMERGENCY_DRAFT_STORAGE_KEY, serialized);
    return true;
  } catch {
    return false;
  }
}

export function deleteCanvasEmergencyDraft(
  canvasId: string,
  clientId?: string,
): void {
  try {
    const storage = localStorageOrNull();
    if (!storage) return;
    const existing = readCanvasEmergencyDrafts(storage);
    if (!existing) {
      storage.removeItem(EMERGENCY_DRAFT_STORAGE_KEY);
      return;
    }
    const retained = existing.filter(
      (draft) =>
        draft.canvas_id !== canvasId ||
        (clientId !== undefined && draft.client_id !== clientId),
    );
    if (retained.length === existing.length) return;
    if (retained.length === 0) {
      storage.removeItem(EMERGENCY_DRAFT_STORAGE_KEY);
      return;
    }
    storage.setItem(
      EMERGENCY_DRAFT_STORAGE_KEY,
      serializeCanvasEmergencyDrafts(retained),
    );
  } catch {
    // Emergency cleanup is best effort in restricted storage contexts.
  }
}

export function canvasSaveBatchMatchesPending(
  batch: Pick<PersistedCanvasSaveBatch, "base_revision" | "operations">,
  revision: number,
  pendingOperations: readonly CanvasOperation[],
): boolean {
  if (
    batch.base_revision !== revision ||
    batch.operations.length === 0 ||
    batch.operations.length > pendingOperations.length
  ) {
    return false;
  }
  return batch.operations.every((operation, index) =>
    jsonValueEqual(operation, pendingOperations[index]),
  );
}

export function isSuspiciousEmptyCanvasDraft(
  draftGraph: CanvasGraph,
  serverGraph: CanvasGraph,
  operations: readonly CanvasOperation[] = [],
): boolean {
  if (draftGraph.nodes.length > 0 || serverGraph.nodes.length === 0) {
    return false;
  }
  const explicitlyRemovedNodeIds = new Set(
    operations.flatMap((operation) =>
      operation.op === "remove_nodes" ? operation.node_ids : [],
    ),
  );
  return !serverGraph.nodes.every((node) =>
    explicitlyRemovedNodeIds.has(node.id),
  );
}

export async function getCanvasDraft(
  canvasId: string,
  clientId: string,
): Promise<CanvasDraft | null> {
  const db = await openDatabase();
  try {
    const value = await requestResult<unknown>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .get(canvasDraftKey(canvasId, clientId)),
    );
    return isCanvasDraft(value, canvasId, clientId) ? value : null;
  } finally {
    db.close();
  }
}

export async function listCanvasDrafts(
  canvasId: string,
): Promise<CanvasDraft[]> {
  const db = await openDatabase();
  try {
    const drafts = await requestResult<unknown[]>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .index(CANVAS_ID_INDEX)
        .getAll(canvasId),
    );
    return drafts
      .filter(
        (draft): draft is CanvasDraft =>
          isCanvasDraft(draft) && draft.canvas_id === canvasId,
      )
      .sort((left, right) => right.updated_at - left.updated_at);
  } finally {
    db.close();
  }
}

export async function putCanvasDraft(
  draft: Omit<CanvasDraft, "key">,
): Promise<void> {
  const db = await openDatabase();
  try {
    const transaction = db.transaction(DRAFT_STORE, "readwrite");
    transaction.objectStore(DRAFT_STORE).put({
      ...draft,
      key: canvasDraftKey(draft.canvas_id, draft.client_id),
    });
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}

export async function deleteCanvasDraft(
  canvasId: string,
  clientId: string,
): Promise<void> {
  const db = await openDatabase();
  try {
    const transaction = db.transaction(DRAFT_STORE, "readwrite");
    transaction
      .objectStore(DRAFT_STORE)
      .delete(canvasDraftKey(canvasId, clientId));
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}

export async function getCanvasSaveBatch(
  canvasId: string,
  clientId: string,
): Promise<PersistedCanvasSaveBatch | null> {
  const db = await openDatabase();
  try {
    const value = await requestResult<unknown>(
      db.transaction(SAVE_BATCH_STORE, "readonly")
        .objectStore(SAVE_BATCH_STORE)
        .get(canvasDraftKey(canvasId, clientId)),
    );
    return isCanvasSaveBatch(value, canvasId, clientId) ? value : null;
  } finally {
    db.close();
  }
}

export async function putCanvasSaveBatch(
  batch: Omit<PersistedCanvasSaveBatch, "key">,
): Promise<void> {
  const db = await openDatabase();
  try {
    const transaction = db.transaction(SAVE_BATCH_STORE, "readwrite");
    transaction.objectStore(SAVE_BATCH_STORE).put({
      ...batch,
      key: canvasDraftKey(batch.canvas_id, batch.client_id),
    });
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}

export async function deleteCanvasSaveBatch(
  canvasId: string,
  clientId: string,
): Promise<void> {
  const db = await openDatabase();
  try {
    const transaction = db.transaction(SAVE_BATCH_STORE, "readwrite");
    transaction
      .objectStore(SAVE_BATCH_STORE)
      .delete(canvasDraftKey(canvasId, clientId));
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("IndexedDB is unavailable"));
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const transaction = request.transaction;
      if (!transaction) return;
      const draftStore = request.result.objectStoreNames.contains(DRAFT_STORE)
        ? transaction.objectStore(DRAFT_STORE)
        : request.result.createObjectStore(DRAFT_STORE, { keyPath: "key" });
      if (!draftStore.indexNames.contains(CANVAS_ID_INDEX)) {
        draftStore.createIndex(CANVAS_ID_INDEX, "canvas_id");
      }
      const saveBatchStore = request.result.objectStoreNames.contains(
        SAVE_BATCH_STORE,
      )
        ? transaction.objectStore(SAVE_BATCH_STORE)
        : request.result.createObjectStore(SAVE_BATCH_STORE, {
            keyPath: "key",
          });
      if (!saveBatchStore.indexNames.contains(CANVAS_ID_INDEX)) {
        saveBatchStore.createIndex(CANVAS_ID_INDEX, "canvas_id");
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
    request.onblocked = () => reject(new Error("IndexedDB upgrade is blocked"));
  });
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

function localStorageOrNull(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function readCanvasEmergencyDrafts(
  storage: Storage,
): CanvasEmergencyDraft[] | null {
  try {
    const raw = storage.getItem(EMERGENCY_DRAFT_STORAGE_KEY);
    if (!raw || raw.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      !isRecord(parsed) ||
      parsed.version !== EMERGENCY_DRAFT_VERSION ||
      !Array.isArray(parsed.drafts)
    ) {
      return null;
    }
    const drafts = parsed.drafts.filter(isCanvasEmergencyDraft);
    if (drafts.length !== parsed.drafts.length) return null;
    const unique = new Map<string, CanvasEmergencyDraft>();
    for (const draft of drafts.sort(
      (left, right) => right.updated_at - left.updated_at,
    )) {
      if (
        unique.size >= MAX_EMERGENCY_DRAFTS ||
        unique.has(emergencyDraftIdentity(draft)) ||
        JSON.stringify(draft).length > MAX_EMERGENCY_DRAFT_LENGTH
      ) {
        continue;
      }
      unique.set(emergencyDraftIdentity(draft), draft);
    }
    return [...unique.values()];
  } catch {
    return null;
  }
}

function serializeCanvasEmergencyDrafts(
  drafts: readonly CanvasEmergencyDraft[],
): string {
  return JSON.stringify({
    version: EMERGENCY_DRAFT_VERSION,
    drafts,
  });
}

function isCanvasEmergencyDraft(value: unknown): value is CanvasEmergencyDraft {
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

function isCanvasDraft(
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

function isCanvasSaveBatch(
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

function isCanvasOperation(value: unknown): value is CanvasOperation {
  if (!isCanvasOperationRecord(value)) return false;
  return CANVAS_OPERATION_VALIDATORS[value.op]?.(value) === true;
}

function canvasOperationGroupSizesAreValid(
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

function emergencyDraftIdentity(
  draft: Pick<CanvasEmergencyDraft, "canvas_id" | "client_id">,
): string {
  return `${draft.canvas_id}\u0000${draft.client_id}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}
