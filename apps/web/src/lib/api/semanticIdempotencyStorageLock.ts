import {
  durabilityError,
  type ExclusiveLockGuard,
  type ExclusiveLockRequest,
} from "./semanticIdempotencyPersistence";
import {
  createStorageLockClock,
  resolveStorageLockTiming,
  type StorageLockClock,
  type StorageLockTimingOptions,
} from "./semanticIdempotencyStorageClock";
import { indexedDbStoreFactory } from "./semanticIdempotencyIndexedDb";
import {
  TRANSACTIONAL_LOCK_RECORD_VERSION,
  type TransactionalLockChange,
  type TransactionalLockRecord,
  type TransactionalLockStore,
  type TransactionalLockStoreFactory,
} from "./semanticIdempotencyStorageLockTypes";

export type { StorageLockTimingOptions } from "./semanticIdempotencyStorageClock";
export type {
  TransactionalLockChange,
  TransactionalLockRecord,
  TransactionalLockStore,
  TransactionalLockStoreFactory,
} from "./semanticIdempotencyStorageLockTypes";

export type TransactionalLockRuntimeOptions = StorageLockTimingOptions & {
  wait?: (delayMs: number) => Promise<void>;
  operationDeadlineMs?: number;
};

type OwnedLease = {
  record: TransactionalLockRecord;
  tail: Promise<void>;
  failure: unknown | null;
};

type ExpiryObservation = {
  signature: string;
  wall: number;
  monotonic: number;
};

function defaultWait(delayMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

function unrefTimer(
  timer: ReturnType<typeof setInterval> | ReturnType<typeof setTimeout>,
): void {
  if (
    typeof timer === "object" &&
    timer !== null &&
    "unref" in timer
  ) {
    (timer as { unref: () => void }).unref();
  }
}

function abortController(): AbortController {
  try {
    if (typeof AbortController !== "function") {
      throw new Error("AbortController is unavailable");
    }
    return new AbortController();
  } catch (error) {
    throw durabilityError("storage lock cancellation is unavailable", error);
  }
}

function operationDeadline(
  timing: ReturnType<typeof resolveStorageLockTiming>,
  configured: number | undefined,
): number {
  const safetyMargin = timing.leaseMs - timing.heartbeatMs * 2;
  const deadline =
    configured ??
    Math.min(1_500, Math.max(1, Math.floor(safetyMargin / 2)));
  if (
    !Number.isSafeInteger(deadline) ||
    deadline <= 0 ||
    deadline >= safetyMargin
  ) {
    throw new TypeError("invalid storage lock operation deadline");
  }
  return deadline;
}

function validToken(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 128 &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

function validLockName(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512
  );
}

function nonNegativeSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function positiveSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function validRecordFields(
  record: Partial<TransactionalLockRecord>,
  name: string,
): boolean {
  return (
    record.version === TRANSACTIONAL_LOCK_RECORD_VERSION &&
    record.name === name &&
    validLockName(record.name) &&
    nonNegativeSafeInteger(record.fence) &&
    positiveSafeInteger(record.revision) &&
    nonNegativeSafeInteger(record.updatedAt) &&
    nonNegativeSafeInteger(record.expiresAt)
  );
}

function validRecordOwner(
  record: Partial<TransactionalLockRecord>,
): boolean {
  if (record.owner === null && record.leaseId === null) {
    return record.expiresAt === 0;
  }
  return (
    validToken(record.owner) &&
    validToken(record.leaseId) &&
    Number(record.expiresAt) > Number(record.updatedAt)
  );
}

function parsedRecord(
  value: unknown,
  name: string,
): TransactionalLockRecord | null {
  if (value === undefined) return null;
  if (!value || typeof value !== "object") {
    throw durabilityError("indexeddb storage lock state is malformed");
  }
  const record = value as Partial<TransactionalLockRecord>;
  if (!validRecordFields(record, name) || !validRecordOwner(record)) {
    throw durabilityError("indexeddb storage lock state is malformed");
  }
  return record as TransactionalLockRecord;
}

function nextInteger(value: number, description: string): number {
  const next = value + 1;
  if (!Number.isSafeInteger(next) || next <= value) {
    throw durabilityError(`indexeddb storage lock ${description} is exhausted`);
  }
  return next;
}

function safeExpiry(now: number, leaseMs: number): number {
  const expiresAt = now + leaseMs;
  if (!Number.isSafeInteger(expiresAt) || expiresAt <= now) {
    throw durabilityError("indexeddb storage lock lease expiry is invalid");
  }
  return expiresAt;
}

function freshToken(factory: () => string, description: string): string {
  let token: string;
  try {
    token = factory();
  } catch (error) {
    throw durabilityError(
      `indexeddb storage lock ${description} is unavailable`,
      error,
    );
  }
  if (!validToken(token)) {
    throw durabilityError(
      `indexeddb storage lock ${description} is unavailable`,
    );
  }
  return token;
}

function sameLease(
  record: TransactionalLockRecord,
  owned: TransactionalLockRecord,
): boolean {
  return (
    record.name === owned.name &&
    record.owner === owned.owner &&
    record.leaseId === owned.leaseId &&
    record.fence === owned.fence
  );
}

function validateRecordTiming(
  record: TransactionalLockRecord,
  wall: number,
  leaseMs: number,
  maxForwardSkewMs: number,
): void {
  if (record.owner === null) return;
  if (
    record.expiresAt - record.updatedAt > leaseMs ||
    record.updatedAt - wall >= maxForwardSkewMs
  ) {
    throw durabilityError("indexeddb storage lock lease is invalid");
  }
}

function expirySignature(record: TransactionalLockRecord): string {
  return [
    record.owner,
    record.leaseId,
    record.fence,
    record.revision,
    record.updatedAt,
    record.expiresAt,
  ].join(":");
}

function observationIsMature(
  observation: ExpiryObservation | null,
  record: TransactionalLockRecord,
  sample: { wall: number; monotonic: number },
  observationMs: number,
  maxForwardSkewMs: number,
): boolean {
  if (!observation || observation.signature !== expirySignature(record)) {
    return false;
  }
  const wallElapsed = sample.wall - observation.wall;
  const monotonicElapsed = sample.monotonic - observation.monotonic;
  if (
    wallElapsed < 0 ||
    monotonicElapsed < 0 ||
    wallElapsed - monotonicElapsed >= maxForwardSkewMs
  ) {
    throw durabilityError("indexeddb storage lock clock changed unexpectedly");
  }
  return monotonicElapsed >= observationMs;
}

function claimedRecord(
  name: string,
  current: TransactionalLockRecord | null,
  owner: string,
  leaseId: string,
  wall: number,
  leaseMs: number,
): TransactionalLockRecord {
  return {
    version: TRANSACTIONAL_LOCK_RECORD_VERSION,
    name,
    owner,
    leaseId,
    fence: nextInteger(current?.fence ?? 0, "fence"),
    revision: nextInteger(current?.revision ?? 0, "revision"),
    updatedAt: wall,
    expiresAt: safeExpiry(wall, leaseMs),
  };
}

function renewedRecord(
  record: TransactionalLockRecord,
  wall: number,
  leaseMs: number,
): TransactionalLockRecord {
  return {
    ...record,
    revision: nextInteger(record.revision, "revision"),
    updatedAt: wall,
    expiresAt: safeExpiry(wall, leaseMs),
  };
}

function releasedRecord(
  record: TransactionalLockRecord,
  wall: number,
): TransactionalLockRecord {
  return {
    ...record,
    owner: null,
    leaseId: null,
    revision: nextInteger(record.revision, "revision"),
    updatedAt: wall,
    expiresAt: 0,
  };
}

function queueOwned<T>(
  owned: OwnedLease,
  operation: () => Promise<T>,
): Promise<T> {
  const queued = owned.tail.then(operation, operation);
  owned.tail = queued.then(
    () => undefined,
    () => undefined,
  );
  return queued;
}

function closeStore(store: TransactionalLockStore): void {
  try {
    store.close();
  } catch {
    // Closing is best-effort after the operation has already settled.
  }
}

function openStoreBeforeDeadline(
  openStore: TransactionalLockStoreFactory,
  deadlineMs: number,
): Promise<TransactionalLockStore> {
  return new Promise((resolve, reject) => {
    const controller = abortController();
    const timeoutError = durabilityError(
      "indexeddb storage lock open timed out",
    );
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        controller.abort(timeoutError);
      } catch {
        controller.abort();
      }
      reject(timeoutError);
    }, deadlineMs);
    const cleanup = () => clearTimeout(timer);
    let pending: Promise<TransactionalLockStore>;
    try {
      pending = openStore(controller.signal);
    } catch (error) {
      settled = true;
      cleanup();
      reject(durabilityError("indexeddb storage lock open failed", error));
      return;
    }
    Promise.resolve(pending).then(
      (store) => {
        if (settled) {
          closeStore(store);
          return;
        }
        settled = true;
        cleanup();
        resolve(store);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

function transactBeforeDeadline<T>(
  store: TransactionalLockStore,
  name: string,
  operation: (record: unknown) => TransactionalLockChange<T>,
  rollback: (() => void) | undefined,
  deadlineMs: number,
  description: string,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const controller = abortController();
    const timeoutError = durabilityError(
      `indexeddb storage lock ${description} timed out`,
    );
    let settled = false;
    let rolledBack = false;
    const rollbackOnce = () => {
      if (rolledBack || !rollback) return;
      rolledBack = true;
      rollback();
    };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        controller.abort(timeoutError);
      } catch {
        controller.abort();
      }
      try {
        rollbackOnce();
      } catch {
        // The timeout remains authoritative.
      }
      reject(timeoutError);
    }, deadlineMs);
    const cleanup = () => clearTimeout(timer);
    const guardedOperation = (record: unknown) => {
      if (settled || controller.signal.aborted) throw timeoutError;
      return operation(record);
    };
    let pending: Promise<T>;
    try {
      pending = store.transact(
        name,
        guardedOperation,
        rollbackOnce,
        controller.signal,
      );
    } catch (error) {
      settled = true;
      cleanup();
      try {
        rollbackOnce();
      } catch {
        // The original transaction error remains authoritative.
      }
      reject(error);
      return;
    }
    Promise.resolve(pending).then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        try {
          rollbackOnce();
        } catch {
          // The transaction rejection remains authoritative.
        }
        reject(error);
      },
    );
  });
}

async function acquireLease(
  store: TransactionalLockStore,
  name: string,
  owner: string,
  leaseId: string,
  clock: StorageLockClock,
  timing: ReturnType<typeof resolveStorageLockTiming>,
  wait: (delayMs: number) => Promise<void>,
  deadlineMs: number,
): Promise<OwnedLease> {
  let observation: ExpiryObservation | null = null;
  for (let attempt = 0; attempt < timing.maxAttempts; attempt += 1) {
    const claimed = await transactBeforeDeadline(
      store,
      name,
      (stored) => {
        const sample = clock.sample();
        const current = parsedRecord(stored, name);
        if (!current || current.owner === null) {
          const record = claimedRecord(
            name,
            current,
            owner,
            leaseId,
            sample.wall,
            timing.leaseMs,
          );
          return { record, value: record };
        }
        validateRecordTiming(
          current,
          sample.wall,
          timing.leaseMs,
          timing.maxForwardSkewMs,
        );
        if (current.expiresAt > sample.wall) {
          observation = null;
          return { value: null };
        }
        if (
          !observation ||
          observation.signature !== expirySignature(current)
        ) {
          observation = {
            signature: expirySignature(current),
            wall: sample.wall,
            monotonic: sample.monotonic,
          };
          return { value: null };
        }
        if (
          !observationIsMature(
            observation,
            current,
            sample,
            timing.reapObservationMs,
            timing.maxForwardSkewMs,
          )
        ) {
          return { value: null };
        }
        const record = claimedRecord(
          name,
          current,
          owner,
          leaseId,
          sample.wall,
          timing.leaseMs,
        );
        return { record, value: record };
      },
      undefined,
      deadlineMs,
      "acquisition transaction",
    );
    if (claimed) {
      return { record: claimed, tail: Promise.resolve(), failure: null };
    }
    await wait(timing.retryMs);
  }
  throw durabilityError("could not acquire indexeddb storage lock");
}

async function renewOwned(
  store: TransactionalLockStore,
  owned: OwnedLease,
  clock: StorageLockClock,
  leaseMs: number,
  deadlineMs: number,
): Promise<void> {
  await queueOwned(owned, async () => {
    if (owned.failure) throw owned.failure;
    const next = await transactBeforeDeadline(
      store,
      owned.record.name,
      (stored) => {
        const sample = clock.sample();
        const current = parsedRecord(stored, owned.record.name);
        if (
          !current ||
          !sameLease(current, owned.record) ||
          current.expiresAt <= sample.wall
        ) {
          throw durabilityError("indexeddb storage lock ownership was lost");
        }
        const record = renewedRecord(current, sample.wall, leaseMs);
        return { record, value: record };
      },
      undefined,
      deadlineMs,
      "lease renewal",
    );
    owned.record = next;
  });
}

async function runOwnedMutation<T>(
  store: TransactionalLockStore,
  owned: OwnedLease,
  clock: StorageLockClock,
  leaseMs: number,
  deadlineMs: number,
  mutation: () => T,
  rollback?: () => void,
): Promise<T> {
  return queueOwned(owned, async () => {
    if (owned.failure) throw owned.failure;
    const result = await transactBeforeDeadline(
      store,
      owned.record.name,
      (stored) => {
        const sample = clock.sample();
        const current = parsedRecord(stored, owned.record.name);
        if (
          !current ||
          !sameLease(current, owned.record) ||
          current.expiresAt <= sample.wall
        ) {
          throw durabilityError("indexeddb storage lock ownership was lost");
        }
        const value = mutation();
        const record = renewedRecord(current, sample.wall, leaseMs);
        return { record, value: { record, value } };
      },
      rollback,
      deadlineMs,
      "fenced mutation",
    );
    owned.record = result.record;
    return result.value;
  });
}

async function releaseOwned(
  store: TransactionalLockStore,
  owned: OwnedLease,
  clock: StorageLockClock,
  deadlineMs: number,
): Promise<void> {
  await queueOwned(owned, async () => {
    await transactBeforeDeadline(
      store,
      owned.record.name,
      (stored) => {
        const current = parsedRecord(stored, owned.record.name);
        if (!current || !sameLease(current, owned.record)) {
          return { value: undefined };
        }
        const sample = clock.sample();
        return {
          record: releasedRecord(current, sample.wall),
          value: undefined,
        };
      },
      undefined,
      deadlineMs,
      "lease release",
    );
  });
}

async function runOwnedCallback<T>(
  store: TransactionalLockStore,
  owned: OwnedLease,
  clock: StorageLockClock,
  timing: ReturnType<typeof resolveStorageLockTiming>,
  deadlineMs: number,
  callback: (guard?: ExclusiveLockGuard) => T | Promise<T>,
): Promise<T> {
  let heartbeatTask: Promise<void> | null = null;
  const heartbeat = setInterval(() => {
    if (heartbeatTask || owned.failure) return;
    heartbeatTask = renewOwned(
      store,
      owned,
      clock,
      timing.leaseMs,
      deadlineMs,
    )
      .catch((error: unknown) => {
        owned.failure = error;
      })
      .finally(() => {
        heartbeatTask = null;
      });
  }, timing.heartbeatMs);
  unrefTimer(heartbeat);
  const guard: ExclusiveLockGuard = {
    assertCurrent: () =>
      renewOwned(store, owned, clock, timing.leaseMs, deadlineMs),
    runMutation: (mutation, rollback) =>
      runOwnedMutation(
        store,
        owned,
        clock,
        timing.leaseMs,
        deadlineMs,
        mutation,
        rollback,
      ),
  };
  try {
    await guard.assertCurrent();
    const result = await callback(guard);
    if (heartbeatTask) await heartbeatTask;
    if (owned.failure) throw owned.failure;
    await guard.assertCurrent();
    return result;
  } finally {
    clearInterval(heartbeat);
    if (heartbeatTask) await heartbeatTask;
  }
}

export function createTransactionalLockRequest(
  openStore: TransactionalLockStoreFactory,
  now: () => number,
  freshIdentity: () => string,
  runtimeOptions: TransactionalLockRuntimeOptions = {},
): ExclusiveLockRequest {
  const timing = resolveStorageLockTiming(runtimeOptions);
  const wait = runtimeOptions.wait ?? defaultWait;
  const deadlineMs = operationDeadline(
    timing,
    runtimeOptions.operationDeadlineMs,
  );
  return async <T>(
    name: string,
    _options: { mode: "exclusive" },
    callback: (guard?: ExclusiveLockGuard) => T | Promise<T>,
  ): Promise<T> => {
    if (!validLockName(name)) {
      throw durabilityError("indexeddb storage lock name is invalid");
    }
    const store = await openStoreBeforeDeadline(openStore, deadlineMs);
    let clock: StorageLockClock | null = null;
    let owned: OwnedLease | null = null;
    let result: T | undefined;
    let failure: unknown | null = null;
    try {
      clock = createStorageLockClock(
        now,
        runtimeOptions.monotonicNow,
        timing.maxForwardSkewMs,
      );
      const owner = freshToken(freshIdentity, "owner");
      const leaseId = freshToken(freshIdentity, "lease identity");
      owned = await acquireLease(
        store,
        name,
        owner,
        leaseId,
        clock,
        timing,
        wait,
        deadlineMs,
      );
      result = await runOwnedCallback(
        store,
        owned,
        clock,
        timing,
        deadlineMs,
        callback,
      );
    } catch (error) {
      failure = error;
    }
    if (owned && clock) {
      try {
        await releaseOwned(store, owned, clock, deadlineMs);
      } catch (error) {
        failure ??= error;
      }
    }
    closeStore(store);
    if (failure) throw failure;
    return result as T;
  };
}

function indexedDbFactory(
  configured: IDBFactory | null | undefined,
): IDBFactory | null {
  if (configured !== undefined) return configured;
  try {
    return globalThis.indexedDB ?? null;
  } catch {
    return null;
  }
}

export function indexedDbLockRequestOrNull(
  configuredFactory: IDBFactory | null | undefined,
  now: () => number,
  freshIdentity: () => string,
  runtimeOptions: TransactionalLockRuntimeOptions = {},
): ExclusiveLockRequest | null {
  const factory = indexedDbFactory(configuredFactory);
  if (!factory || typeof AbortController !== "function") return null;
  return createTransactionalLockRequest(
    indexedDbStoreFactory(factory),
    now,
    freshIdentity,
    runtimeOptions,
  );
}

export function failClosedLockRequest(): ExclusiveLockRequest {
  return async () => {
    throw durabilityError(
      "reliable cross-context idempotency coordination is unavailable",
    );
  };
}
