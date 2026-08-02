import { createBroadcastChannel } from "@/shared/realtime/browser";

export const AUTH_SESSION_CHANGE_PROTOCOL_VERSION = 1 as const;
const AUTH_SESSION_CHANGE_CHANNEL = "lumen:auth-session:v1";

type AuthSessionChange = {
  version: typeof AUTH_SESSION_CHANGE_PROTOCOL_VERSION;
  type: "session_changed";
  sender: string;
  sentAt: number;
};

function senderId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

const sender = senderId();

function isAuthSessionChange(value: unknown): value is AuthSessionChange {
  if (!value || typeof value !== "object") return false;
  const message = value as Partial<AuthSessionChange>;
  return (
    message.version === AUTH_SESSION_CHANGE_PROTOCOL_VERSION &&
    message.type === "session_changed" &&
    typeof message.sender === "string" &&
    typeof message.sentAt === "number"
  );
}

export function notifyAuthSessionChanged(): void {
  if (typeof BroadcastChannel === "undefined") return;
  try {
    const channel = createBroadcastChannel(AUTH_SESSION_CHANGE_CHANNEL);
    channel.postMessage({
      version: AUTH_SESSION_CHANGE_PROTOCOL_VERSION,
      type: "session_changed",
      sender,
      sentAt: Date.now(),
    } satisfies AuthSessionChange);
    channel.close();
  } catch {
    // Cross-tab notification is best effort; local auth safety still applies.
  }
}

export function subscribeToAuthSessionChanges(
  listener: () => void,
): () => void {
  if (typeof BroadcastChannel === "undefined") return () => undefined;
  try {
    const channel = createBroadcastChannel(AUTH_SESSION_CHANGE_CHANNEL);
    channel.onmessage = (event) => {
      if (isAuthSessionChange(event.data) && event.data.sender !== sender) {
        listener();
      }
    };
    return () => {
      channel.onmessage = null;
      channel.close();
    };
  } catch {
    return () => undefined;
  }
}
