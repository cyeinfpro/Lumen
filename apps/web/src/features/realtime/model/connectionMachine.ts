import type { RecoveryReason } from "./contracts";
import { recoveryTransition } from "./connectionRecovery";
import type {
  ConnectionEffect,
  ConnectionEvent,
  ConnectionMachineConfig,
  ConnectionState,
  ConnectionTransition,
} from "./connectionTypes";

export type {
  ConnectionEffect,
  ConnectionEvent,
  ConnectionMachineConfig,
  ConnectionState,
  ConnectionTransition,
} from "./connectionTypes";

function cursorOf(state: ConnectionState): string | undefined {
  return "cursor" in state ? state.cursor : undefined;
}

function publish(
  state: ConnectionState,
  effects: ConnectionEffect[],
): ConnectionTransition {
  return { state, effects: [...effects, { kind: "publishStatus" }] };
}

export function transitionConnection(
  state: ConnectionState,
  event: ConnectionEvent,
  config: ConnectionMachineConfig,
): ConnectionTransition {
  const cursor = cursorOf(state);
  if (event.type === "cursor") {
    return {
      state: "cursor" in state ? { ...state, cursor: event.cursor } : state,
      effects: [],
    };
  }
  const terminal = terminalTransition(event, cursor);
  if (terminal) return terminal;
  const recovery = recoveryTransition(event, state, cursor, config);
  if (recovery) return recovery;
  return activeTransition(state, event, config);
}

function terminalTransition(
  event: ConnectionEvent,
  cursor?: string,
): ConnectionTransition | null {
  const effects: ConnectionEffect[] = [
    { kind: "cancelRetry" },
    { kind: "closeSource" },
  ];
  if (event.type === "stop" || event.type === "hidden") {
    return publish({ kind: "closed", cursor }, effects);
  }
  if (event.type === "unauthorized") {
    return publish({ kind: "unauthorized" }, effects);
  }
  if (event.type === "offline") {
    return publish({ kind: "offline", cursor }, effects);
  }
  return null;
}

function errorTransition(
  state: ConnectionState,
  at: number,
  config: ConnectionMachineConfig,
): ConnectionTransition {
  const cursor = cursorOf(state);
  const priorAttempt =
    state.kind === "connecting" || state.kind === "backoff"
      ? state.attempt
      : 0;
  const snapshotReady =
    state.kind === "connecting" || state.kind === "backoff"
      ? state.snapshotReady
      : false;
  const max = config.maxRetryCount ?? Number.POSITIVE_INFINITY;
  if (priorAttempt >= max) {
    return publish(
      { kind: "closed", cursor },
      [{ kind: "closeSource" }, { kind: "cancelRetry" }],
    );
  }
  const delayMs = config.retryDelay(priorAttempt);
  return publish(
    {
      kind: "backoff",
      attempt: priorAttempt + 1,
      retryAt: at + delayMs,
      cursor,
      snapshotReady,
    },
    [{ kind: "closeSource" }, { kind: "scheduleRetry", delayMs }],
  );
}

function activeTransition(
  state: ConnectionState,
  event: ConnectionEvent,
  config: ConnectionMachineConfig,
): ConnectionTransition {
  const cursor = cursorOf(state);
  if (event.type === "open") {
    if (state.kind !== "connecting") return { state, effects: [] };
    if (
      event.snapshotRequired &&
      !state.snapshotReady
    ) {
      const reason: RecoveryReason = {
        kind: "initial_snapshot",
        cursor,
      };
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
    return publish(
      { kind: "open", cursor, openedAt: event.at },
      [{ kind: "cancelRetry" }],
    );
  }
  if (event.type === "error") return errorTransition(state, event.at, config);
  if (event.type === "retry_timer" && state.kind === "backoff") {
    return publish(
      {
        kind: "connecting",
        attempt: state.attempt,
        cursor,
        snapshotReady: state.snapshotReady,
      },
      [{ kind: "openSource", cursor }],
    );
  }
  if (!isConnectEvent(event)) return { state, effects: [] };
  if (state.kind === "unauthorized") return { state, effects: [] };
  return publish(
    {
      kind: "connecting",
      attempt: 0,
      cursor,
      snapshotReady: false,
    },
    [
      { kind: "cancelRetry" },
      { kind: "closeSource" },
      { kind: "openSource", cursor },
    ],
  );
}

function isConnectEvent(event: ConnectionEvent): boolean {
  return (
    event.type === "start" ||
    event.type === "online" ||
    event.type === "visible" ||
    event.type === "manual_reconnect"
  );
}
