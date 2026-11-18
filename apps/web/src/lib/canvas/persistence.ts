import {
  applyCanvasOperationsToGraph,
  commonOperationPrefixLength,
  diffCanvasGraphsFast,
  hasValidDraftEnvelopeForDelta,
  reconcileRecoveredCanvasDraft,
} from "./persistenceDelta";
import {
  EMERGENCY_DRAFT_STORAGE_KEY,
  MAX_EMERGENCY_DELTA_BYTES,
  MAX_EMERGENCY_DRAFTS,
  MAX_EMERGENCY_DRAFT_LENGTH,
  MAX_EMERGENCY_DRAFT_STORAGE_LENGTH,
  MAX_EMERGENCY_POINTER_BYTES,
  canvasDraftKey,
  deltaChangedNodesWithinConfigBudget,
  emergencyDraftIdentity,
  isCanvasDraft,
  isCanvasEmergencyDraft,
  isCanvasEmergencyPointer,
  isCanvasOperation,
  isCanvasSaveBatch,
  jsonValueEqual,
  readCanvasEmergencyEntries,
  serializeCanvasEmergencyEntries,
  stripEmergencyFullTag,
  utf8ByteLength,
  type CanvasDraft,
  type CanvasDraftSnapshot,
  type CanvasEmergencyDeltaDraft,
  type CanvasEmergencyDraft,
  type CanvasEmergencyPointer,
  type PersistedCanvasSaveBatch,
  type StoredCanvasEmergencyEntry,
  type StoredEmergencyFullDraft,
} from "./persistenceValidation";
import type { CanvasGraph, CanvasOperation } from "./types";

const DATABASE_NAME = "lumen-canvas";
const DATABASE_VERSION = 3;
const DRAFT_STORE = "drafts";
const SAVE_BATCH_STORE = "save-batches";
const CANVAS_ID_INDEX = "canvas_id";
const CANVAS_OWNER_STORAGE_KEY = "lumen:canvas-owner:v1";

let privateCanvasPersistenceEpoch = 0;
let privateCanvasActivationGeneration = 0;
let privateCanvasPersistenceEnabled = false;
let privateCanvasOwner: string | null = null;
const confirmedIdbDraftByKey = new Map<string, CanvasDraft>();

export {
  MAX_EMERGENCY_DELTA_BYTES,
  MAX_EMERGENCY_POINTER_BYTES,
  applyCanvasOperationsToGraph,
  canvasDraftKey,
};
export type {
  CanvasDraft,
  CanvasDraftSnapshot,
  CanvasEmergencyDeltaDraft,
  CanvasEmergencyDraft,
  CanvasEmergencyPointer,
  PersistedCanvasSaveBatch,
};

export function canvasDraftSnapshotMatches(
  acknowledged: CanvasDraftSnapshot | null,
  current: CanvasDraftSnapshot,
): boolean {
  return (
    acknowledged !== null &&
    acknowledged.graph === current.graph &&
    acknowledged.revision === current.revision &&
    acknowledged.pendingOperations === current.pendingOperations &&
    acknowledged.pendingOperationGroupSizes ===
      current.pendingOperationGroupSizes
  );
}

// Reload recovery needs localStorage for both emergency drafts and the owner fence.
export function canvasEmergencyStorageAvailable(): boolean {
  const key = "lumen:canvas-storage-probe";
  try {
    const storage = localStorageOrNull();
    if (!storage || !privateCanvasPersistenceEnabled || !privateCanvasOwner) {
      return false;
    }
    if (storage.getItem(CANVAS_OWNER_STORAGE_KEY) !== privateCanvasOwner) {
      return false;
    }
    storage.setItem(key, "1");
    const available = storage.getItem(key) === "1";
    storage.removeItem(key);
    return available;
  } catch {
    return false;
  }
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

export function getCanvasEmergencyPointer(
  canvasId: string,
  clientId?: string,
): CanvasEmergencyPointer | null {
  if (!privateCanvasPersistenceEnabled) return null;
  try {
    const storage = localStorageOrNull();
    if (!storage) return null;
    const entries = readCanvasEmergencyEntries(storage);
    if (!entries) return null;
    const pointers = entries.filter(
      (entry): entry is CanvasEmergencyPointer =>
        entry.kind === "pointer" && entry.canvas_id === canvasId,
    );
    return (
      pointers.find((entry) => entry.client_id === clientId) ??
      pointers[0] ??
      null
    );
  } catch {
    return null;
  }
}

export function putCanvasEmergencyPointer(
  pointer: Omit<CanvasEmergencyPointer, "kind">,
): boolean {
  if (!privateCanvasPersistenceEnabled) return false;
  try {
    const candidate: CanvasEmergencyPointer = { ...pointer, kind: "pointer" };
    if (!isCanvasEmergencyPointer(candidate)) return false;
    if (
      utf8ByteLength(JSON.stringify(candidate)) >= MAX_EMERGENCY_POINTER_BYTES
    ) {
      return false;
    }
    const storage = localStorageOrNull();
    if (!storage) return false;
    return writeEmergencyEntryToStorage(storage, candidate);
  } catch {
    return false;
  }
}

export function getCanvasEmergencyDraft(
  canvasId: string,
  clientId?: string,
): CanvasEmergencyDraft | null {
  if (!privateCanvasPersistenceEnabled) return null;
  try {
    const storage = localStorageOrNull();
    if (!storage) return null;
    const entries = readCanvasEmergencyEntries(storage);
    if (!entries) return null;
    const fullDrafts = entries.filter(
      (entry): entry is StoredEmergencyFullDraft =>
        entry.kind === "full" && entry.canvas_id === canvasId,
    );
    const matched =
      fullDrafts.find((draft) => draft.client_id === clientId) ??
      fullDrafts[0] ??
      null;
    return matched ? stripEmergencyFullTag(matched) : null;
  } catch {
    return null;
  }
}

export function putCanvasEmergencyDraft(draft: CanvasEmergencyDraft): boolean {
  if (!privateCanvasPersistenceEnabled) return false;
  try {
    const storage = localStorageOrNull();
    if (!storage) return false;
    const baseline = confirmedIdbDraftByKey.get(
      canvasDraftKey(draft.canvas_id, draft.client_id),
    );
    if (baseline && baseline.base_revision === draft.base_revision) {
      return putEmergencyDeltaAgainstBaseline(storage, baseline, draft);
    }
    return putEmergencyFullDraftFallback(storage, draft);
  } catch {
    return false;
  }
}

function putEmergencyDeltaAgainstBaseline(
  storage: Storage,
  baseline: CanvasDraft,
  draft: CanvasEmergencyDraft,
): boolean {
  if (!hasValidDraftEnvelopeForDelta(draft)) return false;

  const prefixOpCount = commonOperationPrefixLength(
    baseline.operations,
    draft.operations,
  );
  const deltaOperations = draft.operations.slice(prefixOpCount);
  const graphDeltaOperations = diffCanvasGraphsFast(
    baseline.graph,
    draft.graph,
  );
  const validDeltaOps =
    deltaOperations.every(isCanvasOperation) &&
    graphDeltaOperations.every(isCanvasOperation) &&
    deltaChangedNodesWithinConfigBudget(graphDeltaOperations);
  if (!validDeltaOps) return false;

  const hasUnconfirmedChanges =
    prefixOpCount < baseline.operations.length ||
    deltaOperations.length > 0 ||
    graphDeltaOperations.length > 0;
  if (!hasUnconfirmedChanges) {
    return writeEmergencyEntryToStorage(storage, {
      kind: "pointer",
      canvas_id: draft.canvas_id,
      client_id: draft.client_id,
      dirty_revision: draft.base_revision,
      updated_at: draft.updated_at,
      has_pending_idb: false,
      op_count: draft.operations.length,
    });
  }

  const deltaEntry: CanvasEmergencyDeltaDraft = {
    kind: "delta",
    canvas_id: draft.canvas_id,
    client_id: draft.client_id,
    base_revision: draft.base_revision,
    prefix_op_count: prefixOpCount,
    delta_operations: deltaOperations,
    graph_delta_operations: graphDeltaOperations,
    ...(draft.operation_group_sizes !== undefined
      ? { operation_group_sizes: [...draft.operation_group_sizes] }
      : {}),
    updated_at: draft.updated_at,
  };
  if (utf8ByteLength(JSON.stringify(deltaEntry)) > MAX_EMERGENCY_DELTA_BYTES) {
    return false;
  }
  return writeEmergencyEntryToStorage(storage, deltaEntry);
}

function putEmergencyFullDraftFallback(
  storage: Storage,
  draft: CanvasEmergencyDraft,
): boolean {
  if (!isCanvasEmergencyDraft(draft)) return false;
  const serializedDraft = JSON.stringify(draft);
  if (serializedDraft.length > MAX_EMERGENCY_DRAFT_LENGTH) return false;
  return writeEmergencyEntryToStorage(storage, { ...draft, kind: "full" });
}

function writeEmergencyEntryToStorage(
  storage: Storage,
  entry: StoredCanvasEmergencyEntry,
): boolean {
  const existing = readCanvasEmergencyEntries(storage) ?? [];
  const retained = existing
    .filter(
      (candidate) =>
        emergencyDraftIdentity(candidate) !== emergencyDraftIdentity(entry),
    )
    .sort((left, right) => right.updated_at - left.updated_at)
    .slice(0, MAX_EMERGENCY_DRAFTS - 1);
  const entries = [entry, ...retained];
  let serialized = serializeCanvasEmergencyEntries(entries);
  while (
    serialized.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH &&
    entries.length > 1
  ) {
    entries.pop();
    serialized = serializeCanvasEmergencyEntries(entries);
  }
  if (serialized.length > MAX_EMERGENCY_DRAFT_STORAGE_LENGTH) return false;
  storage.setItem(EMERGENCY_DRAFT_STORAGE_KEY, serialized);
  return true;
}

function buildCleanPointerFromBaseline(
  baseline: CanvasDraft,
): CanvasEmergencyPointer {
  return {
    kind: "pointer",
    canvas_id: baseline.canvas_id,
    client_id: baseline.client_id,
    dirty_revision: baseline.base_revision,
    updated_at: baseline.updated_at,
    has_pending_idb: false,
    op_count: baseline.operations.length,
  };
}

function filterEmergencyEntriesAfterDelete(
  existing: readonly StoredCanvasEmergencyEntry[],
  canvasId: string,
  clientId: string | undefined,
  baseline: CanvasDraft | undefined,
): { nextEntries: StoredCanvasEmergencyEntry[]; changed: boolean } {
  const nextEntries: StoredCanvasEmergencyEntry[] = [];
  let changed = false;
  const retainPointer = Boolean(baseline && baseline.operations.length > 0);
  for (const entry of existing) {
    const matchesTarget =
      entry.canvas_id === canvasId &&
      (clientId === undefined || entry.client_id === clientId);
    if (!matchesTarget) {
      nextEntries.push(entry);
      continue;
    }
    if (retainPointer && baseline) {
      if (entry.kind !== "pointer" || entry.has_pending_idb) changed = true;
      nextEntries.push(buildCleanPointerFromBaseline(baseline));
    } else {
      changed = true;
    }
  }
  return { nextEntries, changed };
}

export function deleteCanvasEmergencyDraft(
  canvasId: string,
  clientId?: string,
): void {
  try {
    const storage = localStorageOrNull();
    if (!storage) return;
    const existing = readCanvasEmergencyEntries(storage);
    if (!existing) {
      storage.removeItem(EMERGENCY_DRAFT_STORAGE_KEY);
      return;
    }
    const baseline =
      clientId !== undefined
        ? confirmedIdbDraftByKey.get(canvasDraftKey(canvasId, clientId))
        : undefined;
    const { nextEntries, changed } = filterEmergencyEntriesAfterDelete(
      existing,
      canvasId,
      clientId,
      baseline,
    );
    if (!changed && nextEntries.length === existing.length) return;
    if (nextEntries.length === 0) {
      storage.removeItem(EMERGENCY_DRAFT_STORAGE_KEY);
      return;
    }
    storage.setItem(
      EMERGENCY_DRAFT_STORAGE_KEY,
      serializeCanvasEmergencyEntries(nextEntries),
    );
  } catch {
    // Emergency cleanup is best effort in restricted storage contexts.
  }
}

export function resetCanvasPersistenceMemoryForTests(): void {
  confirmedIdbDraftByKey.clear();
}

async function clearPrivateCanvasStorage(): Promise<void> {
  confirmedIdbDraftByKey.clear();
  try {
    localStorageOrNull()?.removeItem(EMERGENCY_DRAFT_STORAGE_KEY);
  } catch {
    // Private-state cleanup is best effort in restricted storage contexts.
  }
  if (typeof indexedDB === "undefined") return;
  try {
    const db = await openDatabase();
    try {
      const transaction = db.transaction(
        [DRAFT_STORE, SAVE_BATCH_STORE],
        "readwrite",
      );
      transaction.objectStore(DRAFT_STORE).clear();
      transaction.objectStore(SAVE_BATCH_STORE).clear();
      await transactionDone(transaction);
    } finally {
      db.close();
    }
  } catch {
    // A navigation or storage policy may close IndexedDB during invalidation.
  }
}

export async function clearPrivateCanvasPersistence(): Promise<void> {
  privateCanvasActivationGeneration += 1;
  privateCanvasPersistenceEpoch += 1;
  privateCanvasPersistenceEnabled = false;
  privateCanvasOwner = null;
  await clearPrivateCanvasStorage();
}

export function resumePrivateCanvasPersistence(): void {
  privateCanvasActivationGeneration += 1;
  privateCanvasPersistenceEpoch += 1;
  privateCanvasPersistenceEnabled = true;
}

export async function activatePrivateCanvasPersistence(
  userId: string,
): Promise<void> {
  const normalizedUserId = userId.trim();
  if (!normalizedUserId) {
    await clearPrivateCanvasPersistence();
    return;
  }
  const activationGeneration = privateCanvasActivationGeneration + 1;
  privateCanvasActivationGeneration = activationGeneration;
  privateCanvasPersistenceEpoch += 1;
  privateCanvasPersistenceEnabled = false;
  privateCanvasOwner = null;
  const storage = localStorageOrNull();
  let previousOwner: string | null = null;
  try {
    previousOwner = storage?.getItem(CANVAS_OWNER_STORAGE_KEY)?.trim() || null;
  } catch {
    previousOwner = null;
  }
  if (previousOwner !== normalizedUserId) {
    await clearPrivateCanvasStorage();
  }
  if (activationGeneration !== privateCanvasActivationGeneration) return;
  try {
    storage?.setItem(CANVAS_OWNER_STORAGE_KEY, normalizedUserId);
  } catch {
    // The in-memory fence still protects this page when storage is restricted.
  }
  if (activationGeneration !== privateCanvasActivationGeneration) return;
  privateCanvasPersistenceEpoch += 1;
  privateCanvasOwner = normalizedUserId;
  privateCanvasPersistenceEnabled = true;
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
  if (!privateCanvasPersistenceEnabled) return null;
  const db = await openDatabase();
  try {
    const key = canvasDraftKey(canvasId, clientId);
    const value = await requestResult<unknown>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .get(key),
    );
    const baseDraft = isCanvasDraft(value, canvasId, clientId) ? value : null;
    if (baseDraft) {
      confirmedIdbDraftByKey.set(key, baseDraft);
    }
    const emergencyEntry = findStoredEmergencyEntry(canvasId, clientId);
    if (!baseDraft) {
      return emergencyEntry?.kind === "full"
        ? { ...stripEmergencyFullTag(emergencyEntry), key }
        : null;
    }
    const saveBatch = await readOptionalSaveBatch(
      db,
      canvasId,
      clientId,
      emergencyEntry !== null,
    );
    return reconcileRecoveredCanvasDraft(baseDraft, saveBatch, emergencyEntry);
  } finally {
    db.close();
  }
}

export async function listCanvasDrafts(
  canvasId: string,
): Promise<CanvasDraft[]> {
  if (!privateCanvasPersistenceEnabled) return [];
  const db = await openDatabase();
  try {
    const drafts = await requestResult<unknown[]>(
      db.transaction(DRAFT_STORE, "readonly")
        .objectStore(DRAFT_STORE)
        .index(CANVAS_ID_INDEX)
        .getAll(canvasId),
    );
    const validDrafts = drafts.filter(
      (draft): draft is CanvasDraft =>
        isCanvasDraft(draft) && draft.canvas_id === canvasId,
    );
    const reconciled: CanvasDraft[] = [];
    for (const baseDraft of validDrafts) {
      confirmedIdbDraftByKey.set(baseDraft.key, baseDraft);
      const emergencyEntry = findStoredEmergencyEntry(
        canvasId,
        baseDraft.client_id,
      );
      const saveBatch = await readOptionalSaveBatch(
        db,
        canvasId,
        baseDraft.client_id,
        emergencyEntry !== null,
      );
      reconciled.push(
        reconcileRecoveredCanvasDraft(baseDraft, saveBatch, emergencyEntry),
      );
    }
    return reconciled.sort((left, right) => right.updated_at - left.updated_at);
  } finally {
    db.close();
  }
}

function syncConfirmedIdbDraftAfterWrite(
  epoch: number,
  persistedDraft: CanvasDraft,
): void {
  if (
    !privateCanvasPersistenceEnabled ||
    epoch !== privateCanvasPersistenceEpoch
  ) {
    return;
  }
  if (persistedDraft.operations.length === 0) {
    confirmedIdbDraftByKey.delete(persistedDraft.key);
    deleteCanvasEmergencyDraft(
      persistedDraft.canvas_id,
      persistedDraft.client_id,
    );
    return;
  }
  confirmedIdbDraftByKey.set(persistedDraft.key, persistedDraft);
  putCanvasEmergencyPointer({
    canvas_id: persistedDraft.canvas_id,
    client_id: persistedDraft.client_id,
    dirty_revision: persistedDraft.base_revision,
    updated_at: persistedDraft.updated_at,
    has_pending_idb: false,
    op_count: persistedDraft.operations.length,
  });
}

export async function putCanvasDraft(
  draft: Omit<CanvasDraft, "key">,
): Promise<void> {
  const epoch = privateCanvasPersistenceEpoch;
  if (privateCanvasPersistenceEnabled) {
    putCanvasEmergencyPointer({
      canvas_id: draft.canvas_id,
      client_id: draft.client_id,
      dirty_revision: draft.base_revision,
      updated_at: draft.updated_at,
      has_pending_idb: true,
      op_count: draft.operations.length,
    });
  }
  const db = await openDatabase();
  try {
    assertPrivateCanvasPersistenceEpoch(epoch);
    const persistedDraft: CanvasDraft = {
      ...draft,
      key: canvasDraftKey(draft.canvas_id, draft.client_id),
    };
    const transaction = db.transaction(DRAFT_STORE, "readwrite");
    transaction.objectStore(DRAFT_STORE).put(persistedDraft);
    await transactionDone(transaction);
    syncConfirmedIdbDraftAfterWrite(epoch, persistedDraft);
  } finally {
    db.close();
  }
}

export async function deleteCanvasDraft(
  canvasId: string,
  clientId: string,
): Promise<void> {
  const key = canvasDraftKey(canvasId, clientId);
  confirmedIdbDraftByKey.delete(key);
  const db = await openDatabase();
  try {
    const transaction = db.transaction(DRAFT_STORE, "readwrite");
    transaction.objectStore(DRAFT_STORE).delete(key);
    await transactionDone(transaction);
    deleteCanvasEmergencyDraft(canvasId, clientId);
  } finally {
    db.close();
  }
}

export async function getCanvasSaveBatch(
  canvasId: string,
  clientId: string,
): Promise<PersistedCanvasSaveBatch | null> {
  if (!privateCanvasPersistenceEnabled) return null;
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
  const epoch = privateCanvasPersistenceEpoch;
  const db = await openDatabase();
  try {
    assertPrivateCanvasPersistenceEpoch(epoch);
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
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB open failed"));
    request.onblocked = () => reject(new Error("IndexedDB upgrade is blocked"));
  });
}

function assertPrivateCanvasPersistenceEpoch(epoch: number): void {
  if (
    !privateCanvasPersistenceEnabled ||
    epoch !== privateCanvasPersistenceEpoch
  ) {
    throw new DOMException(
      "Canvas persistence was invalidated",
      "AbortError",
    );
  }
}

function localStorageOrNull(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function findStoredEmergencyEntry(
  canvasId: string,
  clientId: string,
): StoredCanvasEmergencyEntry | null {
  try {
    const storage = localStorageOrNull();
    if (!storage) return null;
    const entries = readCanvasEmergencyEntries(storage);
    if (!entries) return null;
    return (
      entries.find(
        (entry) =>
          entry.canvas_id === canvasId && entry.client_id === clientId,
      ) ?? null
    );
  } catch {
    return null;
  }
}

async function readOptionalSaveBatch(
  db: IDBDatabase,
  canvasId: string,
  clientId: string,
  shouldCheck: boolean,
): Promise<PersistedCanvasSaveBatch | null> {
  if (!shouldCheck) return null;
  try {
    const raw = await requestResult<unknown>(
      db.transaction(SAVE_BATCH_STORE, "readonly")
        .objectStore(SAVE_BATCH_STORE)
        .get(canvasDraftKey(canvasId, clientId)),
    );
    return isCanvasSaveBatch(raw, canvasId, clientId) ? raw : null;
  } catch {
    return null;
  }
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
