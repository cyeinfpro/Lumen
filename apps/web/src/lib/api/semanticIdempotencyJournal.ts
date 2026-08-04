import {
  pendingMatchesLease,
  type SemanticIdempotencyLease,
} from "./semanticIdempotencyLease";
import {
  durabilityError,
  MAX_STORAGE_LENGTH,
  STORAGE_VERSION,
  validCatalog,
  validLegacyState,
  validPriorRoot,
  validRoot,
  type DurableStorage,
  type ExclusiveLockGuard,
  type PersistedCatalog,
  type PersistedResolvedEntry,
  type PersistedRoot,
  type RootSnapshot,
} from "./semanticIdempotencyPersistence";
import { compactResolvedEntries } from "./semanticIdempotencyRecords";
import {
  enumerateStorageKeys,
  readStorageRaw,
  removeStorageRaw,
  writeStorageRaw,
} from "./semanticIdempotencyStorage";

export function parsePersistedRoot(
  raw: string | null,
  allowMalformed: boolean,
): RootSnapshot {
  if (raw === null) return { kind: "missing" };
  if (raw.length > MAX_STORAGE_LENGTH) {
    if (allowMalformed) return { kind: "missing" };
    throw durabilityError("shared idempotency root is too large");
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (validCatalog(parsed)) return { kind: "catalog", state: parsed };
    if (validRoot(parsed)) return { kind: "current", state: parsed };
    if (validPriorRoot(parsed)) return { kind: "prior", state: parsed };
    if (validLegacyState(parsed)) return { kind: "legacy", state: parsed };
  } catch (error) {
    if (!allowMalformed) {
      throw durabilityError("shared idempotency root is malformed", error);
    }
  }
  if (allowMalformed) return { kind: "missing" };
  throw durabilityError("shared idempotency root is malformed");
}

export async function writePersistedCatalog(
  storage: DurableStorage,
  storageKey: string,
  catalog: PersistedCatalog,
  guard: ExclusiveLockGuard,
): Promise<void> {
  const serialized = JSON.stringify(catalog);
  if (serialized.length > MAX_STORAGE_LENGTH) {
    throw durabilityError("shared idempotency catalog is too large");
  }
  await writeStorageRaw(storage, storageKey, serialized, guard);
}

export async function writePersistedRoot(
  storage: DurableStorage,
  storageKey: string,
  root: PersistedRoot,
  guard: ExclusiveLockGuard,
): Promise<boolean> {
  const serialized = JSON.stringify(root);
  if (serialized.length > MAX_STORAGE_LENGTH) {
    throw durabilityError("shared idempotency root is too large");
  }
  await writeStorageRaw(storage, storageKey, serialized, guard);
  return true;
}

function entryPrefix(storageKey: string, namespace: string): string {
  return `${storageKey}.entry.${namespace}.`;
}

export async function writePersistedResolvedEntry(
  storage: DurableStorage,
  storageKey: string,
  namespace: string,
  entry: PersistedResolvedEntry,
  guard: ExclusiveLockGuard,
): Promise<boolean> {
  const key = `${entryPrefix(storageKey, namespace)}${entry.fingerprint}`;
  const serialized = JSON.stringify(entry);
  if (serialized.length > MAX_STORAGE_LENGTH) {
    throw durabilityError("shared idempotency entry is too large");
  }
  await writeStorageRaw(storage, key, serialized, guard);
  return true;
}

export async function removePersistedEntryRecords(
  storage: DurableStorage,
  storageKey: string,
  namespace: string | undefined,
  guard: ExclusiveLockGuard,
): Promise<void> {
  const prefix = namespace
    ? entryPrefix(storageKey, namespace)
    : `${storageKey}.entry.`;
  const keys = enumerateStorageKeys(storage, prefix, false);
  if (!keys) return;
  for (const key of keys) {
    await removeStorageRaw(storage, key, guard);
  }
}

export async function writeResolvedTombstoneBestEffort(
  storage: DurableStorage,
  storageKey: string,
  namespace: string,
  lease: SemanticIdempotencyLease,
  maxEntries: number,
  now: () => number,
  guard: ExclusiveLockGuard,
): Promise<void> {
  const resolved: PersistedResolvedEntry = {
    version: STORAGE_VERSION,
    state: "resolved",
    fingerprint: lease.fingerprint,
    generation: lease.generation,
    lastUsedAt: now(),
  };
  try {
    if (
      !(await writePersistedResolvedEntry(
        storage,
        storageKey,
        namespace,
        resolved,
        guard,
      ))
    ) {
      return;
    }
    await compactResolvedEntries(
      storage,
      storageKey,
      namespace,
      maxEntries,
      undefined,
      guard,
    );
  } catch {
    // Retiring the root journal is the terminal commit. Tombstones are only
    // bounded history used to avoid deterministic key reuse after compaction.
  }
}

export function durableLeaseIsRetired(
  storage: DurableStorage,
  rootStorageKey: string,
  lease: SemanticIdempotencyLease,
  expected: {
    identityDigest: string;
    storageNamespace: string;
    identityGeneration: number;
  },
): boolean {
  if (!lease.durable || !lease.namespace) return false;
  try {
    const snapshot = parsePersistedRoot(
      readStorageRaw(storage, rootStorageKey),
      false,
    );
    if (
      snapshot.kind !== "current" ||
      snapshot.state.namespace !== expected.storageNamespace ||
      snapshot.state.identity !== expected.identityDigest ||
      snapshot.state.identityGeneration !== expected.identityGeneration ||
      snapshot.state.sequence < lease.generation ||
      [
        snapshot.state.namespace,
        snapshot.state.identity,
        snapshot.state.identityGeneration,
      ].join(".") !== lease.namespace
    ) {
      return false;
    }
    return !pendingMatchesLease(
      snapshot.state.pending[lease.fingerprint],
      lease,
    );
  } catch {
    return false;
  }
}
