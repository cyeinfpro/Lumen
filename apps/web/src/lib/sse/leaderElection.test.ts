import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../test-support/load-ts-module.mjs";
import type {
  BroadcastChannelFactory,
  BroadcastChannelLike,
  CrossTabBus as CrossTabBusType,
} from "./crossTabBus";
import type {
  LeaderClock,
  LeaderElection as LeaderElectionType,
} from "./leaderElection";

const { CrossTabBus } = loadTsModule(
  new URL("./crossTabBus.ts", import.meta.url),
) as {
  CrossTabBus: new (
    channelKey: string,
    tabId: string,
    factory: BroadcastChannelFactory,
  ) => CrossTabBusType;
};
const { LeaderElection } = loadTsModule(
  new URL("./leaderElection.ts", import.meta.url),
) as {
  LeaderElection: new (
    tabId: string,
    bus: CrossTabBusType,
    clock?: LeaderClock,
  ) => LeaderElectionType;
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

test("two tabs elect one leader and follower takes over after leader exits", () => {
  const clock = new FakeClock();
  const hub = new FakeBroadcastHub();
  const firstBus = new CrossTabBus("user:u1", "tab-a", hub.create);
  const secondBus = new CrossTabBus("user:u1", "tab-b", hub.create);
  const first = new LeaderElection("tab-a", firstBus, clock);
  const second = new LeaderElection("tab-b", secondBus, clock);
  first.start();
  second.start();
  clock.tick(50);
  equal(first.isLeader(), true);
  equal(second.isLeader(), false);

  first.stop();
  firstBus.close();
  clock.tick(50);
  equal(second.isLeader(), true);
  second.stop();
  secondBus.close();
});

test("default browser clock preserves the global timer receiver", () => {
  const originalTimerDescriptors = {
    setTimeout: Object.getOwnPropertyDescriptor(globalThis, "setTimeout")!,
    clearTimeout: Object.getOwnPropertyDescriptor(globalThis, "clearTimeout")!,
    setInterval: Object.getOwnPropertyDescriptor(globalThis, "setInterval")!,
    clearInterval: Object.getOwnPropertyDescriptor(globalThis, "clearInterval")!,
  };
  let nextTimer = 1;
  const scheduled: Array<{ kind: "timeout" | "interval"; delayMs: number }> = [];
  const cleared: Array<{ kind: "timeout" | "interval"; timer: unknown }> = [];

  const strictSetTimeout = function (
    this: unknown,
    callback: TimerHandler,
    delayMs?: number,
  ) {
    equal(this, globalThis);
    equal(typeof callback, "function");
    scheduled.push({ kind: "timeout", delayMs: delayMs ?? 0 });
    return nextTimer++ as unknown as ReturnType<typeof setTimeout>;
  };
  const strictClearTimeout = function (this: unknown, timer: unknown) {
    equal(this, globalThis);
    cleared.push({ kind: "timeout", timer });
  };
  const strictSetInterval = function (
    this: unknown,
    callback: TimerHandler,
    delayMs?: number,
  ) {
    equal(this, globalThis);
    equal(typeof callback, "function");
    scheduled.push({ kind: "interval", delayMs: delayMs ?? 0 });
    return nextTimer++ as unknown as ReturnType<typeof setInterval>;
  };
  const strictClearInterval = function (this: unknown, timer: unknown) {
    equal(this, globalThis);
    cleared.push({ kind: "interval", timer });
  };

  Object.defineProperties(globalThis, {
    setTimeout: { configurable: true, writable: true, value: strictSetTimeout },
    clearTimeout: {
      configurable: true,
      writable: true,
      value: strictClearTimeout,
    },
    setInterval: {
      configurable: true,
      writable: true,
      value: strictSetInterval,
    },
    clearInterval: {
      configurable: true,
      writable: true,
      value: strictClearInterval,
    },
  });

  try {
    const strictModule = loadTsModule(
      new URL("./leaderElection.ts", import.meta.url),
    ) as {
      LeaderElection: new (
        tabId: string,
        bus: CrossTabBusType,
      ) => LeaderElectionType;
    };
    const hub = new FakeBroadcastHub();
    const bus = new CrossTabBus("user:u1", "tab-a", hub.create);
    const election = new strictModule.LeaderElection("tab-a", bus);

    election.start();
    election.stop();
    bus.close();

    deepEqual(scheduled, [
      { kind: "timeout", delayMs: 50 },
      { kind: "interval", delayMs: 2_000 },
    ]);
    deepEqual(cleared, [
      { kind: "timeout", timer: 1 },
      { kind: "interval", timer: 2 },
    ]);
  } finally {
    Object.defineProperties(globalThis, originalTimerDescriptors);
  }
});
