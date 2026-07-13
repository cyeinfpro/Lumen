import type { CanvasGraph, CanvasOperation } from "./types";

const DATABASE_NAME = "lumen-canvas";
const DATABASE_VERSION = 1;
const DRAFT_STORE = "drafts";

export interface CanvasDraft {
  key: string;
  canvas_id: string;
  client_id: string;
  base_revision: number;
  graph: CanvasGraph;
  operations: CanvasOperation[];
  updated_at: number;
}

export function canvasDraftKey(canvasId: string, clientId: string): string {
  return `${canvasId}:${clientId}`;
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

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("IndexedDB is unavailable"));
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(DRAFT_STORE)) {
        request.result.createObjectStore(DRAFT_STORE, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
    request.onblocked = () => reject(new Error("IndexedDB upgrade is blocked"));
  });
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
