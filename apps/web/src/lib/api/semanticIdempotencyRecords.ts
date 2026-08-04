import {
  durabilityError,
  MAX_STORAGE_LENGTH,
  validEntry,
  validPriorEntry,
  type DurableStorage,
  type ExclusiveLockGuard,
  type PersistedResolvedEntry,
  type PriorPersistedEntry,
  type PriorPersistedRoot,
} from "./semanticIdempotencyPersistence";
import {
  enumerateStorageKeys,
  readStorageRaw,
  removeStorageRaw,
} from "./semanticIdempotencyStorage";

function entryPrefix(storageKey: string, namespace: string): string {
  return `${storageKey}.entry.${namespace}.`;
}

function enumerateKeys(storage: DurableStorage, prefix: string): string[] {
  return enumerateStorageKeys(storage, prefix, true) ?? [];
}

function parsedValue(raw: string, description: string): unknown {
  if (raw.length > MAX_STORAGE_LENGTH) {
    throw durabilityError(`${description} is too large`);
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch (error) {
    throw durabilityError(`${description} is malformed`, error);
  }
}

export function priorIdentityNamespace(root: PriorPersistedRoot): string {
  if (!root.identity) {
    throw durabilityError("prior idempotency identity is unavailable");
  }
  return `${root.identity}.${root.identityGeneration}`;
}

export function readPriorPersistedEntries(
  storage: DurableStorage,
  storageKey: string,
  root: PriorPersistedRoot,
): PriorPersistedEntry[] {
  const prefix = entryPrefix(storageKey, priorIdentityNamespace(root));
  return enumerateKeys(storage, prefix).flatMap((key) => {
    const raw = readStorageRaw(storage, key);
    if (raw === null) return [];
    const fingerprint = key.slice(prefix.length);
    const parsed = parsedValue(raw, "prior idempotency entry");
    if (validPriorEntry(parsed) && parsed.fingerprint === fingerprint) {
      return [parsed];
    }
    throw durabilityError("prior idempotency entry is malformed");
  });
}

export async function compactResolvedEntries(
  storage: DurableStorage,
  storageKey: string,
  namespace: string,
  limit: number,
  keepFingerprint?: string,
  guard?: ExclusiveLockGuard,
): Promise<void> {
  const prefix = entryPrefix(storageKey, namespace);
  const resolved: Array<{
    storageKey: string;
    entry: PersistedResolvedEntry;
  }> = [];
  for (const key of enumerateKeys(storage, prefix)) {
    const fingerprint = key.slice(prefix.length);
    if (fingerprint === keepFingerprint) continue;
    const raw = readStorageRaw(storage, key);
    if (raw === null) continue;
    const parsed = parsedValue(raw, "shared idempotency entry");
    if (!validEntry(parsed) || parsed.fingerprint !== fingerprint) {
      throw durabilityError("shared idempotency entry is malformed");
    }
    if (parsed.state === "resolved") {
      resolved.push({ storageKey: key, entry: parsed });
    }
  }
  resolved.sort(
    (left, right) =>
      left.entry.lastUsedAt - right.entry.lastUsedAt ||
      left.entry.generation - right.entry.generation,
  );
  for (const record of resolved.slice(0, Math.max(0, resolved.length - limit))) {
    if (!guard) {
      throw durabilityError("shared idempotency storage guard is unavailable");
    }
    await removeStorageRaw(storage, record.storageKey, guard);
  }
}
