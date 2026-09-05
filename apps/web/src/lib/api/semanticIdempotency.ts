import { uuid } from "../utils";
import {
  durableLeaseIsRetired,
  parsePersistedRoot,
  writePersistedRoot,
  writeResolvedTombstoneBestEffort,
} from "./semanticIdempotencyJournal";
import {
  creatorCanDiscard,
  pendingEntry,
  pendingMatchesLease,
  semanticLease,
  type PendingEntry,
  type SemanticIdempotencyLease,
} from "./semanticIdempotencyLease";
import {
  createPersistedRoot,
  ensurePersistedCatalog,
  identityRootStorageKey,
} from "./semanticIdempotencyMigration";
import {
  durabilityError,
  isDigest,
  normalizedIdentity,
  resolveStorageAccess,
  SemanticIdempotencyDurabilityError,
  sha256Hex,
  UNFENCED_LOCK_GUARD,
  webLockRequestOrNull,
  type DurableStorage,
  type ExclusiveLockGuard,
  type ExclusiveLockRequest,
  type PersistedPendingOperation,
  type PersistedRoot,
  type SemanticIdempotencyStoreOptions,
} from "./semanticIdempotencyPersistence";
import {
  compactResolvedEntries,
} from "./semanticIdempotencyRecords";
import { readStorageRaw } from "./semanticIdempotencyStorage";
import {
  failClosedLockRequest,
  indexedDbLockRequestOrNull,
} from "./semanticIdempotencyStorageLock";
import {
  isAmbiguousRequestFailure,
  semanticRequestFingerprint,
} from "./semanticIdempotencySemantics";

const DEFAULT_TTL_MS = 15 * 60_000;
const DEFAULT_MAX_ENTRIES = 128;
const DEFAULT_STORAGE_KEY = "lumen.semantic-idempotency.v1";
const MAX_WRITE_ATTEMPTS = 4;

type ActiveRoot = { root: PersistedRoot; raw: string; namespace: string };

function resolveLockRequest(
  options: SemanticIdempotencyStoreOptions,
  now: () => number,
): ExclusiveLockRequest | null {
  if (options.lockRequest !== undefined) return options.lockRequest;
  return (
    webLockRequestOrNull() ??
    indexedDbLockRequestOrNull(options.indexedDb, now, uuid) ??
    failClosedLockRequest()
  );
}

export type { SemanticIdempotencyLease } from "./semanticIdempotencyLease";
export type { SemanticIdempotencyStoreOptions } from "./semanticIdempotencyPersistence";

export { SemanticIdempotencyDurabilityError };

export class SemanticIdempotencyStore {
  private readonly entries = new Map<string, PendingEntry>();
  private readonly ttlMs: number;
  private readonly maxEntries: number;
  private readonly now: () => number;
  private readonly freshKey: () => string;
  private readonly freshNamespace: () => string;
  private readonly storage: DurableStorage | null;
  private readonly storageAccessError: unknown | null;
  private readonly storageKey: string;
  private readonly digest: (value: string) => string | Promise<string>;
  private readonly lockRequest: ExclusiveLockRequest | null;
  private readonly lockName: string;
  private identity: string | null = null;
  private identityDigest: string | null = null;
  private storageNamespace: string | null = null;
  private identityNamespace: string | null = null;
  private rootStorageKey: string | null = null;
  private identityGeneration = 0;
  private identityEpoch = 0;
  private identityActivation: Promise<void> | null = null;
  private activationDurabilityError: SemanticIdempotencyDurabilityError | null =
    null;

  constructor(options: SemanticIdempotencyStoreOptions = {}) {
    this.ttlMs = options.ttlMs ?? DEFAULT_TTL_MS;
    this.maxEntries = options.maxEntries ?? DEFAULT_MAX_ENTRIES;
    this.now = options.now ?? Date.now;
    this.freshKey = options.freshKey ?? uuid;
    this.freshNamespace = options.freshNamespace ?? uuid;
    const storageAccess = resolveStorageAccess(options.storage);
    this.storage = storageAccess.storage;
    this.storageAccessError = storageAccess.error;
    this.storageKey = options.storageKey ?? DEFAULT_STORAGE_KEY;
    this.digest = options.digest ?? sha256Hex;
    this.lockRequest = resolveLockRequest(options, this.now);
    this.lockName = `${this.storageKey}.lock`;
    if (!Number.isFinite(this.ttlMs) || this.ttlMs <= 0) {
      throw new TypeError("idempotency ttlMs must be positive");
    }
    if (!Number.isInteger(this.maxEntries) || this.maxEntries <= 0) {
      throw new TypeError("idempotency maxEntries must be a positive integer");
    }
  }

  activateIdentity(userId: string | null): Promise<void> {
    const nextIdentity = normalizedIdentity(userId);
    if (
      this.identity === nextIdentity &&
      this.identityActivation !== null
    ) {
      return this.identityActivation;
    }

    const identityChanged =
      this.identity !== nextIdentity || nextIdentity === null;
    const epoch = identityChanged ? ++this.identityEpoch : this.identityEpoch;
    this.identity = nextIdentity;
    this.identityDigest = null;
    this.storageNamespace = null;
    this.identityNamespace = null;
    this.rootStorageKey = null;
    this.identityGeneration = 0;
    this.activationDurabilityError = null;
    this.entries.clear();

    const activation = this.startIdentityActivation(nextIdentity, epoch);
    this.identityActivation = activation;
    void activation.finally(() => {
      if (this.identityActivation === activation) {
        this.identityActivation = null;
      }
    });
    return activation;
  }

  private async startIdentityActivation(
    identity: string | null,
    epoch: number,
  ): Promise<void> {
    if (identity === null) {
      await this.activateLoggedOut(epoch);
      return;
    }

    let identityDigest: string;
    try {
      const value = await this.digest(`identity:${identity}`);
      if (!isDigest(value)) {
        throw new Error("identity digest is not SHA-256");
      }
      identityDigest = value;
    } catch (error) {
      this.setActivationError(identity, epoch, "identity hashing failed", error);
      return;
    }
    if (!this.identityIsCurrent(identity, epoch)) return;
    this.identityDigest = identityDigest;
    if (this.storageAccessError) {
      this.setActivationError(
        identity,
        epoch,
        "browser idempotency storage is unavailable",
        this.storageAccessError,
      );
      return;
    }
    if (!this.storage) return;

    try {
      await this.withExclusive((guard) =>
        this.activatePersistedIdentity(
          identityDigest,
          identity,
          epoch,
          guard,
        ),
      );
    } catch (error) {
      this.setActivationError(
        identity,
        epoch,
        "shared idempotency state is unavailable",
        error,
      );
    }
  }

  private async activateLoggedOut(epoch: number): Promise<void> {
    this.assertIdentityCurrent(null, epoch);
  }

  private async activatePersistedIdentity(
    identityDigest: string,
    identity: string,
    epoch: number,
    guard: ExclusiveLockGuard,
  ): Promise<void> {
    this.assertIdentityCurrent(identity, epoch);
    const catalog = await ensurePersistedCatalog(
      this.requireStorage(),
      this.storageKey,
      parsePersistedRoot(this.readRaw(this.storageKey), false),
      this.freshNamespace,
      guard,
    );
    this.assertIdentityCurrent(identity, epoch);
    const rootStorageKey = identityRootStorageKey(
      this.storageKey,
      catalog.namespace,
      identityDigest,
    );
    const snapshot = parsePersistedRoot(
      this.readRaw(rootStorageKey),
      false,
    );
    let root: PersistedRoot;
    if (snapshot.kind === "missing") {
      root = createPersistedRoot(identityDigest, 1, this.freshNamespace);
      await writePersistedRoot(
        this.requireStorage(),
        rootStorageKey,
        root,
        guard,
      );
    } else if (
      snapshot.kind === "current" &&
      snapshot.state.identity === identityDigest
    ) {
      root = snapshot.state;
    } else {
      throw durabilityError("identity idempotency root is malformed");
    }
    this.activateNamespace(
      root,
      identityDigest,
      identity,
      epoch,
      rootStorageKey,
    );
  }

  private activateNamespace(
    root: PersistedRoot,
    identityDigest: string,
    identity: string,
    epoch: number,
    rootStorageKey: string,
  ): void {
    this.assertIdentityCurrent(identity, epoch);
    this.identityDigest = identityDigest;
    this.storageNamespace = root.namespace;
    this.identityGeneration = root.identityGeneration;
    this.identityNamespace = this.namespaceFor(
      root.namespace,
      identityDigest,
      root.identityGeneration,
    );
    this.rootStorageKey = rootStorageKey;
    this.activationDurabilityError = null;
  }

  async acquire(
    scope: unknown,
    payload: unknown,
  ): Promise<SemanticIdempotencyLease> {
    const identity = this.identity;
    const identityEpoch = this.identityEpoch;
    const activation = this.identityActivation;
    if (activation) await activation;
    this.assertIdentityCurrent(identity, identityEpoch);
    if (identity !== null && this.activationDurabilityError) {
      throw this.activationDurabilityError;
    }

    const canonical = semanticRequestFingerprint(scope, payload);
    let fingerprint: string;
    try {
      const value = await this.digest(`request:${canonical}`);
      if (!isDigest(value)) throw new Error("request digest is not SHA-256");
      fingerprint = value;
    } catch (error) {
      if (this.requiresDurability()) {
        throw durabilityError("request hashing failed", error);
      }
      return this.acquireMemory(canonical, identityEpoch, uuid());
    }
    this.assertIdentityCurrent(identity, identityEpoch);

    if (!this.requiresDurability()) {
      return this.acquireMemory(fingerprint, identityEpoch, uuid());
    }
    this.throwActivationError();
    const participantToken = uuid();
    return this.withExclusive((guard) =>
      this.acquireDurable(
        fingerprint,
        identity,
        identityEpoch,
        participantToken,
        guard,
      ),
    );
  }

  private acquireMemory(
    fingerprint: string,
    identityEpoch: number,
    participantToken: string,
  ): SemanticIdempotencyLease {
    const existing = this.entries.get(fingerprint);
    if (existing?.identityEpoch === identityEpoch) {
      existing.shared = true;
      return semanticLease(
        fingerprint,
        existing,
        participantToken,
        "borrowed",
      );
    }
    const entry: PendingEntry = {
      key: this.freshKey(),
      generation: 1,
      expiresAt: this.now() + this.ttlMs,
      durable: false,
      identityEpoch,
      namespace: null,
      ownerToken: participantToken,
      submitted: false,
      shared: false,
    };
    this.entries.set(fingerprint, entry);
    return semanticLease(fingerprint, entry, participantToken, "created");
  }

  private async acquireDurable(
    fingerprint: string,
    identity: string | null,
    identityEpoch: number,
    participantToken: string,
    guard: ExclusiveLockGuard,
  ): Promise<SemanticIdempotencyLease> {
    for (let attempt = 0; attempt < MAX_WRITE_ATTEMPTS; attempt += 1) {
      this.assertIdentityCurrent(identity, identityEpoch);
      const active = this.reloadRootForOperation(identity, identityEpoch);
      const { namespace } = active;
      const existing = active.root.pending[fingerprint];
      if (existing) {
        const borrowed =
          existing.shared === true ? existing : { ...existing, shared: true };
        if (
          borrowed !== existing &&
          !(await this.tryWriteRoot(
            {
              ...active.root,
              pending: { ...active.root.pending, [fingerprint]: borrowed },
            },
            guard,
          ))
        ) {
          continue;
        }
        return semanticLease(
          fingerprint,
          this.cachePending(fingerprint, borrowed, identityEpoch, namespace),
          participantToken,
          "borrowed",
        );
      }

      await compactResolvedEntries(
        this.requireStorage(),
        this.storageKey,
        namespace,
        Math.max(0, this.maxEntries - 1),
        fingerprint,
        guard,
      );
      const generation = this.nextSequence(active.root.sequence);
      const key = await this.deriveDurableKey(
        active.root.namespace,
        fingerprint,
        generation,
      );
      this.assertIdentityCurrent(identity, identityEpoch);
      if (this.readRaw(this.requireRootStorageKey()) !== active.raw) continue;

      const pending: PersistedPendingOperation = {
        key,
        generation,
        expiresAt: this.now() + this.ttlMs,
        ownerToken: participantToken,
        submitted: false,
        shared: false,
      };
      const nextRoot: PersistedRoot = {
        ...active.root,
        sequence: generation,
        pending: { ...active.root.pending, [fingerprint]: pending },
      };
      if (!(await this.tryWriteRoot(nextRoot, guard))) continue;
      return semanticLease(
        fingerprint,
        this.cachePending(fingerprint, pending, identityEpoch, namespace),
        participantToken,
        "created",
      );
    }
    throw durabilityError("could not persist a shared idempotency key");
  }

  async confirm(lease: SemanticIdempotencyLease): Promise<void> {
    try {
      await this.resolveLease(lease);
    } catch (error) {
      if (
        error instanceof SemanticIdempotencyDurabilityError &&
        this.durableLeaseIsRetired(lease)
      ) {
        this.entries.delete(lease.fingerprint);
        return;
      }
      throw error;
    }
  }

  // Verified snapshots may confirm a lost POST reply without allocating an intent.
  async confirmPendingKey(userId: string, key: string): Promise<void> {
    const epoch = this.identityEpoch;
    if (this.identityActivation) await this.identityActivation;
    if (!this.identityIsCurrent(userId, epoch)) return;
    let match: [string, PendingEntry] | undefined;
    if (this.requiresDurability()) {
      this.throwActivationError();
      const { root, namespace } = this.reloadRootForOperation(userId, epoch);
      const record = Object.entries(root.pending).find(([, entry]) => entry.key === key);
      if (record) match = [record[0], pendingEntry(record[1], epoch, namespace)];
    } else {
      match = [...this.entries].find(([, entry]) => entry.key === key);
    }
    if (match) await this.confirm(semanticLease(match[0], match[1], "", "borrowed"));
  }

  async markSubmitted(lease: SemanticIdempotencyLease): Promise<void> {
    if (!this.leaseIsCurrent(lease)) {
      throw new Error("semantic idempotency identity changed");
    }
    if (!lease.durable) {
      const current = this.entries.get(lease.fingerprint);
      if (
        !current ||
        current.key !== lease.key ||
        current.generation !== lease.generation
      ) {
        throw durabilityError("semantic idempotency lease is unavailable");
      }
      current.submitted = true;
      return;
    }
    this.throwActivationError();
    await this.withExclusive(async (guard) => {
      for (let attempt = 0; attempt < MAX_WRITE_ATTEMPTS; attempt += 1) {
        if (!this.leaseIsCurrent(lease)) {
          throw new Error("semantic idempotency identity changed");
        }
        const active = this.reloadRootForOperation(
          this.identity,
          lease.identityEpoch,
        );
        if (active.namespace !== lease.namespace) {
          throw durabilityError("semantic idempotency lease is unavailable");
        }
        const existing = active.root.pending[lease.fingerprint];
        if (!pendingMatchesLease(existing, lease)) {
          throw durabilityError("semantic idempotency lease is unavailable");
        }
        if (existing.submitted === true) {
          this.cachePending(
            lease.fingerprint,
            existing,
            lease.identityEpoch,
            active.namespace,
          );
          return;
        }
        const submitted = { ...existing, submitted: true };
        if (this.readRaw(this.requireRootStorageKey()) !== active.raw) continue;
        if (
          !(await this.tryWriteRoot(
            {
              ...active.root,
              pending: {
                ...active.root.pending,
                [lease.fingerprint]: submitted,
              },
            },
            guard,
          ))
        ) {
          continue;
        }
        this.cachePending(
          lease.fingerprint,
          submitted,
          lease.identityEpoch,
          active.namespace,
        );
        return;
      }
      throw durabilityError("could not persist submitted idempotency lease");
    });
  }

  async recordFailure(
    lease: SemanticIdempotencyLease,
    error: unknown,
  ): Promise<void> {
    if (!this.leaseIsCurrent(lease)) return;
    if (!isAmbiguousRequestFailure(error)) {
      await this.resolveLease(lease);
      return;
    }
    await this.retainLease(lease);
  }

  async discard(lease: SemanticIdempotencyLease): Promise<void> {
    if (!this.leaseIsCurrent(lease)) return;
    if (!lease.durable) {
      const current = this.entries.get(lease.fingerprint);
      if (
        current?.key === lease.key &&
        current.generation === lease.generation &&
        lease.ownership === "created" &&
        current.ownerToken === lease.participantToken &&
        !current.submitted &&
        !current.shared
      ) {
        this.entries.delete(lease.fingerprint);
      }
      return;
    }
    this.throwActivationError();
    await this.withExclusive(async (guard) => {
      for (let attempt = 0; attempt < MAX_WRITE_ATTEMPTS; attempt += 1) {
        if (!this.leaseIsCurrent(lease)) return;
        const active = this.reloadRootForOperation(
          this.identity,
          lease.identityEpoch,
        );
        if (active.namespace !== lease.namespace) return;
        const existing = active.root.pending[lease.fingerprint];
        if (!pendingMatchesLease(existing, lease)) {
          this.entries.delete(lease.fingerprint);
          return;
        }
        if (!creatorCanDiscard(existing, lease)) {
          this.cachePending(
            lease.fingerprint,
            existing,
            lease.identityEpoch,
            active.namespace,
          );
          return;
        }
        const pending = { ...active.root.pending };
        delete pending[lease.fingerprint];
        if (this.readRaw(this.requireRootStorageKey()) !== active.raw) continue;
        if (
          !(await this.tryWriteRoot({ ...active.root, pending }, guard))
        ) {
          continue;
        }
        this.entries.delete(lease.fingerprint);
        return;
      }
      throw durabilityError("could not discard the shared idempotency key");
    });
  }

  clear(): Promise<void> {
    return this.activateIdentity(null);
  }

  private async resolveLease(
    lease: SemanticIdempotencyLease,
  ): Promise<void> {
    if (!this.leaseIsCurrent(lease)) return;
    if (!lease.durable) {
      this.deleteMemoryLease(lease);
      return;
    }
    this.throwActivationError();
    await this.withExclusive(async (guard) => {
      if (!this.leaseIsCurrent(lease)) return;
      const { namespace } = this.reloadRootForOperation(
        this.identity,
        lease.identityEpoch,
      );
      if (namespace !== lease.namespace) return;
      for (let attempt = 0; attempt < MAX_WRITE_ATTEMPTS; attempt += 1) {
        const active = this.reloadRootForOperation(
          this.identity,
          lease.identityEpoch,
        );
        const existing = active.root.pending[lease.fingerprint];
        if (!pendingMatchesLease(existing, lease)) {
          this.entries.delete(lease.fingerprint);
          return;
        }
        const pending = { ...active.root.pending };
        delete pending[lease.fingerprint];
        if (this.readRaw(this.requireRootStorageKey()) !== active.raw) continue;
        if (
          !(await this.tryWriteRoot({ ...active.root, pending }, guard))
        ) {
          continue;
        }
        await writeResolvedTombstoneBestEffort(
          this.requireStorage(),
          this.storageKey,
          namespace,
          lease,
          this.maxEntries,
          this.now,
          guard,
        );
        this.entries.delete(lease.fingerprint);
        return;
      }
      throw durabilityError("could not retire the shared idempotency key");
    });
  }

  private async retainLease(
    lease: SemanticIdempotencyLease,
  ): Promise<void> {
    if (!lease.durable) {
      const current = this.entries.get(lease.fingerprint);
      if (
        !current ||
        (current.key === lease.key &&
          current.identityEpoch === lease.identityEpoch)
      ) {
        this.entries.set(lease.fingerprint, {
          key: lease.key,
          generation: lease.generation,
          expiresAt: lease.expiresAt,
          durable: false,
          identityEpoch: lease.identityEpoch,
          namespace: null,
          ownerToken:
            lease.ownership === "created" ? lease.participantToken : null,
          submitted: true,
          shared: lease.ownership === "borrowed",
        });
      }
      return;
    }
    this.throwActivationError();
    await this.withExclusive(async (guard) => {
      await guard.assertCurrent();
      if (!this.leaseIsCurrent(lease)) return;
      const active = this.reloadRootForOperation(
        this.identity,
        lease.identityEpoch,
      );
      const { namespace } = active;
      if (namespace !== lease.namespace) return;
      const existing = active.root.pending[lease.fingerprint];
      if (pendingMatchesLease(existing, lease)) {
        this.cachePending(
          lease.fingerprint,
          existing,
          lease.identityEpoch,
          namespace,
        );
      } else {
        this.entries.delete(lease.fingerprint);
      }
    });
  }

  private deleteMemoryLease(lease: SemanticIdempotencyLease): void {
    const current = this.entries.get(lease.fingerprint);
    if (
      current?.key === lease.key &&
      current.identityEpoch === lease.identityEpoch
    ) {
      this.entries.delete(lease.fingerprint);
    }
  }

  private cachePending(
    fingerprint: string,
    entry: PersistedPendingOperation,
    identityEpoch: number,
    namespace: string,
  ): PendingEntry {
    const pending = pendingEntry(entry, identityEpoch, namespace);
    this.entries.set(fingerprint, pending);
    return pending;
  }

  private async deriveDurableKey(
    storageNamespace: string,
    fingerprint: string,
    generation: number,
  ): Promise<string> {
    try {
      const value = await this.digest(
        `idempotency-key:${storageNamespace}:${this.requireIdentityDigest()}:${fingerprint}:${generation}`,
      );
      if (!isDigest(value)) throw new Error("key digest is not SHA-256");
      // API idempotency contracts cap client keys at 64 characters. Keep the
      // full SHA-256 digest instead of adding a prefix that makes the key 73
      // characters and causes durable-browser requests to fail validation.
      return value;
    } catch (error) {
      throw durabilityError("idempotency key derivation failed", error);
    }
  }

  private nextSequence(sequence: number): number {
    const next = sequence + 1;
    if (!Number.isSafeInteger(next) || next <= sequence) {
      throw durabilityError("idempotency sequence is exhausted");
    }
    return next;
  }

  private durableLeaseIsRetired(
    lease: SemanticIdempotencyLease,
  ): boolean {
    if (
      !lease.durable ||
      !this.storage ||
      !this.identityDigest ||
      !this.storageNamespace
    ) {
      return false;
    }
    return durableLeaseIsRetired(
      this.storage,
      this.requireRootStorageKey(),
      lease,
      {
      identityDigest: this.identityDigest,
      storageNamespace: this.storageNamespace,
      identityGeneration: this.identityGeneration,
      },
    );
  }

  private reloadRootForOperation(
    identity: string | null,
    identityEpoch: number,
  ): ActiveRoot {
    this.assertIdentityCurrent(identity, identityEpoch);
    const identityDigest = this.requireIdentityDigest();
    const storageNamespace = this.requireStorageNamespace();
    const namespace = this.requireNamespace();
    const raw = this.readRaw(this.requireRootStorageKey());
    const snapshot = parsePersistedRoot(raw, false);
    if (
      snapshot.kind !== "current" ||
      snapshot.state.namespace !== storageNamespace ||
      snapshot.state.identity !== identityDigest ||
      snapshot.state.identityGeneration !== this.identityGeneration
    ) {
      throw new Error("semantic idempotency identity changed");
    }
    if (raw === null) {
      throw durabilityError("shared idempotency root is unavailable");
    }
    return { root: snapshot.state, raw, namespace };
  }

  private async tryWriteRoot(
    root: PersistedRoot,
    guard: ExclusiveLockGuard,
  ): Promise<boolean> {
    return writePersistedRoot(
      this.requireStorage(),
      this.requireRootStorageKey(),
      root,
      guard,
    );
  }

  private readRaw(key: string): string | null {
    return readStorageRaw(this.requireStorage(), key);
  }

  private namespaceFor(
    storageNamespace: string,
    identityDigest: string,
    generation: number,
  ): string {
    return `${storageNamespace}.${identityDigest}.${generation}`;
  }

  private requireStorage(): DurableStorage {
    if (!this.storage) {
      throw durabilityError("shared idempotency storage is unavailable");
    }
    return this.storage;
  }

  private requireIdentityDigest(): string {
    if (!this.identityDigest) {
      throw durabilityError("semantic idempotency identity is not active");
    }
    return this.identityDigest;
  }

  private requireStorageNamespace(): string {
    if (!this.storageNamespace) {
      throw durabilityError("semantic idempotency storage namespace is not active");
    }
    return this.storageNamespace;
  }

  private requireNamespace(): string {
    if (!this.identityNamespace) {
      throw durabilityError("semantic idempotency namespace is not active");
    }
    return this.identityNamespace;
  }

  private requireRootStorageKey(): string {
    if (!this.rootStorageKey) {
      throw durabilityError("semantic idempotency root is not active");
    }
    return this.rootStorageKey;
  }

  private requiresDurability(): boolean {
    return this.storage !== null && this.identity !== null;
  }

  private throwActivationError(): void {
    if (this.activationDurabilityError) throw this.activationDurabilityError;
    this.requireStorage();
    this.requireIdentityDigest();
    this.requireNamespace();
    this.requireRootStorageKey();
  }

  private setActivationError(
    identity: string,
    epoch: number,
    message: string,
    cause: unknown,
  ): void {
    if (!this.identityIsCurrent(identity, epoch)) return;
    this.activationDurabilityError = durabilityError(message, cause);
  }

  private withExclusive<T>(
    callback: (guard: ExclusiveLockGuard) => T | Promise<T>,
  ): Promise<T> {
    const run = async (guard = UNFENCED_LOCK_GUARD) => {
      await guard.assertCurrent();
      const result = await callback(guard);
      await guard.assertCurrent();
      return result;
    };
    if (!this.lockRequest) return Promise.resolve().then(() => run());
    return this.lockRequest(this.lockName, { mode: "exclusive" }, run);
  }

  private identityIsCurrent(identity: string | null, epoch: number): boolean {
    return this.identity === identity && this.identityEpoch === epoch;
  }

  private assertIdentityCurrent(
    identity: string | null,
    epoch: number,
  ): void {
    if (!this.identityIsCurrent(identity, epoch)) {
      throw new Error("semantic idempotency identity changed");
    }
  }

  private leaseIsCurrent(lease: SemanticIdempotencyLease): boolean {
    return lease.identityEpoch === this.identityEpoch;
  }
}

export const semanticPostIdempotency = new SemanticIdempotencyStore();

export async function withSemanticPostIdempotency<T>(
  scope: unknown,
  payload: unknown,
  request: (idempotencyKey: string) => Promise<T>,
): Promise<T> {
  const lease = await semanticPostIdempotency.acquire(scope, payload);
  try {
    await semanticPostIdempotency.markSubmitted(lease);
    const result = await request(lease.key);
    await semanticPostIdempotency.confirm(lease);
    return result;
  } catch (error) {
    await semanticPostIdempotency.recordFailure(lease, error);
    throw error;
  }
}

export {
  idempotentPostRequest,
  semanticJsonPostRequest,
  semanticPostRequest,
} from "./semanticIdempotencyRequest";
export {
  isAmbiguousRequestFailure,
  isDefinitiveRequestFailure,
  markDefinitiveRequestFailure,
  semanticRequestFingerprint,
} from "./semanticIdempotencySemantics";
