import type { CrossTabBus } from "./crossTabBus";
import type { CrossTabMessage } from "./crossTabProtocol";

export type LeaderClock = {
  now(): number;
  setTimeout(callback: () => void, delayMs: number): ReturnType<typeof setTimeout>;
  clearTimeout(timer: ReturnType<typeof setTimeout>): void;
  setInterval(callback: () => void, delayMs: number): ReturnType<typeof setInterval>;
  clearInterval(timer: ReturnType<typeof setInterval>): void;
};

const BROWSER_CLOCK: LeaderClock = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) =>
    globalThis.setTimeout(callback, delayMs),
  clearTimeout: (timer) => globalThis.clearTimeout(timer),
  setInterval: (callback, delayMs) =>
    globalThis.setInterval(callback, delayMs),
  clearInterval: (timer) => globalThis.clearInterval(timer),
};

export class LeaderElection {
  private peers = new Map<string, number>();
  private leaderId: string | null = null;
  private leader = false;
  private electionTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private monitorTimer: ReturnType<typeof setInterval> | null = null;
  private unsubscribe: (() => void) | null = null;
  private listeners = new Set<(leader: boolean) => void>();
  private peerHelloListeners = new Set<(peerId: string) => void>();
  private readonly tabId: string;
  private readonly bus: CrossTabBus;
  private readonly clock: LeaderClock;
  private readonly heartbeatMs: number;
  private readonly staleMs: number;
  private readonly electionDelayMs: number;

  constructor(
    tabId: string,
    bus: CrossTabBus,
    clock: LeaderClock = BROWSER_CLOCK,
    heartbeatMs = 2_000,
    staleMs = 6_000,
    electionDelayMs = 50,
  ) {
    this.tabId = tabId;
    this.bus = bus;
    this.clock = clock;
    this.heartbeatMs = heartbeatMs;
    this.staleMs = staleMs;
    this.electionDelayMs = electionDelayMs;
  }

  start(): void {
    this.bus.start();
    if (!this.bus.available()) {
      this.setLeader(true);
      return;
    }
    this.unsubscribe = this.bus.subscribe((message) => this.onMessage(message));
    this.bus.post({ type: "hello" }, this.clock.now());
    this.scheduleElection();
    this.monitorTimer = this.clock.setInterval(
      () => this.monitor(),
      this.heartbeatMs,
    );
  }

  subscribe(listener: (leader: boolean) => void): () => void {
    this.listeners.add(listener);
    listener(this.leader);
    return () => this.listeners.delete(listener);
  }

  subscribePeerHello(listener: (peerId: string) => void): () => void {
    this.peerHelloListeners.add(listener);
    return () => this.peerHelloListeners.delete(listener);
  }

  isLeader(): boolean {
    return this.leader;
  }

  stop(): void {
    if (this.leader) {
      this.bus.post({ type: "leader_goodbye" }, this.clock.now());
    }
    this.unsubscribe?.();
    this.unsubscribe = null;
    if (this.electionTimer) this.clock.clearTimeout(this.electionTimer);
    if (this.heartbeatTimer) this.clock.clearInterval(this.heartbeatTimer);
    if (this.monitorTimer) this.clock.clearInterval(this.monitorTimer);
    this.electionTimer = null;
    this.heartbeatTimer = null;
    this.monitorTimer = null;
    this.leaderId = null;
    this.setLeader(false);
  }

  private onMessage(message: CrossTabMessage): void {
    const now = this.clock.now();
    if (message.type === "hello") {
      this.peers.set(message.sender, now);
      if (this.leader) {
        this.bus.post({ type: "leader_heartbeat" }, now);
        for (const listener of this.peerHelloListeners) {
          listener(message.sender);
        }
      }
      return;
    }
    if (message.type === "leader_goodbye") {
      this.peers.delete(message.sender);
      if (this.leaderId === message.sender) {
        this.leaderId = null;
        this.scheduleElection();
      }
      return;
    }
    if (message.type !== "leader_heartbeat") return;
    this.peers.set(message.sender, now);
    if (
      this.leader &&
      message.sender.localeCompare(this.tabId) < 0
    ) {
      this.setLeader(false);
    }
    this.leaderId =
      this.leader && this.tabId.localeCompare(message.sender) < 0
        ? this.tabId
        : message.sender;
  }

  private monitor(): void {
    const now = this.clock.now();
    for (const [peer, seenAt] of this.peers) {
      if (now - seenAt > this.staleMs) this.peers.delete(peer);
    }
    if (
      !this.leader &&
      (!this.leaderId ||
        now - (this.peers.get(this.leaderId) ?? 0) > this.staleMs)
    ) {
      this.leaderId = null;
      this.scheduleElection();
    }
  }

  private scheduleElection(): void {
    if (this.electionTimer) return;
    this.electionTimer = this.clock.setTimeout(() => {
      this.electionTimer = null;
      const candidates = [this.tabId, ...this.peers.keys()].sort();
      const winner = candidates[0] ?? this.tabId;
      this.leaderId = winner;
      this.setLeader(winner === this.tabId);
    }, this.electionDelayMs);
  }

  private setLeader(next: boolean): void {
    if (this.leader === next) return;
    this.leader = next;
    if (this.heartbeatTimer) {
      this.clock.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (next) {
      this.leaderId = this.tabId;
      this.bus.post({ type: "leader_heartbeat" }, this.clock.now());
      this.heartbeatTimer = this.clock.setInterval(() => {
        this.bus.post({ type: "leader_heartbeat" }, this.clock.now());
      }, this.heartbeatMs);
    }
    for (const listener of this.listeners) listener(next);
  }
}
