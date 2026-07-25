export const REALTIME_SCHEMA_VERSION = 1 as const;

export const CONTROL_EVENT_NAMES = [
  "replay_truncated",
  "server_epoch_changed",
  "auth_invalidated",
  "heartbeat",
] as const;

export type RealtimeControlEventName = (typeof CONTROL_EVENT_NAMES)[number];

export type ReplayTruncatedEvent = {
  kind: "control";
  type: "replay_truncated";
  version: typeof REALTIME_SCHEMA_VERSION;
  reason: string;
  cursor?: string;
  limit?: number;
};

export type ServerEpochChangedEvent = {
  kind: "control";
  type: "server_epoch_changed";
  version: typeof REALTIME_SCHEMA_VERSION;
  epoch: string;
  cursor?: string;
};

export type AuthInvalidatedEvent = {
  kind: "control";
  type: "auth_invalidated";
  version: typeof REALTIME_SCHEMA_VERSION;
};

export type StreamHeartbeatEvent = {
  kind: "control";
  type: "heartbeat";
  version: typeof REALTIME_SCHEMA_VERSION;
  cursor?: string;
};

export type RealtimeControlEvent =
  | ReplayTruncatedEvent
  | ServerEpochChangedEvent
  | AuthInvalidatedEvent
  | StreamHeartbeatEvent;

export type RealtimeDomainEvent<TType extends string = string> = {
  kind: "domain";
  type: TType;
  version: typeof REALTIME_SCHEMA_VERSION;
  payload: Record<string, unknown>;
  cursor?: string;
};

export type ParsedRealtimeEvent =
  | { kind: "event"; event: RealtimeControlEvent | RealtimeDomainEvent }
  | {
      kind: "invalid";
      reason: "invalid_json" | "invalid_shape" | "unknown_version";
      detail?: string;
    }
  | { kind: "unknown"; type: string };

export type RecoveryReason =
  | { kind: "replay_gap"; reason: string; cursor?: string }
  | { kind: "server_epoch_changed"; epoch: string; cursor?: string };

export function isControlEventName(
  value: string,
): value is RealtimeControlEventName {
  return (CONTROL_EVENT_NAMES as readonly string[]).includes(value);
}
