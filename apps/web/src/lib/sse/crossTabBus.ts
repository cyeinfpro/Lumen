import {
  CROSS_TAB_PROTOCOL_VERSION,
  isCrossTabMessage,
  type CrossTabMessage,
  type CrossTabOutgoingMessage,
} from "./crossTabProtocol";

export interface BroadcastChannelLike {
  onmessage: ((event: MessageEvent) => void) | null;
  postMessage(message: unknown): void;
  close(): void;
}

export type BroadcastChannelFactory = (name: string) => BroadcastChannelLike;

export class CrossTabBus {
  private channel: BroadcastChannelLike | null = null;
  private listeners = new Set<(message: CrossTabMessage) => void>();
  readonly channelKey: string;
  private readonly tabId: string;
  private readonly factory?: BroadcastChannelFactory;

  constructor(
    channelKey: string,
    tabId: string,
    factory?: BroadcastChannelFactory,
  ) {
    this.channelKey = channelKey;
    this.tabId = tabId;
    this.factory = factory;
  }

  start(): void {
    if (this.channel) return;
    const factory =
      this.factory ??
      (typeof BroadcastChannel === "undefined"
        ? undefined
        : (name: string) => new BroadcastChannel(name));
    if (!factory) return;
    this.channel = factory(`lumen:sse:v2:${hashKey(this.channelKey)}`);
    this.channel.onmessage = (event) => {
      if (!isCrossTabMessage(event.data, this.channelKey)) return;
      if (event.data.sender === this.tabId) return;
      for (const listener of this.listeners) listener(event.data);
    };
  }

  available(): boolean {
    return this.channel !== null;
  }

  subscribe(listener: (message: CrossTabMessage) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  post(
    message: CrossTabOutgoingMessage,
    now = Date.now(),
  ): void {
    this.channel?.postMessage({
      ...message,
      version: CROSS_TAB_PROTOCOL_VERSION,
      channelKey: this.channelKey,
      sender: this.tabId,
      sentAt: now,
    } as CrossTabMessage);
  }

  close(): void {
    if (!this.channel) return;
    this.channel.onmessage = null;
    this.channel.close();
    this.channel = null;
    this.listeners.clear();
  }
}

function hashKey(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}
