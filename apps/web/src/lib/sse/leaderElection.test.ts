import { equal } from "node:assert/strict";
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
    clock: LeaderClock,
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
