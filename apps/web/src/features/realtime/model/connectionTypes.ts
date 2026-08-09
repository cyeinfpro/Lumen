import type { RecoveryReason } from "./contracts";

export type ConnectionState =
  | { kind: "idle"; cursor?: string }
  | {
      kind: "connecting";
      attempt: number;
      cursor?: string;
      snapshotReady: boolean;
    }
  | { kind: "open"; cursor?: string; openedAt: number }
  | {
      kind: "snapshot_recovering";
      reason: RecoveryReason;
      attempt: number;
      retryAt?: number;
      cursor?: string;
    }
  | {
      kind: "backoff";
      attempt: number;
      retryAt: number;
      cursor?: string;
      snapshotReady: boolean;
    }
  | { kind: "offline"; cursor?: string }
  | { kind: "unauthorized" }
  | { kind: "closed"; cursor?: string };

export type ConnectionEvent =
  | { type: "start" }
  | { type: "open"; at: number; snapshotRequired?: boolean }
  | { type: "error"; at: number }
  | { type: "retry_timer" }
  | { type: "offline" }
  | { type: "online" }
  | { type: "hidden" }
  | { type: "visible" }
  | { type: "replay_gap"; reason: string; cursor?: string }
  | { type: "recovery_required"; reason: string; cursor?: string }
  | { type: "epoch_change"; epoch: string; cursor?: string }
  | { type: "snapshot_success"; cursor?: string }
  | { type: "snapshot_failure"; at: number }
  | { type: "initial_snapshot"; cursor?: string }
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
