import {
  REALTIME_SCHEMA_VERSION,
  isControlEventName,
  type ParsedRealtimeEvent,
  type RealtimeControlEvent,
  type RealtimeDomainEvent,
} from "./contracts";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function version(value: Record<string, unknown>): number {
  const raw = value.schema_version ?? value.version;
  return raw === undefined ? REALTIME_SCHEMA_VERSION : Number(raw);
}

function controlEvent(
  name: string,
  payload: Record<string, unknown>,
  cursor?: string,
): RealtimeControlEvent | null {
  if (!isControlEventName(name)) return null;
  if (name === "replay_truncated") {
    return {
      kind: "control",
      type: name,
      version: REALTIME_SCHEMA_VERSION,
      reason: text(payload.reason) ?? "too_many_events",
      cursor: text(payload.cursor) ?? cursor,
      limit:
        typeof payload.limit === "number" && Number.isFinite(payload.limit)
          ? payload.limit
          : undefined,
    };
  }
  if (name === "server_epoch_changed") {
    const epoch = text(payload.epoch);
    return epoch
      ? {
          kind: "control",
          type: name,
          version: REALTIME_SCHEMA_VERSION,
          epoch,
          cursor: text(payload.cursor) ?? cursor,
        }
      : null;
  }
  if (name === "auth_invalidated") {
    return { kind: "control", type: name, version: REALTIME_SCHEMA_VERSION };
  }
  return {
    kind: "control",
    type: name,
    version: REALTIME_SCHEMA_VERSION,
    cursor: text(payload.cursor) ?? cursor,
  };
}

export function parseRealtimeEvent(input: {
  name: string;
  data: unknown;
  cursor?: string;
  allowedDomainEvents: ReadonlySet<string>;
}): ParsedRealtimeEvent {
  let decoded = input.data;
  if (typeof decoded === "string") {
    try {
      decoded = JSON.parse(decoded);
    } catch {
      return { kind: "invalid", reason: "invalid_json" };
    }
  }
  const payload = record(decoded);
  if (!payload) return { kind: "invalid", reason: "invalid_shape" };
  if (version(payload) !== REALTIME_SCHEMA_VERSION) {
    return { kind: "invalid", reason: "unknown_version" };
  }
  const control = controlEvent(input.name, payload, input.cursor);
  if (control) return { kind: "event", event: control };
  if (isControlEventName(input.name)) {
    return { kind: "invalid", reason: "invalid_shape" };
  }
  if (!input.allowedDomainEvents.has(input.name)) {
    return { kind: "unknown", type: input.name };
  }
  const event: RealtimeDomainEvent = {
    kind: "domain",
    type: input.name,
    version: REALTIME_SCHEMA_VERSION,
    payload,
    cursor: input.cursor,
  };
  return { kind: "event", event };
}
