import {
  INITIAL_SNAPSHOT_RECOVERY_REASON,
  type RealtimeControlEvent,
  type RecoveryReason,
} from "./contracts";
import {
  ReplayCoordinator,
  type SnapshotAdapter,
  type SnapshotExecutionContext,
  type SnapshotResult,
} from "./replayCoordinator";

type SnapshotProvider = {
  recoverSnapshot?: SnapshotAdapter;
};

export function isInitialSnapshotReason(reason: RecoveryReason): boolean {
  return (
    reason.kind === "initial_snapshot" ||
    (reason.kind === "recovery_required" &&
      reason.reason === INITIAL_SNAPSHOT_RECOVERY_REASON)
  );
}

export function recoveryReason(
  event: RealtimeControlEvent,
): RecoveryReason | null {
  if (event.type === "replay_truncated") {
    return {
      kind: "replay_gap",
      reason: event.reason,
      cursor: event.cursor,
    };
  }
  if (event.type === "recovery_required") {
    if (event.reason === INITIAL_SNAPSHOT_RECOVERY_REASON) {
      return { kind: "initial_snapshot", cursor: event.cursor };
    }
    return {
      kind: "recovery_required",
      reason: event.reason,
      cursor: event.cursor,
    };
  }
  if (event.type === "server_epoch_changed") {
    return {
      kind: "server_epoch_changed",
      epoch: event.epoch,
      cursor: event.cursor,
    };
  }
  return null;
}

export function recoveryControlEvent(
  reason: RecoveryReason,
): RealtimeControlEvent {
  if (reason.kind === "server_epoch_changed") {
    return {
      kind: "control",
      type: "server_epoch_changed",
      version: 1,
      epoch: reason.epoch,
      cursor: reason.cursor,
    };
  }
  return {
    kind: "control",
    type: "recovery_required",
    version: 1,
    reason:
      reason.kind === "initial_snapshot"
        ? INITIAL_SNAPSHOT_RECOVERY_REASON
        : reason.reason,
    cursor: reason.cursor,
  };
}

export function snapshotAdapters(
  providers: Iterable<SnapshotProvider>,
): SnapshotAdapter[] {
  return [
    ...new Set(
      [...providers]
        .map((provider) => provider.recoverSnapshot)
        .filter((adapter): adapter is SnapshotAdapter => Boolean(adapter)),
    ),
  ];
}

export async function executeSnapshotRecovery(options: {
  adapters: SnapshotAdapter[];
  reason: RecoveryReason;
  signal: AbortSignal;
  context: SnapshotExecutionContext;
  now: () => number;
}): Promise<SnapshotResult> {
  const { adapters, reason, signal, context, now } = options;
  if (adapters.length === 0) {
    signal.throwIfAborted();
    if (!isInitialSnapshotReason(reason)) {
      throw new Error("snapshot_adapter_unavailable");
    }
    return { syncedAt: now() };
  }
  const coordinator = new ReplayCoordinator(
    async (scopes, currentReason, currentSignal, currentContext) => {
      currentSignal.throwIfAborted();
      const results = await Promise.all(
        adapters.map((adapter) =>
          adapter(
            scopes,
            currentReason,
            currentSignal,
            currentContext,
          ),
        ),
      );
      currentSignal.throwIfAborted();
      return {
        cursor:
          results.find((result) => result.cursor)?.cursor ??
          ("cursor" in currentReason ? currentReason.cursor : undefined),
        syncedAt: now(),
      };
    },
  );
  return coordinator.recover(reason, signal, context);
}
