import {
  durabilityError,
  type DurableStorage,
  type ExclusiveLockGuard,
} from "./semanticIdempotencyPersistence";

export function readStorageRaw(
  storage: DurableStorage,
  key: string,
): string | null {
  try {
    return storage.getItem(key);
  } catch (error) {
    throw durabilityError("shared idempotency storage read failed", error);
  }
}

function restoreIfUnchanged(
  storage: DurableStorage,
  key: string,
  expected: string | null,
  prior: string | null,
): void {
  if (readStorageRaw(storage, key) !== expected) return;
  try {
    if (prior === null) storage.removeItem(key);
    else storage.setItem(key, prior);
  } catch (error) {
    throw durabilityError("shared idempotency rollback failed", error);
  }
  if (readStorageRaw(storage, key) !== prior) {
    throw durabilityError("shared idempotency rollback was not durable");
  }
}

export async function writeStorageRaw(
  storage: DurableStorage,
  key: string,
  value: string,
  guard: ExclusiveLockGuard,
): Promise<void> {
  let prior: string | null = null;
  let attempted = false;
  const rollback = () => {
    if (attempted) restoreIfUnchanged(storage, key, value, prior);
  };
  await guard.runMutation(
    () => {
      prior = readStorageRaw(storage, key);
      attempted = true;
      try {
        storage.setItem(key, value);
      } catch (error) {
        rollback();
        throw durabilityError("shared idempotency storage write failed", error);
      }
      if (readStorageRaw(storage, key) !== value) {
        rollback();
        throw durabilityError(
          "shared idempotency storage write was not durable",
        );
      }
    },
    rollback,
  );
}

export async function removeStorageRaw(
  storage: DurableStorage,
  key: string,
  guard: ExclusiveLockGuard,
): Promise<void> {
  let prior: string | null = null;
  let attempted = false;
  const rollback = () => {
    if (attempted) restoreIfUnchanged(storage, key, null, prior);
  };
  await guard.runMutation(
    () => {
      prior = readStorageRaw(storage, key);
      attempted = true;
      try {
        storage.removeItem(key);
      } catch (error) {
        rollback();
        throw durabilityError(
          "shared idempotency storage removal failed",
          error,
        );
      }
      if (readStorageRaw(storage, key) !== null) {
        rollback();
        throw durabilityError(
          "shared idempotency storage removal was not durable",
        );
      }
    },
    rollback,
  );
}

export function enumerateStorageKeys(
  storage: DurableStorage,
  prefix: string,
  required: boolean,
): string[] | null {
  if (
    typeof storage.key !== "function" ||
    typeof storage.length !== "number"
  ) {
    if (required) {
      throw durabilityError("shared idempotency storage cannot enumerate keys");
    }
    return null;
  }
  try {
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(prefix)) keys.push(key);
    }
    return keys;
  } catch (error) {
    if (required) {
      throw durabilityError("shared idempotency key enumeration failed", error);
    }
    return null;
  }
}
