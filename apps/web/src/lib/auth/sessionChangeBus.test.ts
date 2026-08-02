import { equal } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../test-support/load-ts-module.mjs";

type Channel = {
  onmessage: ((event: MessageEvent) => void) | null;
  postMessage(message: unknown): void;
  close(): void;
};

class FakeBroadcastHub {
  messages: unknown[] = [];
  private channels = new Map<string, Set<FakeChannel>>();

  create = (name: string): Channel => {
    const channel = new FakeChannel(name, this);
    const channels = this.channels.get(name) ?? new Set<FakeChannel>();
    channels.add(channel);
    this.channels.set(name, channels);
    return channel;
  };

  post(name: string, sender: FakeChannel, message: unknown): void {
    this.messages.push(message);
    for (const channel of this.channels.get(name) ?? []) {
      if (channel !== sender) {
        channel.onmessage?.({ data: message } as MessageEvent);
      }
    }
  }

  close(name: string, channel: FakeChannel): void {
    this.channels.get(name)?.delete(channel);
  }
}

class FakeChannel implements Channel {
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly name: string;
  private readonly hub: FakeBroadcastHub;

  constructor(name: string, hub: FakeBroadcastHub) {
    this.name = name;
    this.hub = hub;
  }

  postMessage(message: unknown): void {
    this.hub.post(this.name, this, message);
  }

  close(): void {
    this.hub.close(this.name, this);
  }
}

test("session changes cross tabs but ignore the sender and malformed payloads", () => {
  const originalBroadcastChannel = Object.getOwnPropertyDescriptor(
    globalThis,
    "BroadcastChannel",
  );
  const hub = new FakeBroadcastHub();
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: FakeChannel,
  });

  try {
    const {
      AUTH_SESSION_CHANGE_PROTOCOL_VERSION,
      notifyAuthSessionChanged,
      subscribeToAuthSessionChanges,
    } = loadTsModule(
      new URL("./sessionChangeBus.ts", import.meta.url),
      {
        "@/shared/realtime/browser": {
          createBroadcastChannel: hub.create,
        },
      },
    ) as {
      AUTH_SESSION_CHANGE_PROTOCOL_VERSION: number;
      notifyAuthSessionChanged(): void;
      subscribeToAuthSessionChanges(listener: () => void): () => void;
    };
    let changes = 0;
    const stop = subscribeToAuthSessionChanges(() => {
      changes += 1;
    });
    const external = hub.create("lumen:auth-session:v1");

    external.postMessage({
      version: AUTH_SESSION_CHANGE_PROTOCOL_VERSION,
      type: "session_changed",
      sender: "other-tab",
      sentAt: 1,
    });
    external.postMessage({ type: "session_changed" });
    notifyAuthSessionChanged();

    equal(changes, 1);
    equal(
      (hub.messages.at(-1) as { type?: string }).type,
      "session_changed",
    );
    stop();
    external.close();
  } finally {
    if (originalBroadcastChannel) {
      Object.defineProperty(
        globalThis,
        "BroadcastChannel",
        originalBroadcastChannel,
      );
    } else {
      Reflect.deleteProperty(globalThis, "BroadcastChannel");
    }
  }
});
