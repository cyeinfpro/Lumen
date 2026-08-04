export const TRANSACTIONAL_LOCK_RECORD_VERSION = 1;

export type TransactionalLockRecord = {
  version: typeof TRANSACTIONAL_LOCK_RECORD_VERSION;
  name: string;
  owner: string | null;
  leaseId: string | null;
  fence: number;
  revision: number;
  updatedAt: number;
  expiresAt: number;
};

export type TransactionalLockChange<T> = {
  record?: TransactionalLockRecord;
  value: T;
};

export type TransactionalLockStore = {
  transact: <T>(
    name: string,
    operation: (record: unknown) => TransactionalLockChange<T>,
    rollback?: () => void,
    signal?: AbortSignal,
  ) => Promise<T>;
  close: () => void;
};

export type TransactionalLockStoreFactory =
  (signal: AbortSignal) => Promise<TransactionalLockStore>;
