import {
  CATALOG_VERSION,
  durabilityError,
  isStorageNamespace,
  STORAGE_VERSION,
  type DurableStorage,
  type ExclusiveLockGuard,
  type LegacyPersistedEntry,
  type PersistedCatalog,
  type PersistedRoot,
  type PriorPersistedRoot,
  type RootSnapshot,
} from "./semanticIdempotencyPersistence";
import {
  priorIdentityNamespace,
  readPriorPersistedEntries,
} from "./semanticIdempotencyRecords";
import {
  removePersistedEntryRecords,
  writePersistedCatalog,
  writePersistedRoot,
} from "./semanticIdempotencyJournal";

export function createPersistedRoot(
  identity: string | null,
  identityGeneration: number,
  freshNamespace: () => string,
): PersistedRoot {
  const namespace = freshNamespace();
  if (!isStorageNamespace(namespace)) {
    throw durabilityError("fresh idempotency namespace is invalid");
  }
  return {
    version: STORAGE_VERSION,
    namespace,
    sequence: 0,
    pending: {},
    identity,
    identityGeneration,
  };
}

export function migrateLegacyRoot(
  legacyEntries: LegacyPersistedEntry[],
  identityDigest: string,
  freshNamespace: () => string,
): PersistedRoot {
  const root = createPersistedRoot(identityDigest, 1, freshNamespace);
  const pending = { ...root.pending };
  let sequence = 0;
  for (const legacy of legacyEntries) {
    sequence += 1;
    pending[legacy.fingerprint] = {
      key: legacy.key,
      generation: sequence,
      expiresAt: legacy.expiresAt,
    };
  }
  return { ...root, sequence, pending };
}

export function migratePriorRoot(
  storage: DurableStorage,
  storageKey: string,
  priorRoot: PriorPersistedRoot,
  identityDigest: string,
  freshNamespace: () => string,
): PersistedRoot {
  const root = createPersistedRoot(
    identityDigest,
    priorRoot.identityGeneration,
    freshNamespace,
  );
  const pending = { ...root.pending };
  let sequence = 0;
  for (const entry of readPriorPersistedEntries(
    storage,
    storageKey,
    priorRoot,
  )) {
    if (entry.state !== "pending") continue;
    sequence += 1;
    pending[entry.fingerprint] = {
      key: entry.key,
      generation: sequence,
      expiresAt: entry.expiresAt,
    };
  }
  return { ...root, sequence, pending };
}

function catalogWithNamespace(namespace: string): PersistedCatalog {
  if (!isStorageNamespace(namespace)) {
    throw durabilityError("idempotency catalog namespace is invalid");
  }
  return {
    version: CATALOG_VERSION,
    state: "catalog",
    namespace,
  };
}

export function createPersistedCatalog(
  freshNamespace: () => string,
): PersistedCatalog {
  return catalogWithNamespace(freshNamespace());
}

export function identityRootStorageKey(
  storageKey: string,
  catalogNamespace: string,
  identityDigest: string,
): string {
  return `${storageKey}.root.${catalogNamespace}.${identityDigest}`;
}

type CatalogMigration = {
  catalog: PersistedCatalog;
  root: PersistedRoot | null;
  cleanupNamespace: string | null;
};

function migrationState(
  storage: DurableStorage,
  storageKey: string,
  snapshot: RootSnapshot,
  freshNamespace: () => string,
): CatalogMigration {
  if (snapshot.kind === "catalog") {
    return { catalog: snapshot.state, root: null, cleanupNamespace: null };
  }
  if (snapshot.kind === "missing") {
    return {
      catalog: createPersistedCatalog(freshNamespace),
      root: null,
      cleanupNamespace: null,
    };
  }
  if (snapshot.kind === "current") {
    if (
      snapshot.state.identity === null &&
      Object.keys(snapshot.state.pending).length > 0
    ) {
      throw durabilityError(
        "unowned legacy idempotency operations cannot be migrated",
      );
    }
    return {
      catalog: catalogWithNamespace(snapshot.state.namespace),
      root: snapshot.state.identity ? snapshot.state : null,
      cleanupNamespace: null,
    };
  }
  if (snapshot.kind === "legacy") {
    const root = migrateLegacyRoot(
      snapshot.state.entries,
      snapshot.state.identity,
      freshNamespace,
    );
    return {
      catalog: catalogWithNamespace(root.namespace),
      root,
      cleanupNamespace: null,
    };
  }
  if (!snapshot.state.identity) {
    return {
      catalog: createPersistedCatalog(freshNamespace),
      root: null,
      cleanupNamespace: null,
    };
  }
  const cleanupNamespace = priorIdentityNamespace(snapshot.state);
  const root = migratePriorRoot(
    storage,
    storageKey,
    snapshot.state,
    snapshot.state.identity,
    freshNamespace,
  );
  return {
    catalog: catalogWithNamespace(root.namespace),
    root,
    cleanupNamespace,
  };
}

export async function ensurePersistedCatalog(
  storage: DurableStorage,
  storageKey: string,
  snapshot: RootSnapshot,
  freshNamespace: () => string,
  guard: ExclusiveLockGuard,
): Promise<PersistedCatalog> {
  if (snapshot.kind === "catalog") return snapshot.state;
  const migration = migrationState(
    storage,
    storageKey,
    snapshot,
    freshNamespace,
  );
  if (migration.root?.identity) {
    await writePersistedRoot(
      storage,
      identityRootStorageKey(
        storageKey,
        migration.catalog.namespace,
        migration.root.identity,
      ),
      migration.root,
      guard,
    );
  }
  await writePersistedCatalog(
    storage,
    storageKey,
    migration.catalog,
    guard,
  );
  if (migration.cleanupNamespace) {
    try {
      await removePersistedEntryRecords(
        storage,
        storageKey,
        migration.cleanupNamespace,
        guard,
      );
    } catch {
      // The catalog/root commit is authoritative. Legacy resolved records are
      // privacy-safe digests and can be left for later storage maintenance.
    }
  }
  return migration.catalog;
}
