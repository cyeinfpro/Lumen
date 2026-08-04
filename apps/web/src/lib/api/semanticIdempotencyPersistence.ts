export const STORAGE_VERSION = 3;
export const CATALOG_VERSION = 4;
const PRIOR_STORAGE_VERSION = 2;
const LEGACY_STORAGE_VERSION = 1;
export const MAX_STORAGE_LENGTH = 64 * 1024;
const DEFAULT_WEB_LOCK_DEADLINE_MS = 5_000;

export type DurableStorage = Pick<
  Storage,
  "getItem" | "setItem" | "removeItem"
> &
  Partial<Pick<Storage, "key">> & {
    readonly length?: number;
  };

export type ExclusiveLockGuard = {
  assertCurrent: () => void | Promise<void>;
  runMutation: <T>(
    mutation: () => T,
    rollback?: () => void,
  ) => T | Promise<T>;
};

export const UNFENCED_LOCK_GUARD: ExclusiveLockGuard = Object.freeze({
  assertCurrent: () => {},
  runMutation: <T>(mutation: () => T) => mutation(),
});

export type ExclusiveLockRequest = <T>(
  name: string,
  options: { mode: "exclusive" },
  callback: (guard?: ExclusiveLockGuard) => T | Promise<T>,
) => Promise<T>;

export type SemanticIdempotencyStoreOptions = {
  ttlMs?: number;
  maxEntries?: number;
  now?: () => number;
  freshKey?: () => string;
  freshNamespace?: () => string;
  storage?: DurableStorage | null;
  storageKey?: string;
  digest?: (value: string) => string | Promise<string>;
  lockRequest?: ExclusiveLockRequest | null;
  indexedDb?: IDBFactory | null;
};

export type PersistedPendingOperation = {
  key: string;
  generation: number;
  expiresAt: number;
  ownerToken?: string;
  submitted?: boolean;
  shared?: boolean;
};

export type PersistedResolvedEntry = {
  version: typeof STORAGE_VERSION;
  state: "resolved";
  fingerprint: string;
  generation: number;
  lastUsedAt: number;
};

export type PersistedEntry = PersistedResolvedEntry;

export type PersistedRoot = {
  version: typeof STORAGE_VERSION;
  namespace: string;
  sequence: number;
  pending: Record<string, PersistedPendingOperation>;
  identity: string | null;
  identityGeneration: number;
};

export type PersistedCatalog = {
  version: typeof CATALOG_VERSION;
  state: "catalog";
  namespace: string;
};

export type PriorPersistedPendingEntry = {
  version: typeof PRIOR_STORAGE_VERSION;
  state: "pending";
  fingerprint: string;
  key: string;
  generation: number;
  expiresAt: number;
};

export type PriorPersistedResolvedEntry = {
  version: typeof PRIOR_STORAGE_VERSION;
  state: "resolved";
  fingerprint: string;
  generation: number;
  expiresAt: number;
};

export type PriorPersistedEntry =
  | PriorPersistedPendingEntry
  | PriorPersistedResolvedEntry;

export type PriorPersistedRoot = {
  version: typeof PRIOR_STORAGE_VERSION;
  identity: string | null;
  identityGeneration: number;
};

export type LegacyPersistedEntry = {
  fingerprint: string;
  key: string;
  expiresAt: number;
};

export type LegacyPersistedState = {
  version: typeof LEGACY_STORAGE_VERSION;
  identity: string;
  entries: LegacyPersistedEntry[];
};

export type RootSnapshot =
  | { kind: "missing" }
  | { kind: "catalog"; state: PersistedCatalog }
  | { kind: "current"; state: PersistedRoot }
  | { kind: "prior"; state: PriorPersistedRoot }
  | { kind: "legacy"; state: LegacyPersistedState };

export class SemanticIdempotencyDurabilityError extends Error {
  readonly code = "semantic_idempotency_durability";
  readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "SemanticIdempotencyDurabilityError";
    this.cause = cause;
  }
}

export function durabilityError(
  message: string,
  cause?: unknown,
): SemanticIdempotencyDurabilityError {
  return new SemanticIdempotencyDurabilityError(message, cause);
}

export type LocalStorageAccess =
  | { kind: "available"; storage: DurableStorage }
  | { kind: "server" }
  | { kind: "blocked"; error: unknown };

export type ResolvedStorageAccess = {
  storage: DurableStorage | null;
  error: unknown | null;
};

export function localStorageAccess(): LocalStorageAccess {
  const browser = typeof window !== "undefined";
  if (!("localStorage" in globalThis)) {
    return browser
      ? {
          kind: "blocked",
          error: new Error("localStorage is unavailable"),
        }
      : { kind: "server" };
  }
  try {
    const storage = globalThis.localStorage;
    return storage
      ? { kind: "available", storage }
      : {
          kind: "blocked",
          error: new Error("localStorage is unavailable"),
        };
  } catch (error) {
    return { kind: "blocked", error };
  }
}

export function resolveStorageAccess(
  configured: DurableStorage | null | undefined,
): ResolvedStorageAccess {
  if (configured !== undefined) {
    return { storage: configured, error: null };
  }
  const access = localStorageAccess();
  if (access.kind === "available") {
    return { storage: access.storage, error: null };
  }
  return {
    storage: null,
    error: access.kind === "blocked" ? access.error : null,
  };
}

function deadlineTimer(
  callback: () => void,
  delayMs: number,
): ReturnType<typeof setTimeout> {
  return setTimeout(callback, delayMs);
}

function abortControllerOrNull(): typeof AbortController | null {
  try {
    return typeof AbortController === "function" ? AbortController : null;
  } catch {
    return null;
  }
}

export function webLockRequestOrNull(
  deadlineMs = DEFAULT_WEB_LOCK_DEADLINE_MS,
): ExclusiveLockRequest | null {
  if (!Number.isSafeInteger(deadlineMs) || deadlineMs <= 0) {
    throw new TypeError("web lock deadline must be a positive integer");
  }
  try {
    const Controller = abortControllerOrNull();
    if (
      !Controller ||
      typeof navigator === "undefined" ||
      !navigator.locks
    ) {
      return null;
    }
    const locks = navigator.locks;
    const requestWithSignal = locks.request.bind(locks) as <T>(
      name: string,
      options: { mode: "exclusive"; signal: AbortSignal },
      callback: () => T | Promise<T>,
    ) => Promise<T>;
    return async <T>(
      name: string,
      options: { mode: "exclusive" },
      callback: (guard?: ExclusiveLockGuard) => T | Promise<T>,
    ): Promise<T> =>
      new Promise<T>((resolve, reject) => {
        const controller = new Controller();
        const expired = durabilityError("web lock acquisition timed out");
        let settled = false;
        let granted = false;
        const timer = deadlineTimer(() => {
          if (settled || granted) return;
          settled = true;
          try {
            controller.abort(expired);
          } catch {
            controller.abort();
          }
          reject(expired);
        }, deadlineMs);
        const cleanup = () => clearTimeout(timer);
        const invoke = async (): Promise<T> => {
          if (settled || controller.signal.aborted) throw expired;
          granted = true;
          cleanup();
          return callback(UNFENCED_LOCK_GUARD);
        };
        let pending: Promise<T>;
        try {
          pending = requestWithSignal(
            name,
            { ...options, signal: controller.signal },
            invoke,
          );
          if (
            !pending ||
            typeof (pending as { then?: unknown }).then !== "function"
          ) {
            throw new TypeError("Web Locks request did not return a promise");
          }
        } catch (error) {
          settled = true;
          cleanup();
          reject(durabilityError("web lock request failed", error));
          return;
        }
        Promise.resolve(pending).then(
          (value) => {
            if (settled) return;
            settled = true;
            cleanup();
            if (!granted) {
              reject(
                durabilityError(
                  "web lock request completed without granting the lock",
                ),
              );
              return;
            }
            resolve(value);
          },
          (error: unknown) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(
              controller.signal.aborted
                ? expired
                : durabilityError("web lock request failed", error),
            );
          },
        );
      });
  } catch {
    return null;
  }
}

export async function sha256Hex(value: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error("Web Crypto digest is unavailable");
  }
  const digest = await subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function normalizedIdentity(userId: string | null): string | null {
  const value = userId?.trim() ?? "";
  return value || null;
}

export function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

function isKey(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 128 &&
    /^[\x21-\x7e]+$/.test(value)
  );
}

export function isStorageNamespace(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 128 &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

function isGeneration(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function isExpiry(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isPendingOperation(
  value: unknown,
): value is PersistedPendingOperation {
  if (value === null || typeof value !== "object") return false;
  const operation = value as Partial<PersistedPendingOperation>;
  return (
    isKey(operation.key) &&
    isGeneration(operation.generation) &&
    isExpiry(operation.expiresAt) &&
    (operation.ownerToken === undefined ||
      isStorageNamespace(operation.ownerToken)) &&
    (operation.submitted === undefined ||
      typeof operation.submitted === "boolean") &&
    (operation.shared === undefined ||
      typeof operation.shared === "boolean")
  );
}

function isPendingMap(
  value: unknown,
): value is Record<string, PersistedPendingOperation> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  return Object.entries(value).every(
    ([fingerprint, operation]) =>
      isDigest(fingerprint) && isPendingOperation(operation),
  );
}

function validLegacyEntry(value: unknown): value is LegacyPersistedEntry {
  if (value === null || typeof value !== "object") return false;
  const entry = value as Partial<LegacyPersistedEntry>;
  return (
    isDigest(entry.fingerprint) &&
    isKey(entry.key) &&
    isExpiry(entry.expiresAt)
  );
}

export function validLegacyState(
  value: unknown,
): value is LegacyPersistedState {
  if (value === null || typeof value !== "object") return false;
  const state = value as Partial<LegacyPersistedState>;
  return (
    state.version === LEGACY_STORAGE_VERSION &&
    isDigest(state.identity) &&
    Array.isArray(state.entries) &&
    state.entries.every(validLegacyEntry)
  );
}

export function validRoot(value: unknown): value is PersistedRoot {
  if (value === null || typeof value !== "object") return false;
  const root = value as Partial<PersistedRoot>;
  return (
    root.version === STORAGE_VERSION &&
    isStorageNamespace(root.namespace) &&
    Number.isSafeInteger(root.sequence) &&
    Number(root.sequence) >= 0 &&
    isPendingMap(root.pending) &&
    Object.values(root.pending).every(
      (operation) => operation.generation <= Number(root.sequence),
    ) &&
    (root.identity === null || isDigest(root.identity)) &&
    Number.isSafeInteger(root.identityGeneration) &&
    Number(root.identityGeneration) >= 0
  );
}

export function validCatalog(
  value: unknown,
): value is PersistedCatalog {
  if (value === null || typeof value !== "object") return false;
  const catalog = value as Partial<PersistedCatalog>;
  return (
    catalog.version === CATALOG_VERSION &&
    catalog.state === "catalog" &&
    isStorageNamespace(catalog.namespace)
  );
}

export function validPriorRoot(
  value: unknown,
): value is PriorPersistedRoot {
  if (value === null || typeof value !== "object") return false;
  const root = value as Partial<PriorPersistedRoot>;
  return (
    root.version === PRIOR_STORAGE_VERSION &&
    (root.identity === null || isDigest(root.identity)) &&
    Number.isSafeInteger(root.identityGeneration) &&
    Number(root.identityGeneration) >= 0
  );
}

export function validEntry(value: unknown): value is PersistedEntry {
  if (value === null || typeof value !== "object") return false;
  const entry = value as Partial<PersistedEntry>;
  if (
    entry.version !== STORAGE_VERSION ||
    !isDigest(entry.fingerprint) ||
    !isGeneration(entry.generation)
  ) {
    return false;
  }
  return (
    entry.state === "resolved" &&
    !("key" in entry) &&
    isExpiry(entry.lastUsedAt)
  );
}

export function validPriorEntry(
  value: unknown,
): value is PriorPersistedEntry {
  if (value === null || typeof value !== "object") return false;
  const entry = value as Partial<PriorPersistedEntry>;
  if (
    entry.version !== PRIOR_STORAGE_VERSION ||
    !isDigest(entry.fingerprint) ||
    !isGeneration(entry.generation) ||
    !isExpiry(entry.expiresAt)
  ) {
    return false;
  }
  if (entry.state === "resolved") return !("key" in entry);
  return entry.state === "pending" && isKey(entry.key);
}
