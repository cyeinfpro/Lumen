import { deepEqual, equal, ok } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";
import type {
  BroadcastChannelLike,
} from "./crossTabBus";
import type {
  EventStreamSink,
  EventStreamTransport,
  OpenStreamInput,
  StreamHandle,
} from "../api/eventSourceTransport";
import type { LeaderClock } from "./leaderElection";
import type {
  RealtimeRuntime as RealtimeRuntimeType,
  RealtimeStatus,
  RuntimeSubscriber,
} from "./runtime";

const { RealtimeRuntime } = loadTsModule(
  new URL("./runtime.ts", import.meta.url),
  {
    "@/lib/apiClient": {
      sseUrl: (_channels: string[], cursor?: string) =>
        `/events${cursor ? `?cursor=${cursor}` : ""}`,
    },
    "@/shared/realtime/browser": {
      createBroadcastChannel() {
        throw new Error("test must inject a BroadcastChannel factory");
      },
    },
  },
) as {
  RealtimeRuntime: new (options: {
    channels: string[];
    tabId: string;
    transport: EventStreamTransport;
    broadcastFactory(name: string): BroadcastChannelLike;
    leaderClock: LeaderClock;
    now(): number;
  }) => RealtimeRuntimeType;
};

class FakeClock implements LeaderClock {
  private value = 0;
  private nextId = 1;
  private timers = new Map<
    number,
    { at: number; callback: () => void; interval?: number }
  >();

  now = () => this.value;

  setTimeout(callback: () => void, delayMs: number) {
    return this.add(callback, delayMs) as unknown as ReturnType<typeof setTimeout>;
  }

  clearTimeout(timer: ReturnType<typeof setTimeout>) {
    this.timers.delete(Number(timer));
  }

  setInterval(callback: () => void, delayMs: number) {
    return this.add(
      callback,
      delayMs,
      delayMs,
    ) as unknown as ReturnType<typeof setInterval>;
  }

  clearInterval(timer: ReturnType<typeof setInterval>) {
    this.timers.delete(Number(timer));
  }

  tick(ms: number) {
    const target = this.value + ms;
    while (true) {
      const next = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= target)
        .sort((left, right) => left[1].at - right[1].at)[0];
      if (!next) break;
      const [id, timer] = next;
      this.value = timer.at;
      if (timer.interval) timer.at += timer.interval;
      else this.timers.delete(id);
      timer.callback();
    }
    this.value = target;
  }

  private add(callback: () => void, delay: number, interval?: number): number {
    const id = this.nextId++;
    this.timers.set(id, { at: this.value + delay, callback, interval });
    return id;
  }
}

class FakeBroadcastHub {
  channels = new Map<string, Set<FakeChannel>>();
  messages: unknown[] = [];

  create = (name: string): BroadcastChannelLike => {
    const channel = new FakeChannel(name, this);
    const channels = this.channels.get(name) ?? new Set();
    channels.add(channel);
    this.channels.set(name, channels);
    return channel;
  };
}

class FakeChannel implements BroadcastChannelLike {
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly name: string;
  private readonly hub: FakeBroadcastHub;

  constructor(name: string, hub: FakeBroadcastHub) {
    this.name = name;
    this.hub = hub;
  }

  postMessage(message: unknown) {
    this.hub.messages.push(message);
    for (const channel of this.hub.channels.get(this.name) ?? []) {
      if (channel !== this) {
        channel.onmessage?.({ data: message } as MessageEvent);
      }
    }
  }

  close() {
    this.hub.channels.get(this.name)?.delete(this);
  }
}

class FakeTransport implements EventStreamTransport {
  opens: Array<{
    input: OpenStreamInput;
    sink: EventStreamSink;
    closed: boolean;
  }> = [];

  open(input: OpenStreamInput, sink: EventStreamSink): StreamHandle {
    const entry = { input, sink, closed: false };
    this.opens.push(entry);
    return {
      close() {
        entry.closed = true;
      },
    };
  }

  emit(
    index: number,
    name: string,
    data: unknown,
    cursor?: string,
  ): void {
    const entry = this.opens[index];
    if (entry && !entry.closed) entry.sink.onEvent(name, data, cursor);
  }
}

function subscriber(
  statuses: RealtimeStatus[],
  recoverSnapshot: RuntimeSubscriber["recoverSnapshot"],
): RuntimeSubscriber {
  return {
    handlers: {},
    recoverSnapshot,
    setStatus(status) {
      statuses.push(status);
    },
  };
}

function runtime(
  tabId: string,
  transport: FakeTransport,
  hub: FakeBroadcastHub,
  clock: FakeClock,
): RealtimeRuntimeType {
  return new RealtimeRuntime({
    channels: ["user:u1"],
    tabId,
    transport,
    broadcastFactory: hub.create,
    leaderClock: clock,
    now: clock.now,
  });
}

async function flushPromises(): Promise<void> {
  await new Promise<void>((resolve) => setImmediate(resolve));
}

test("recovery_required triggers one snapshot and reconnects from its cursor", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  let calls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe(
    subscriber(statuses, async (_scopes, reason, signal) => {
      calls += 1;
      equal(reason.kind, "recovery_required");
      equal(reason.reason, "replay_unavailable");
      equal(signal.aborted, false);
      return { cursor: "20-0", syncedAt: 100 };
    }),
  );
  clock.tick(50);

  transport.emit(
    0,
    "recovery_required",
    JSON.stringify({ reason: "replay_unavailable" }),
  );
  await flushPromises();

  equal(calls, 1);
  equal(transport.opens.length, 2);
  equal(transport.opens[1].input.url, "/events?cursor=20-0");
  equal(statuses.at(-1), "connecting");
  unsubscribe();
});

test("shared runtime recovers with only the subscribers that provide adapters", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const recoveryStatuses: RealtimeStatus[] = [];
  const passiveStatuses: RealtimeStatus[] = [];
  const passiveEvents: Array<{ data: unknown; id: string }> = [];
  let recoveryCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribeRecovery = instance.subscribe(
    subscriber(recoveryStatuses, async (_scopes, reason) => {
      recoveryCalls += 1;
      equal(reason.kind, "replay_gap");
      equal(reason.reason, "history_pruned");
      return { cursor: "42-0", syncedAt: 100 };
    }),
  );
  const unsubscribePassive = instance.subscribe({
    handlers: {
      asset_updated(data, id) {
        passiveEvents.push({ data, id });
      },
    },
    setStatus(status) {
      passiveStatuses.push(status);
    },
  });
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);

  transport.emit(
    0,
    "replay_truncated",
    JSON.stringify({ reason: "history_pruned", cursor: "10-0" }),
  );
  await flushPromises();

  equal(recoveryCalls, 1);
  equal(transport.opens.length, 2);
  equal(transport.opens[1].input.url, "/events?cursor=42-0");
  equal(passiveStatuses.at(-1), "connecting");

  transport.opens[1].sink.onOpen({} as Event);
  transport.emit(
    1,
    "asset_updated",
    JSON.stringify({ asset_id: "asset-1" }),
    "43-0",
  );

  equal(passiveStatuses.at(-1), "open");
  deepEqual(passiveEvents, [
    { data: { asset_id: "asset-1" }, id: "43-0" },
  ]);
  unsubscribeRecovery();
  unsubscribePassive();
});

test("adding a subscriber with existing event names does not churn the SSE source", () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const instance = runtime("tab-a", transport, hub, clock);
  const statuses: RealtimeStatus[] = [];
  const first = instance.subscribe({
    handlers: { "asset_updated": () => {} },
    setStatus(status) {
      statuses.push(status);
    },
  });
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);

  const second = instance.subscribe({
    handlers: { "asset_updated": () => {} },
    setStatus(status) {
      statuses.push(status);
    },
  });

  equal(transport.opens.length, 1);
  ok(statuses.includes("open"));
  second();
  first();
});

test("adding a subscriber with a new event name reconnects once with the union", () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const instance = runtime("tab-a", transport, hub, clock);
  const first = instance.subscribe({
    handlers: { "asset_updated": () => {} },
    setStatus() {},
  });
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);

  const second = instance.subscribe({
    handlers: { "generation.succeeded": () => {} },
    setStatus() {},
  });

  equal(transport.opens.length, 2);
  deepEqual(
    new Set(transport.opens[1].input.eventNames),
    new Set([
      "asset_updated",
      "generation.succeeded",
      "replay_truncated",
      "recovery_required",
      "server_epoch_changed",
      "auth_invalidated",
      "heartbeat",
    ]),
  );
  second();
  first();
});

test("shared runtime fails recovery when any real adapter fails", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const successfulStatuses: RealtimeStatus[] = [];
  const failingStatuses: RealtimeStatus[] = [];
  let successfulCalls = 0;
  let failingCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribeSuccessful = instance.subscribe(
    subscriber(successfulStatuses, async () => {
      successfulCalls += 1;
      return { cursor: "should-not-open", syncedAt: 100 };
    }),
  );
  const unsubscribeFailing = instance.subscribe(
    subscriber(failingStatuses, async () => {
      failingCalls += 1;
      throw new Error("snapshot unavailable");
    }),
  );
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);

  transport.emit(
    0,
    "recovery_required",
    JSON.stringify({ reason: "connection_slot_lost", cursor: "10-0" }),
  );
  await flushPromises();

  equal(successfulCalls, 1);
  equal(failingCalls, 1);
  equal(transport.opens.length, 1);
  equal(successfulStatuses.at(-1), "error");
  equal(failingStatuses.at(-1), "error");
  equal(
    hub.messages.filter(
      (message) => (message as { type?: string }).type === "recovery_failed",
    ).length,
    1,
  );
  equal(
    hub.messages.filter(
      (message) => (message as { type?: string }).type === "recovery_complete",
    ).length,
    0,
  );
  unsubscribeSuccessful();
  unsubscribeFailing();
});

test("last subscriber aborts recovery and stale completion has no effects", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  let release!: () => void;
  let recoverySignal: AbortSignal | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe(
    subscriber(statuses, async (_scopes, _reason, signal) => {
      recoverySignal = signal;
      await pending;
      return { cursor: "stale-20-0", syncedAt: 100 };
    }),
  );
  clock.tick(50);
  transport.emit(
    0,
    "recovery_required",
    JSON.stringify({ reason: "connection_slot_lost" }),
  );
  equal(recoverySignal?.aborted, false);

  unsubscribe();
  equal(recoverySignal?.aborted, true);
  const statusCountAfterStop = statuses.length;
  release();
  await flushPromises();

  equal(transport.opens.length, 1);
  equal(instance.active(), false);
  equal(statuses.length, statusCountAfterStop);
  equal(
    hub.messages.filter(
      (message) =>
        (message as { type?: string }).type === "recovery_complete" ||
        (message as { type?: string }).type === "recovery_failed",
    ).length,
    0,
  );
});

test("two tabs run snapshot recovery only on the elected leader", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const leaderStatuses: RealtimeStatus[] = [];
  const followerStatuses: RealtimeStatus[] = [];
  let leaderCalls = 0;
  let followerCalls = 0;
  const leaderRuntime = runtime("tab-a", leaderTransport, hub, clock);
  const followerRuntime = runtime("tab-b", followerTransport, hub, clock);
  const unsubscribeLeader = leaderRuntime.subscribe(
    subscriber(leaderStatuses, async () => {
      leaderCalls += 1;
      return { cursor: "30-0", syncedAt: 200 };
    }),
  );
  const unsubscribeFollower = followerRuntime.subscribe(
    subscriber(followerStatuses, async () => {
      followerCalls += 1;
      return { cursor: "unexpected", syncedAt: 200 };
    }),
  );
  clock.tick(50);

  equal(leaderTransport.opens.length, 1);
  equal(followerTransport.opens.length, 0);
  leaderTransport.emit(
    0,
    "recovery_required",
    JSON.stringify({ reason: "replay_unavailable" }),
  );
  await flushPromises();

  equal(leaderCalls, 1);
  equal(followerCalls, 0);
  ok(followerStatuses.includes("open"));
  leaderTransport.opens[1].sink.onOpen({} as Event);
  equal(followerStatuses.at(-1), "open");
  ok(
    hub.messages.some(
      (message) => (message as { type?: string }).type === "recovery_complete",
    ),
  );
  unsubscribeLeader();
  unsubscribeFollower();
});
