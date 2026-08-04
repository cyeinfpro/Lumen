import { durabilityError } from "./semanticIdempotencyPersistence";

const DEFAULT_LEASE_MS = 5_000;
const DEFAULT_HEARTBEAT_MS = 1_000;
const DEFAULT_RETRY_MS = 5;
const DEFAULT_MAX_ATTEMPTS = 2_400;
const DEFAULT_MAX_FORWARD_SKEW_MS = 500;

export type StorageLockTimingOptions = {
  leaseMs?: number;
  heartbeatMs?: number;
  retryMs?: number;
  maxAttempts?: number;
  maxForwardSkewMs?: number;
  reapObservationMs?: number;
  quarantineMs?: number;
  monotonicNow?: () => number;
  /** @deprecated Use maxForwardSkewMs. */
  maxClockStepMs?: number;
};

export type ResolvedStorageLockTiming = {
  leaseMs: number;
  heartbeatMs: number;
  retryMs: number;
  maxAttempts: number;
  maxForwardSkewMs: number;
  reapObservationMs: number;
  quarantineMs: number;
  reapClaimGraceMs: number;
  reapClaimMs: number;
};

export type StorageLockClockSample = {
  wall: number;
  monotonic: number;
};

export type StorageLockClock = {
  sample: () => StorageLockClockSample;
};

function positiveSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function nonNegativeSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function nonNegativeFiniteNumber(value: unknown): value is number {
  return Number.isFinite(value) && Number(value) >= 0;
}

function validCoreTiming(
  leaseMs: number,
  heartbeatMs: number,
  retryMs: number,
  maxAttempts: number,
): boolean {
  return (
    positiveSafeInteger(leaseMs) &&
    positiveSafeInteger(heartbeatMs) &&
    heartbeatMs * 2 < leaseMs &&
    positiveSafeInteger(retryMs) &&
    positiveSafeInteger(maxAttempts)
  );
}

function validClockTiming(
  maxForwardSkewMs: number,
  leaseSafetyMargin: number,
  reapObservationMs: number,
  quarantineMs: number,
  heartbeatMs: number,
): boolean {
  return (
    positiveSafeInteger(maxForwardSkewMs) &&
    maxForwardSkewMs < leaseSafetyMargin &&
    positiveSafeInteger(reapObservationMs) &&
    reapObservationMs >= heartbeatMs * 2 &&
    positiveSafeInteger(quarantineMs) &&
    quarantineMs >= reapObservationMs
  );
}

export function resolveStorageLockTiming(
  options: StorageLockTimingOptions,
): ResolvedStorageLockTiming {
  const leaseMs = options.leaseMs ?? DEFAULT_LEASE_MS;
  const heartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
  const retryMs = options.retryMs ?? DEFAULT_RETRY_MS;
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;
  const maxForwardSkewMs =
    options.maxForwardSkewMs ??
    options.maxClockStepMs ??
    DEFAULT_MAX_FORWARD_SKEW_MS;
  const reapObservationMs =
    options.reapObservationMs ?? heartbeatMs * 2;
  const quarantineMs = options.quarantineMs ?? leaseMs;
  const leaseSafetyMargin = leaseMs - heartbeatMs * 2;
  if (
    !validCoreTiming(leaseMs, heartbeatMs, retryMs, maxAttempts) ||
    !validClockTiming(
      maxForwardSkewMs,
      leaseSafetyMargin,
      reapObservationMs,
      quarantineMs,
      heartbeatMs,
    )
  ) {
    throw new TypeError("invalid storage lock timing");
  }
  return {
    leaseMs,
    heartbeatMs,
    retryMs,
    maxAttempts,
    maxForwardSkewMs,
    reapObservationMs,
    quarantineMs,
    reapClaimGraceMs: heartbeatMs * 2,
    reapClaimMs: leaseMs * 2,
  };
}

function defaultMonotonicNow(): number {
  const performanceClock = globalThis.performance;
  return performanceClock && typeof performanceClock.now === "function"
    ? performanceClock.now()
    : Date.now();
}

export function createStorageLockClock(
  wallNow: () => number,
  monotonicNow: (() => number) | undefined,
  maxForwardSkewMs: number,
): StorageLockClock {
  const readMonotonic = monotonicNow ?? defaultMonotonicNow;
  let origin: StorageLockClockSample | null = null;
  let previous: StorageLockClockSample | null = null;
  return {
    sample() {
      let wall: number;
      let monotonic: number;
      try {
        wall = wallNow();
        monotonic = readMonotonic();
      } catch (error) {
        throw durabilityError("storage lock clock failed", error);
      }
      if (
        !nonNegativeSafeInteger(wall) ||
        !nonNegativeFiniteNumber(monotonic)
      ) {
        throw durabilityError("storage lock clock is invalid");
      }
      const current = { wall, monotonic };
      if (previous) {
        const wallDelta = wall - previous.wall;
        const monotonicDelta = monotonic - previous.monotonic;
        if (
          wallDelta < 0 ||
          monotonicDelta < 0 ||
          wallDelta - monotonicDelta >= maxForwardSkewMs
        ) {
          throw durabilityError("storage lock clock changed unexpectedly");
        }
      }
      if (
        origin &&
        wall -
          origin.wall -
          (monotonic - origin.monotonic) >=
          maxForwardSkewMs
      ) {
        throw durabilityError("storage lock clock changed unexpectedly");
      }
      origin ??= current;
      previous = current;
      return current;
    },
  };
}
