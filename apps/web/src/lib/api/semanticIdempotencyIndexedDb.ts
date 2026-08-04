import { durabilityError } from "./semanticIdempotencyPersistence";
import type {
  TransactionalLockChange,
  TransactionalLockStore,
} from "./semanticIdempotencyStorageLockTypes";

const DATABASE_NAME = "lumen-semantic-idempotency-locks";
const DATABASE_VERSION = 1;
const OBJECT_STORE_NAME = "locks";

function abortError(signal: AbortSignal, fallback: string) {
  return signal.reason instanceof Error
    ? signal.reason
    : durabilityError(fallback);
}

function closeDatabase(database: IDBDatabase): void {
  try {
    database.close();
  } catch {
    // A late open/close event must not escape into the lock caller.
  }
}

function openIndexedDb(
  factory: IDBFactory,
  signal: AbortSignal,
): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let request: IDBOpenDBRequest;
    let settled = false;
    const cleanup = () => {
      signal.removeEventListener("abort", onAbort);
    };
    const rejectOnce = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const onAbort = () => {
      rejectOnce(abortError(signal, "indexeddb storage lock open aborted"));
    };
    if (signal.aborted) {
      rejectOnce(abortError(signal, "indexeddb storage lock open aborted"));
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
    try {
      request = factory.open(DATABASE_NAME, DATABASE_VERSION);
    } catch (error) {
      rejectOnce(durabilityError("indexeddb storage lock open failed", error));
      return;
    }
    request.onupgradeneeded = () => {
      if (settled) {
        try {
          request.transaction?.abort();
        } catch {
          // The request is already being cancelled.
        }
        return;
      }
      const database = request.result;
      if (!database.objectStoreNames.contains(OBJECT_STORE_NAME)) {
        database.createObjectStore(OBJECT_STORE_NAME, { keyPath: "name" });
      }
    };
    request.onblocked = () => {
      rejectOnce(durabilityError("indexeddb storage lock open was blocked"));
    };
    request.onerror = () => {
      rejectOnce(
        durabilityError(
          "indexeddb storage lock open failed",
          request.error,
        ),
      );
    };
    request.onsuccess = () => {
      const database = request.result;
      if (settled) {
        closeDatabase(database);
        return;
      }
      if (!database.objectStoreNames.contains(OBJECT_STORE_NAME)) {
        closeDatabase(database);
        rejectOnce(
          durabilityError("indexeddb storage lock store is unavailable"),
        );
        return;
      }
      settled = true;
      cleanup();
      database.onversionchange = () => closeDatabase(database);
      resolve(database);
    };
  });
}

function indexedDbStore(
  database: IDBDatabase,
): TransactionalLockStore {
  return {
    transact<T>(
      name: string,
      operation: (record: unknown) => TransactionalLockChange<T>,
      rollback?: () => void,
      signal?: AbortSignal,
    ): Promise<T> {
      return new Promise((resolve, reject) => {
        let transaction: IDBTransaction;
        let result!: T;
        let operationError: unknown | null = null;
        let rolledBack = false;
        let settled = false;
        const rollbackOnce = () => {
          if (rolledBack || !rollback) return;
          rolledBack = true;
          try {
            rollback();
          } catch (error) {
            operationError ??= error;
          }
        };
        const cleanup = () => {
          signal?.removeEventListener("abort", onAbort);
        };
        const rejectOnce = (error: unknown, abort = true) => {
          if (settled) return;
          settled = true;
          operationError ??= error;
          rollbackOnce();
          cleanup();
          if (abort) {
            try {
              transaction.abort();
            } catch {
              // The transaction may already be aborting.
            }
          }
          reject(operationError);
        };
        const onAbort = () => {
          rejectOnce(
            abortError(
              signal!,
              "indexeddb storage lock transaction aborted",
            ),
          );
        };
        if (signal?.aborted) {
          rejectOnce(
            abortError(signal, "indexeddb storage lock transaction aborted"),
            false,
          );
          return;
        }
        try {
          transaction = database.transaction(
            OBJECT_STORE_NAME,
            "readwrite",
          );
        } catch (error) {
          rejectOnce(
            durabilityError(
              "indexeddb storage lock transaction failed",
              error,
            ),
            false,
          );
          return;
        }
        signal?.addEventListener("abort", onAbort, { once: true });
        transaction.oncomplete = () => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve(result);
        };
        transaction.onerror = () => {
          rejectOnce(
            durabilityError(
              "indexeddb storage lock transaction failed",
              transaction.error,
            ),
            false,
          );
        };
        transaction.onabort = () => {
          rejectOnce(
            operationError ??
              durabilityError(
                "indexeddb storage lock transaction failed",
                transaction.error,
              ),
            false,
          );
        };
        let getRequest: IDBRequest<unknown>;
        try {
          getRequest = transaction.objectStore(OBJECT_STORE_NAME).get(name);
        } catch (error) {
          rejectOnce(
            durabilityError("indexeddb storage lock read failed", error),
          );
          return;
        }
        getRequest.onerror = () => {
          rejectOnce(
            durabilityError(
              "indexeddb storage lock read failed",
              getRequest.error,
            ),
          );
        };
        getRequest.onsuccess = () => {
          if (settled) return;
          let change: TransactionalLockChange<T>;
          try {
            change = operation(getRequest.result);
            result = change.value;
          } catch (error) {
            rejectOnce(error);
            return;
          }
          if (!change.record || settled) return;
          let putRequest: IDBRequest<IDBValidKey>;
          try {
            putRequest = transaction
              .objectStore(OBJECT_STORE_NAME)
              .put(change.record);
          } catch (error) {
            rejectOnce(
              durabilityError("indexeddb storage lock write failed", error),
            );
            return;
          }
          putRequest.onerror = () => {
            rejectOnce(
              durabilityError(
                "indexeddb storage lock write failed",
                putRequest.error,
              ),
            );
          };
        };
      });
    },
    close() {
      closeDatabase(database);
    },
  };
}

export function indexedDbStoreFactory(
  factory: IDBFactory,
): (signal: AbortSignal) => Promise<TransactionalLockStore> {
  return async (signal) =>
    indexedDbStore(await openIndexedDb(factory, signal));
}
