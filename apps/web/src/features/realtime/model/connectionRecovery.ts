import type { RecoveryReason } from "./contracts";
import type {
  ConnectionEffect,
  ConnectionEvent,
  ConnectionMachineConfig,
  ConnectionState,
  ConnectionTransition,
} from "./connectionTypes";

function publish(
  state: ConnectionState,
  effects: ConnectionEffect[],
): ConnectionTransition {
  return { state, effects: [...effects, { kind: "publishStatus" }] };
}

function snapshotFailureTransition(
  event: ConnectionEvent,
  state: ConnectionState,
  cursor: string | undefined,
  config: ConnectionMachineConfig,
): ConnectionTransition | null {
  if (
    event.type !== "snapshot_failure" ||
    state.kind !== "snapshot_recovering"
  ) {
    return null;
  }
  const max = config.maxRetryCount ?? Number.POSITIVE_INFINITY;
  if (state.attempt >= max) {
    return publish(
      { kind: "closed", cursor },
      [{ kind: "closeSource" }, { kind: "cancelRetry" }],
    );
  }
  const delayMs = config.retryDelay(state.attempt);
  return publish(
    {
      ...state,
      attempt: state.attempt + 1,
      retryAt: event.at + delayMs,
    },
    [{ kind: "closeSource" }, { kind: "scheduleRetry", delayMs }],
  );
}

function snapshotSuccessTransition(
  event: ConnectionEvent,
  state: ConnectionState,
): ConnectionTransition | null {
  if (
    event.type !== "snapshot_success" ||
    state.kind !== "snapshot_recovering"
  ) {
    return null;
  }
  const cursor = event.cursor ?? state.cursor;
  return publish(
    {
      kind: "connecting",
      attempt: 0,
      cursor,
      snapshotReady: true,
    },
    [
      { kind: "cancelRetry" },
      { kind: "closeSource" },
      { kind: "openSource", cursor },
    ],
  );
}

function snapshotRetryTransition(
  event: ConnectionEvent,
  state: ConnectionState,
): ConnectionTransition | null {
  if (
    event.type !== "retry_timer" ||
    state.kind !== "snapshot_recovering"
  ) {
    return null;
  }
  return {
    state: { ...state, retryAt: undefined },
    effects: [{ kind: "recoverSnapshot", reason: state.reason }],
  };
}

function recoveryReasonForEvent(
  event: ConnectionEvent,
  cursor: string | undefined,
): RecoveryReason | null {
  if (event.type === "initial_snapshot") {
    return { kind: "initial_snapshot", cursor: event.cursor ?? cursor };
  }
  if (event.type === "epoch_change") {
    return {
      kind: "server_epoch_changed",
      epoch: event.epoch,
      cursor: event.cursor ?? cursor,
    };
  }
  if (event.type === "recovery_required") {
    return {
      kind: "recovery_required",
      reason: event.reason,
      cursor: event.cursor ?? cursor,
    };
  }
  if (event.type === "replay_gap") {
    return {
      kind: "replay_gap",
      reason: event.reason,
      cursor: event.cursor ?? cursor,
    };
  }
  return null;
}

function startRecoveryTransition(
  event: ConnectionEvent,
  cursor: string | undefined,
): ConnectionTransition | null {
  const reason = recoveryReasonForEvent(event, cursor);
  if (!reason) return null;
  return publish(
    {
      kind: "snapshot_recovering",
      reason,
      attempt: 0,
      cursor,
    },
    [
      { kind: "cancelRetry" },
      { kind: "closeSource" },
      { kind: "recoverSnapshot", reason },
    ],
  );
}

export function recoveryTransition(
  event: ConnectionEvent,
  state: ConnectionState,
  cursor: string | undefined,
  config: ConnectionMachineConfig,
): ConnectionTransition | null {
  return (
    snapshotFailureTransition(event, state, cursor, config) ??
    snapshotSuccessTransition(event, state) ??
    snapshotRetryTransition(event, state) ??
    startRecoveryTransition(event, cursor)
  );
}
