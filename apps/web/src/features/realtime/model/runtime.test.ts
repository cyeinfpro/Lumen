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
import type { SnapshotAdapter } from "./replayCoordinator";

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
    retryDelay?(attempt: number): number;
    saveCursor?(cursor: string): void | Promise<void>;
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
    recoverSnapshot: recoverSnapshot
      ? afterInitialSnapshot(recoverSnapshot)
      : undefined,
    setStatus(status) {
      statuses.push(status);
    },
  };
}

function afterInitialSnapshot(adapter: SnapshotAdapter): SnapshotAdapter {
  return async (...args) => {
    const reason = args[1];
    if (reason.kind === "initial_snapshot") {
      return { syncedAt: 1 };
    }
    return adapter(...args);
  };
}

function runtime(
  tabId: string,
  transport: FakeTransport,
  hub: FakeBroadcastHub,
  clock: FakeClock,
  options: {
    retryDelay?: (attempt: number) => number;
    saveCursor?: (cursor: string) => void | Promise<void>;
  } = {},
): RealtimeRuntimeType {
  return new RealtimeRuntime({
    channels: ["user:u1"],
    tabId,
    transport,
    broadcastFactory: hub.create,
    leaderClock: clock,
    now: clock.now,
    ...options,
  });
}

async function flushPromises(): Promise<void> {
  await new Promise<void>((resolve) => setImmediate(resolve));
}

async function openWithInitialSnapshot(
  transport: FakeTransport,
  sourceIndex = 0,
): Promise<number> {
  transport.opens[sourceIndex].sink.onOpen({} as Event);
  await flushPromises();
  const readySourceIndex = transport.opens.length - 1;
  ok(readySourceIndex > sourceIndex);
  transport.opens[readySourceIndex].sink.onOpen({} as Event);
  return readySourceIndex;
}

test("initial snapshot failure retries and cannot report open before recovery", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  const retryAttempts: number[] = [];
  let recoveryCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock, {
    retryDelay(attempt) {
      retryAttempts.push(attempt);
      return 0;
    },
  });
  const unsubscribe = instance.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason) => {
      equal(reason.kind, "initial_snapshot");
      recoveryCalls += 1;
      if (recoveryCalls === 1) throw new Error("snapshot unavailable");
      return { cursor: "initial-20-0", syncedAt: 100 };
    },
    setStatus(status) {
      statuses.push(status);
    },
  });
  clock.tick(50);

  transport.opens[0].sink.onOpen({} as Event);
  await flushPromises();

  equal(recoveryCalls, 2);
  deepEqual(retryAttempts, [0]);
  equal(transport.opens[0].closed, true);
  equal(transport.opens[1].input.url, "/events?cursor=initial-20-0");
  equal(statuses.includes("error"), true);
  equal(statuses.includes("open"), false);

  transport.opens[1].sink.onOpen({} as Event);
  equal(statuses.at(-1), "open");
  unsubscribe();
});

test("unmount aborts an in-flight initial snapshot and drops stale completion", async () => {
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
  const unsubscribe = instance.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason, signal) => {
      equal(reason.kind, "initial_snapshot");
      recoverySignal = signal;
      await pending;
      return { cursor: "stale-initial-20-0", syncedAt: 100 };
    },
    setStatus(status) {
      statuses.push(status);
    },
  });
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);
  equal(recoverySignal?.aborted, false);

  unsubscribe();
  equal(recoverySignal?.aborted, true);
  const statusCountAfterStop = statuses.length;
  release();
  await flushPromises();

  equal(instance.active(), false);
  equal(transport.opens.length, 1);
  equal(statuses.length, statusCountAfterStop);
  equal(
    hub.messages.some(
      (message) =>
        (message as { type?: string }).type === "recovery_complete",
    ),
    false,
  );
});

test("two tabs complete the same initial snapshot round before follower opens", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const leaderStatuses: RealtimeStatus[] = [];
  const followerStatuses: RealtimeStatus[] = [];
  let leaderCalls = 0;
  let followerCalls = 0;
  let releaseFollower!: () => void;
  const followerPending = new Promise<void>((resolve) => {
    releaseFollower = resolve;
  });
  const leaderRuntime = runtime("tab-a", leaderTransport, hub, clock);
  const followerRuntime = runtime("tab-b", followerTransport, hub, clock);
  const unsubscribeLeader = leaderRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason) => {
      equal(reason.kind, "initial_snapshot");
      leaderCalls += 1;
      return { cursor: "initial-30-0", syncedAt: 200 };
    },
    setStatus(status) {
      leaderStatuses.push(status);
    },
  });
  const unsubscribeFollower = followerRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason) => {
      equal(reason.kind, "initial_snapshot");
      followerCalls += 1;
      await followerPending;
      return { cursor: "initial-30-0", syncedAt: 200 };
    },
    setStatus(status) {
      followerStatuses.push(status);
    },
  });
  clock.tick(50);

  leaderTransport.opens[0].sink.onOpen({} as Event);
  await flushPromises();

  equal(leaderCalls, 1);
  equal(followerCalls, 1);
  equal(followerTransport.opens.length, 0);
  equal(leaderStatuses.includes("open"), false);
  equal(followerStatuses.includes("open"), false);

  leaderTransport.opens[1].sink.onOpen({} as Event);
  equal(leaderStatuses.at(-1), "open");
  equal(followerStatuses.includes("open"), false);

  releaseFollower();
  await flushPromises();
  equal(followerStatuses.at(-1), "open");
  unsubscribeLeader();
  unsubscribeFollower();
});

test("a late follower joins an open leader through a fresh initial snapshot round", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const leaderStatuses: RealtimeStatus[] = [];
  const followerStatuses: RealtimeStatus[] = [];
  let leaderCalls = 0;
  let followerCalls = 0;
  let releaseFollower!: () => void;
  const followerPending = new Promise<void>((resolve) => {
    releaseFollower = resolve;
  });
  const leaderRuntime = runtime("tab-a", leaderTransport, hub, clock);
  const unsubscribeLeader = leaderRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason) => {
      equal(reason.kind, "initial_snapshot");
      leaderCalls += 1;
      return { cursor: `initial-${leaderCalls}-0`, syncedAt: clock.now() };
    },
    setStatus(status) {
      leaderStatuses.push(status);
    },
  });
  clock.tick(50);
  const readySourceIndex = await openWithInitialSnapshot(leaderTransport);
  equal(leaderStatuses.at(-1), "open");
  equal(leaderCalls, 1);
  const lateRoundStart = hub.messages.length;

  const followerRuntime = runtime("tab-b", followerTransport, hub, clock);
  const unsubscribeFollower = followerRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason) => {
      equal(reason.kind, "initial_snapshot");
      followerCalls += 1;
      await followerPending;
      return { cursor: "initial-2-0", syncedAt: clock.now() };
    },
    setStatus(status) {
      followerStatuses.push(status);
    },
  });
  await flushPromises();

  equal(leaderCalls, 2);
  equal(followerCalls, 1);
  equal(leaderTransport.opens[readySourceIndex].closed, true);
  equal(followerTransport.opens.length, 0);
  equal(followerStatuses.includes("open"), false);
  const lateRoundMessages = hub.messages.slice(lateRoundStart) as Array<{
    type?: string;
    recoveryId?: string;
    event?: { type?: string; reason?: string };
  }>;
  const control = lateRoundMessages.find(
    (message) =>
      message.type === "control_event" &&
      message.event?.type === "recovery_required" &&
      message.event.reason === "initial_snapshot",
  );
  const complete = lateRoundMessages.find(
    (message) => message.type === "recovery_complete",
  );
  ok(control?.recoveryId);
  equal(complete?.recoveryId, control.recoveryId);

  const reopenedSourceIndex = leaderTransport.opens.length - 1;
  leaderTransport.opens[reopenedSourceIndex].sink.onOpen({} as Event);
  equal(leaderStatuses.at(-1), "open");
  equal(followerStatuses.includes("open"), false);

  releaseFollower();
  await flushPromises();
  equal(followerStatuses.at(-1), "open");
  unsubscribeFollower();
  unsubscribeLeader();
});

test("aborting a late follower snapshot prevents stale open", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const followerStatuses: RealtimeStatus[] = [];
  let releaseFollower!: () => void;
  let followerSignal: AbortSignal | undefined;
  const followerPending = new Promise<void>((resolve) => {
    releaseFollower = resolve;
  });
  const leaderRuntime = runtime("tab-a", leaderTransport, hub, clock);
  const unsubscribeLeader = leaderRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async () => ({ cursor: "late-10-0", syncedAt: 100 }),
    setStatus() {},
  });
  clock.tick(50);
  await openWithInitialSnapshot(leaderTransport);

  const followerRuntime = runtime("tab-b", followerTransport, hub, clock);
  const unsubscribeFollower = followerRuntime.subscribe({
    handlers: {},
    recoverSnapshot: async (_scopes, reason, signal) => {
      equal(reason.kind, "initial_snapshot");
      followerSignal = signal;
      await followerPending;
      return { cursor: "late-10-0", syncedAt: 100 };
    },
    setStatus(status) {
      followerStatuses.push(status);
    },
  });
  await flushPromises();
  equal(followerSignal?.aborted, false);

  unsubscribeFollower();
  equal(followerSignal?.aborted, true);
  const statusCountAfterAbort = followerStatuses.length;
  const reopenedSourceIndex = leaderTransport.opens.length - 1;
  leaderTransport.opens[reopenedSourceIndex].sink.onOpen({} as Event);
  releaseFollower();
  await flushPromises();

  equal(followerTransport.opens.length, 0);
  equal(followerStatuses.length, statusCountAfterAbort);
  equal(followerStatuses.includes("open"), false);
  unsubscribeLeader();
});

test("recovery_required triggers one snapshot and reconnects from its cursor", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  let calls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe(
    subscriber(statuses, async (_scopes, reason, signal, context) => {
      calls += 1;
      equal(reason.kind, "recovery_required");
      equal(reason.reason, "replay_unavailable");
      equal(signal.aborted, false);
      equal(context.connectionGeneration, 1);
      equal(context.userScope, "user:u1");
      equal(context.isCurrent(), true);
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

test("unknown protocol versions trigger recovery without domain delivery", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  const issues: string[] = [];
  let deliveries = 0;
  let recoveryCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe({
    handlers: {
      "generation.succeeded": () => {
        deliveries += 1;
      },
    },
    recoverSnapshot: afterInitialSnapshot(async (_scopes, reason) => {
      recoveryCalls += 1;
      equal(reason.kind, "recovery_required");
      equal(reason.reason, "protocol_unknown_version");
      return { cursor: "31-0", syncedAt: 100 };
    }),
    onProtocolIssue(issue) {
      issues.push(issue.reason);
    },
    setStatus(status) {
      statuses.push(status);
    },
  });
  clock.tick(50);
  const sourceIndex = await openWithInitialSnapshot(transport);

  transport.emit(
    sourceIndex,
    "generation.succeeded",
    JSON.stringify({ schema_version: 2, generation_id: "gen-1" }),
    "30-0",
  );
  await flushPromises();

  equal(deliveries, 0);
  equal(recoveryCalls, 1);
  deepEqual(issues, ["unknown_version"]);
  ok(statuses.includes("error"));
  equal(transport.opens[sourceIndex + 1]?.input.url, "/events?cursor=31-0");
  unsubscribe();
});

test("consecutive malformed events emit telemetry then recover at threshold", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  const issueCounts: number[] = [];
  let recoveryCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe({
    ...subscriber(statuses, async (_scopes, reason) => {
      recoveryCalls += 1;
      equal(reason.kind, "recovery_required");
      equal(reason.reason, "protocol_invalid_json");
      return { cursor: "44-0", syncedAt: 100 };
    }),
    handlers: { "generation.succeeded": () => {} },
    onProtocolIssue(issue) {
      issueCounts.push(issue.consecutiveCount);
    },
  });
  clock.tick(50);
  const sourceIndex = await openWithInitialSnapshot(transport);

  transport.emit(sourceIndex, "generation.succeeded", "{", "41-0");
  transport.emit(sourceIndex, "generation.succeeded", "{", "42-0");
  equal(recoveryCalls, 0);
  equal(statuses.at(-1), "open");
  transport.emit(sourceIndex, "generation.succeeded", "{", "43-0");
  await flushPromises();

  deepEqual(issueCounts, [1, 2, 3]);
  equal(recoveryCalls, 1);
  ok(statuses.includes("error"));
  equal(transport.opens[sourceIndex + 1]?.input.url, "/events?cursor=44-0");
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
  const sourceIndex = await openWithInitialSnapshot(transport);

  transport.emit(
    sourceIndex,
    "replay_truncated",
    JSON.stringify({ reason: "history_pruned", cursor: "10-0" }),
  );
  await flushPromises();

  equal(recoveryCalls, 1);
  equal(transport.opens.length, sourceIndex + 2);
  equal(transport.opens[sourceIndex + 1].input.url, "/events?cursor=42-0");
  equal(passiveStatuses.at(-1), "connecting");

  transport.opens[sourceIndex + 1].sink.onOpen({} as Event);
  transport.emit(
    sourceIndex + 1,
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

test("two tabs both finish local replay snapshots before follower reports open", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const leaderStatuses: RealtimeStatus[] = [];
  const followerStatuses: RealtimeStatus[] = [];
  let leaderCalls = 0;
  let followerCalls = 0;
  let releaseFollower!: () => void;
  const followerPending = new Promise<void>((resolve) => {
    releaseFollower = resolve;
  });
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
      await followerPending;
      return { cursor: "30-0", syncedAt: 200 };
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
  equal(followerCalls, 1);
  equal(followerStatuses.includes("open"), false);
  ok(
    hub.messages.some(
      (message) => (message as { type?: string }).type === "recovery_complete",
    ),
  );
  leaderTransport.opens[1].sink.onOpen({} as Event);
  equal(followerStatuses.includes("open"), false);

  releaseFollower();
  await flushPromises();
  equal(followerStatuses.at(-1), "open");
  unsubscribeLeader();
  unsubscribeFollower();
});

test("auth invalidation invokes explicit callbacks in every tab", () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const leaderTransport = new FakeTransport();
  const followerTransport = new FakeTransport();
  const leaderStatuses: RealtimeStatus[] = [];
  const followerStatuses: RealtimeStatus[] = [];
  let leaderInvalidations = 0;
  let followerInvalidations = 0;
  const leaderRuntime = runtime("tab-a", leaderTransport, hub, clock);
  const followerRuntime = runtime("tab-b", followerTransport, hub, clock);
  const unsubscribeLeader = leaderRuntime.subscribe({
    ...subscriber(leaderStatuses, async () => ({})),
    onAuthInvalidated() {
      leaderInvalidations += 1;
    },
  });
  const unsubscribeFollower = followerRuntime.subscribe({
    ...subscriber(followerStatuses, async () => ({})),
    onAuthInvalidated() {
      followerInvalidations += 1;
    },
  });
  clock.tick(50);

  leaderTransport.emit(0, "auth_invalidated", JSON.stringify({}));

  equal(leaderInvalidations, 1);
  equal(followerInvalidations, 1);
  equal(leaderStatuses.at(-1), "error");
  equal(followerStatuses.at(-1), "error");
  equal(leaderTransport.opens[0]?.closed, true);
  unsubscribeLeader();
  unsubscribeFollower();
});

test("explicit session invalidation closes the local stream without retrying", () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const statuses: RealtimeStatus[] = [];
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe(subscriber(statuses, async () => ({})));
  clock.tick(50);

  instance.invalidateSession();
  clock.tick(60_000);

  equal(transport.opens[0]?.closed, true);
  equal(transport.opens.length, 1);
  equal(statuses.at(-1), "error");
  unsubscribe();
});

test("ordinary domain events commit their cursor only after required handlers succeed", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  const delivered: string[] = [];
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe({
    handlers: {
      asset_updated: async (_data, id) => {
        await Promise.resolve();
        delivered.push(id);
      },
    },
    setStatus() {},
  });
  clock.tick(50);
  transport.opens[0].sink.onOpen({} as Event);

  transport.emit(
    0,
    "asset_updated",
    JSON.stringify({ asset_id: "asset-1" }),
    "50-0",
  );
  await flushPromises();

  deepEqual(delivered, ["50-0"]);
  instance.reconnect();
  equal(transport.opens[1]?.input.url, "/events?cursor=50-0");
  unsubscribe();
});

test("required domain handler failure requests snapshot recovery without broadcasting the event", async () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const transport = new FakeTransport();
  let recoveryCalls = 0;
  const instance = runtime("tab-a", transport, hub, clock);
  const unsubscribe = instance.subscribe({
    handlers: {
      asset_updated: () => {
        throw new Error("store apply failed");
      },
    },
    recoverSnapshot: afterInitialSnapshot(async (_scopes, reason) => {
      recoveryCalls += 1;
      equal(reason.kind, "recovery_required");
      equal(reason.reason, "domain_apply_failed");
      equal(reason.cursor, "60-0");
      return { cursor: "61-0", syncedAt: 100 };
    }),
    setStatus() {},
  });
  clock.tick(50);
  const sourceIndex = await openWithInitialSnapshot(transport);

  transport.emit(
    sourceIndex,
    "asset_updated",
    JSON.stringify({ asset_id: "asset-1" }),
    "60-0",
  );
  await flushPromises();

  equal(recoveryCalls, 1);
  equal(
    hub.messages.some(
      (message) =>
        typeof message === "object" &&
        message !== null &&
        "type" in message &&
        message.type === "domain_event",
    ),
    false,
  );
  equal(transport.opens[sourceIndex + 1]?.input.url, "/events?cursor=61-0");
  unsubscribe();
});
