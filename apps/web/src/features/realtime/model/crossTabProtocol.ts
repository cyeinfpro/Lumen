import type {
  RealtimeControlEvent,
  RealtimeDomainEvent,
} from "./contracts";

export const CROSS_TAB_PROTOCOL_VERSION = 2 as const;

type BaseMessage = {
  version: typeof CROSS_TAB_PROTOCOL_VERSION;
  channelKey: string;
  sender: string;
  sentAt: number;
};

export type CrossTabMessage =
  | (BaseMessage & { type: "hello" })
  | (BaseMessage & { type: "leader_heartbeat" })
  | (BaseMessage & { type: "leader_goodbye" })
  | (BaseMessage & { type: "status"; status: string })
  | (BaseMessage & { type: "manual_reconnect" })
  | (BaseMessage & { type: "domain_event"; event: RealtimeDomainEvent })
  | (BaseMessage & {
      type: "control_event";
      event: RealtimeControlEvent;
      recoveryId?: string;
    })
  | (BaseMessage & {
      type: "recovery_complete";
      recoveryId: string;
      cursor?: string;
      syncedAt: number;
    })
  | (BaseMessage & {
      type: "recovery_failed";
      recoveryId: string;
      reason: string;
    });

export type CrossTabOutgoingMessage =
  CrossTabMessage extends infer TMessage
    ? TMessage extends CrossTabMessage
      ? Omit<
          TMessage,
          "version" | "channelKey" | "sender" | "sentAt"
        >
      : never
    : never;

export function isCrossTabMessage(
  value: unknown,
  channelKey: string,
): value is CrossTabMessage {
  if (!value || typeof value !== "object") return false;
  const raw = value as Partial<CrossTabMessage>;
  return (
    raw.version === CROSS_TAB_PROTOCOL_VERSION &&
    raw.channelKey === channelKey &&
    typeof raw.sender === "string" &&
    typeof raw.sentAt === "number" &&
    typeof raw.type === "string"
  );
}
