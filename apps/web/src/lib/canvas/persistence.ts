import type { CanvasGraph, CanvasOperation } from "./types";

const DATABASE_NAME = "lumen-canvas";
const DATABASE_VERSION = 3;
const DRAFT_STORE = "drafts";
const SAVE_BATCH_STORE = "save-batches";
const CANVAS_ID_INDEX = "canvas_id";

export interface CanvasDraft {
  key: string;
  canvas_id: string;
  client_id: string;
  base_revision: number;
  graph: CanvasGraph;
  operations: CanvasOperation[];
  updated_at: number;
}

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

  constructor(write: () => Promise<void>) {
    this.write = write;
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
      await this.write().catch(() => undefined);
    }
  }
}

export function canvasDraftKey(canvasId: string, clientId: string): string {
  return `${canvasId}:${clientId}`;
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
    return await requestResult<CanvasDraft | undefined>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .get(canvasDraftKey(canvasId, clientId)),
    ).then((value) => value ?? null);
  } finally {
    db.close();
  }
}

export async function listCanvasDrafts(
  canvasId: string,
): Promise<CanvasDraft[]> {
  const db = await openDatabase();
  try {
    const drafts = await requestResult<CanvasDraft[]>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .index(CANVAS_ID_INDEX)
        .getAll(canvasId),
    );
    return drafts.sort((left, right) => right.updated_at - left.updated_at);
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
    return await requestResult<PersistedCanvasSaveBatch | undefined>(
      db.transaction(SAVE_BATCH_STORE, "readonly")
        .objectStore(SAVE_BATCH_STORE)
        .get(canvasDraftKey(canvasId, clientId)),
    ).then((value) => value ?? null);
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
