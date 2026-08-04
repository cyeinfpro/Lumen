import type { PersistedPendingOperation } from "./semanticIdempotencyPersistence";

export type LeaseOwnership = "created" | "borrowed";

export type PendingEntry = {
  key: string;
  generation: number;
  expiresAt: number;
  durable: boolean;
  identityEpoch: number;
  namespace: string | null;
  ownerToken: string | null;
  submitted: boolean;
  shared: boolean;
};

export type SemanticIdempotencyLease = Readonly<{
  fingerprint: string;
  key: string;
  generation: number;
  expiresAt: number;
  durable: boolean;
  identityEpoch: number;
  namespace: string | null;
  participantToken: string;
  ownership: LeaseOwnership;
}>;

export function pendingEntry(
  operation: PersistedPendingOperation,
  identityEpoch: number,
  namespace: string,
): PendingEntry {
  return {
    key: operation.key,
    generation: operation.generation,
    expiresAt: operation.expiresAt,
    durable: true,
    identityEpoch,
    namespace,
    ownerToken: operation.ownerToken ?? null,
    submitted: operation.submitted === true,
    shared: operation.shared === true,
  };
}

export function semanticLease(
  fingerprint: string,
  entry: PendingEntry,
  participantToken: string,
  ownership: LeaseOwnership,
): SemanticIdempotencyLease {
  return {
    fingerprint,
    key: entry.key,
    generation: entry.generation,
    expiresAt: entry.expiresAt,
    durable: entry.durable,
    identityEpoch: entry.identityEpoch,
    namespace: entry.namespace,
    participantToken,
    ownership,
  };
}

export function pendingMatchesLease(
  pending: PersistedPendingOperation | undefined,
  lease: SemanticIdempotencyLease,
): pending is PersistedPendingOperation {
  return (
    pending?.key === lease.key &&
    pending.generation === lease.generation
  );
}

export function creatorCanDiscard(
  pending: PersistedPendingOperation,
  lease: SemanticIdempotencyLease,
): boolean {
  return (
    lease.ownership === "created" &&
    pending.ownerToken === lease.participantToken &&
    pending.submitted === false &&
    pending.shared === false
  );
}
