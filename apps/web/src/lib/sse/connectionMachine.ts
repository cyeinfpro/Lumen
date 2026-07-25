import type { RecoveryReason } from "./contracts";

export type ConnectionState =
  | { kind: "idle"; cursor?: string }
  | { kind: "connecting"; attempt: number; cursor?: string }
  | { kind: "open"; cursor?: string; openedAt: number }
  | { kind: "recovering"; reason: RecoveryReason; cursor?: string }
  | { kind: "backoff"; attempt: number; retryAt: number; cursor?: string }
  | { kind: "offline"; cursor?: string }
  | { kind: "unauthorized" }
  | { kind: "closed"; cursor?: string };

export type ConnectionEvent =
  | { type: "start" }
  | { type: "open"; at: number }
  | { type: "error"; at: number }
  | { type: "retry_timer" }
  | { type: "offline" }
  | { type: "online" }
  | { type: "hidden" }
  | { type: "visible" }
  | { type: "replay_gap"; reason: string; cursor?: string }
  | { type: "epoch_change"; epoch: string; cursor?: string }
  | { type: "snapshot_success"; cursor?: string }
  | { type: "snapshot_failure" }
  | { type: "unauthorized" }
  | { type: "manual_reconnect" }
  | { type: "cursor"; cursor: string }
  | { type: "stop" };

export type ConnectionEffect =
  | { kind: "openSource"; cursor?: string }
  | { kind: "closeSource" }
  | { kind: "scheduleRetry"; delayMs: number }
  | { kind: "cancelRetry" }
  | { kind: "recoverSnapshot"; reason: RecoveryReason }
  | { kind: "publishStatus" };

export type ConnectionMachineConfig = {
  now: () => number;
  retryDelay: (attempt: number) => number;
  maxRetryCount?: number;
};

export type ConnectionTransition = {
  state: ConnectionState;
  effects: ConnectionEffect[];
};

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
  const recovery = recoveryTransition(event, state, cursor);
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

function recoveryTransition(
  event: ConnectionEvent,
  state: ConnectionState,
  cursor?: string,
): ConnectionTransition | null {
  if (event.type === "snapshot_failure" && state.kind === "recovering") {
    return publish(state, []);
  }
  if (event.type === "snapshot_success" && state.kind === "recovering") {
    const nextCursor = event.cursor ?? state.cursor;
    return publish(
      { kind: "connecting", attempt: 0, cursor: nextCursor },
      [{ kind: "openSource", cursor: nextCursor }],
    );
  }
  if (event.type !== "replay_gap" && event.type !== "epoch_change") return null;
  const reason: RecoveryReason =
    event.type === "replay_gap"
      ? {
          kind: "replay_gap",
          reason: event.reason,
          cursor: event.cursor ?? cursor,
        }
      : {
          kind: "server_epoch_changed",
          epoch: event.epoch,
          cursor: event.cursor ?? cursor,
        };
  return publish(
    { kind: "recovering", reason, cursor },
    [
      { kind: "cancelRetry" },
      { kind: "closeSource" },
      { kind: "recoverSnapshot", reason },
    ],
  );
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
    return publish(
      { kind: "open", cursor, openedAt: event.at },
      [{ kind: "cancelRetry" }],
    );
  }
  if (event.type === "error") return errorTransition(state, event.at, config);
  if (event.type === "retry_timer" && state.kind === "backoff") {
    return publish(
      { kind: "connecting", attempt: state.attempt, cursor },
      [{ kind: "openSource", cursor }],
    );
  }
  if (!isConnectEvent(event)) return { state, effects: [] };
  if (state.kind === "unauthorized") return { state, effects: [] };
  return publish(
    { kind: "connecting", attempt: 0, cursor },
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
