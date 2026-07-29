import { sseUrl } from "@/lib/apiClient";
import {
  CONTROL_EVENT_NAMES,
  type RealtimeControlEvent,
  type RealtimeDomainEvent,
  type RecoveryReason,
} from "./contracts";
import {
  transitionConnection,
  type ConnectionEffect,
  type ConnectionState,
} from "./connectionMachine";
import {
  CrossTabBus,
  type BroadcastChannelFactory,
} from "./crossTabBus";
import type { CrossTabMessage } from "./crossTabProtocol";
import {
  type EventStreamTransport,
  type StreamHandle,
} from "../api/eventSourceTransport";
import { LeaderElection, type LeaderClock } from "./leaderElection";
import {
  ReplayCoordinator,
  type SnapshotAdapter,
  type SnapshotExecutionContext,
  type SnapshotResult,
} from "./replayCoordinator";
import { parseRealtimeEvent } from "./validation";

export type RealtimeStatus =
  | "connecting"
  | "open"
  | "closed"
  | "error";

export type RuntimeSubscriber = {
  handlers: Record<string, (data: unknown, id: string) => void>;
  onOpen?: (event: Event, context: SnapshotExecutionContext) => void;
  onError?: (event: Event) => void;
  onControl?: (event: RealtimeControlEvent) => void;
  onAuthInvalidated?: () => void;
  recoverSnapshot?: SnapshotAdapter;
  hiddenCloseDelayMs?: number;
  maxRetryCount?: number;
  setStatus(status: RealtimeStatus): void;
};

export type RealtimeRuntimeOptions = {
  channels: string[];
  tabId?: string;
  transport: EventStreamTransport;
  broadcastFactory?: BroadcastChannelFactory;
  leaderClock?: LeaderClock;
  now?: () => number;
  retryDelay?: (attempt: number) => number;
};

const MAX_SEEN_EVENT_IDS = 2_000;

export function retryBaseDelay(attempt: number): number {
  return Math.min(30_000, 1000 * 2 ** Math.min(5, Math.max(0, attempt)));
}

function tabId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function statusFor(state: ConnectionState): RealtimeStatus {
  if (state.kind === "open") return "open";
  if (state.kind === "connecting") return "connecting";
  if (
    state.kind === "backoff" ||
    state.kind === "recovering" ||
    state.kind === "unauthorized"
  ) {
    return "error";
  }
  return "closed";
}

function recoveryReason(event: RealtimeControlEvent): RecoveryReason | null {
  if (event.type === "replay_truncated") {
    return {
      kind: "replay_gap",
      reason: event.reason,
      cursor: event.cursor,
    };
  }
  if (event.type === "recovery_required") {
    return {
      kind: "recovery_required",
      reason: event.reason,
      cursor: event.cursor,
    };
  }
  if (event.type === "server_epoch_changed") {
    return {
      kind: "server_epoch_changed",
      epoch: event.epoch,
      cursor: event.cursor,
    };
  }
  return null;
}

export class RealtimeRuntime {
  private subscribers = new Set<RuntimeSubscriber>();
  private state: ConnectionState = { kind: "idle" };
  private source: StreamHandle | null = null;
  private sourceEventNames = new Set<string>();
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private hiddenTimer: ReturnType<typeof setTimeout> | null = null;
  private seen = new Set<string>();
  private seenQueue: string[] = [];
  private started = false;
  private leader = false;
  private readonly now: () => number;
  private readonly channelKey: string;
  private readonly transport: EventStreamTransport;
  private readonly bus: CrossTabBus;
  private readonly election: LeaderElection;
  private readonly options: RealtimeRuntimeOptions;
  private readonly id: string;
  private connectionGeneration = 0;
  private activeSourceGeneration: number | null = null;
  private recoveryFlight: Promise<void> | null = null;
  private recoveryAbort: AbortController | null = null;
  private recoveryGeneration = 0;
  private recoverySequence = 0;
  private recoveryId: string | null = null;
  private pendingRecoveryId: string | null = null;
  private localRecoveryResult: SnapshotResult | null = null;
  private leaderRecoveryComplete: Extract<
    CrossTabMessage,
    { type: "recovery_complete" }
  > | null = null;
  private unsubscribeBus: (() => void) | null = null;
  private unsubscribeLeader: (() => void) | null = null;

  constructor(options: RealtimeRuntimeOptions) {
    this.options = options;
    this.now = options.now ?? Date.now;
    this.channelKey = [...options.channels].sort().join(",");
    const id = options.tabId ?? tabId();
    this.id = id;
    this.transport = options.transport;
    this.bus = new CrossTabBus(
      this.channelKey,
      id,
      options.broadcastFactory,
    );
    this.election = new LeaderElection(
      id,
      this.bus,
      options.leaderClock,
    );
  }

  subscribe(subscriber: RuntimeSubscriber): () => void {
    this.subscribers.add(subscriber);
    subscriber.setStatus(statusFor(this.state));
    if (!this.started) this.start();
    else if (this.leader && this.sourceNeedsEventNames(subscriber)) {
      this.dispatch({ type: "manual_reconnect" });
    }
    return () => {
      this.subscribers.delete(subscriber);
      if (this.subscribers.size === 0) this.stop();
    };
  }

  reconnect(): void {
    if (this.leader) {
      this.dispatch({ type: "manual_reconnect" });
      return;
    }
    this.bus.post({ type: "manual_reconnect" }, this.now());
  }

  active(): boolean {
    return this.subscribers.size > 0;
  }

  visibility(visible: boolean): void {
    if (!this.leader) return;
    if (visible) {
      if (this.hiddenTimer) clearTimeout(this.hiddenTimer);
      this.hiddenTimer = null;
      this.dispatch({ type: "visible" });
      return;
    }
    if (this.hiddenTimer) clearTimeout(this.hiddenTimer);
    const delay = Math.min(
      ...[...this.subscribers].map(
        (subscriber) => subscriber.hiddenCloseDelayMs ?? 30_000,
      ),
    );
    this.hiddenTimer = setTimeout(() => {
      this.hiddenTimer = null;
      this.dispatch({ type: "hidden" });
    }, Number.isFinite(delay) ? delay : 30_000);
  }

  online(online: boolean): void {
    if (this.leader) this.dispatch({ type: online ? "online" : "offline" });
  }

  private start(): void {
    this.started = true;
    this.unsubscribeBus = this.bus.subscribe((message) =>
      this.onBusMessage(message),
    );
    this.unsubscribeLeader = this.election.subscribe((leader) => {
      this.leader = leader;
      if (!leader) {
        this.cancelRecovery();
        this.dispatch({ type: "stop" });
        return;
      }
      if (this.state.kind === "recovering" && this.recoveryId) {
        if (this.localRecoveryResult) {
          this.completeLeaderRecovery(
            this.recoveryId,
            this.localRecoveryResult,
          );
        }
        return;
      }
      this.dispatch({ type: leader ? "start" : "stop" });
    });
    this.election.start();
  }

  private stop(): void {
    this.started = false;
    this.cancelRecovery();
    this.dispatch({ type: "stop" });
    this.unsubscribeBus?.();
    this.unsubscribeLeader?.();
    this.unsubscribeBus = null;
    this.unsubscribeLeader = null;
    this.election.stop();
    this.bus.close();
    this.sourceEventNames.clear();
    this.activeSourceGeneration = null;
    this.seen.clear();
    this.seenQueue = [];
  }

  private dispatch(event: Parameters<typeof transitionConnection>[1]): void {
    if (
      event.type === "stop" ||
      event.type === "hidden" ||
      event.type === "offline" ||
      event.type === "unauthorized"
    ) {
      this.cancelRecovery();
    }
    // 修复恢复期重复连接：快照恢复在途时丢弃重连类事件。它们会把状态机推出
    // recovering，随后到达的 snapshot_success 因状态不匹配被丢弃 —— 既白开一条用
    // 旧 cursor 的连接，又丢掉恢复得到的新 cursor。恢复完成后自己会重新开流。
    if (this.recoveryId && this.state.kind === "recovering") {
      if (
        event.type === "manual_reconnect" ||
        event.type === "online" ||
        event.type === "visible" ||
        event.type === "start"
      ) {
        return;
      }
    }
    const maxRetryCount = Math.max(
      ...[...this.subscribers].map(
        (subscriber) =>
          subscriber.maxRetryCount ?? Number.POSITIVE_INFINITY,
      ),
    );
    const transition = transitionConnection(this.state, event, {
      now: this.now,
      retryDelay: this.options.retryDelay ?? retryBaseDelay,
      maxRetryCount,
    });
    this.state = transition.state;
    for (const effect of transition.effects) this.applyEffect(effect);
  }

  private applyEffect(effect: ConnectionEffect): void {
    if (effect.kind === "closeSource") {
      this.source?.close();
      this.source = null;
      this.activeSourceGeneration = null;
      this.sourceEventNames.clear();
      return;
    }
    if (effect.kind === "cancelRetry") {
      if (this.retryTimer) clearTimeout(this.retryTimer);
      this.retryTimer = null;
      return;
    }
    if (effect.kind === "scheduleRetry") {
      if (this.retryTimer) clearTimeout(this.retryTimer);
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        this.dispatch({ type: "retry_timer" });
      }, effect.delayMs);
      return;
    }
    if (effect.kind === "openSource") {
      if (this.leader) this.openSource(effect.cursor);
      return;
    }
    if (effect.kind === "recoverSnapshot") {
      const recoveryId =
        this.pendingRecoveryId ?? this.createRecoveryId();
      void this.recover(effect.reason, recoveryId);
      return;
    }
    this.publishStatus();
  }

  private openSource(cursor?: string): void {
    this.source?.close();
    const generation = this.connectionGeneration + 1;
    this.connectionGeneration = generation;
    this.activeSourceGeneration = generation;
    const names = new Set<string>(CONTROL_EVENT_NAMES);
    for (const subscriber of this.subscribers) {
      for (const name of Object.keys(subscriber.handlers)) names.add(name);
    }
    this.sourceEventNames = names;
    this.source = this.transport.open(
      {
        url: sseUrl(this.options.channels, cursor),
        eventNames: [...names],
      },
      {
        onOpen: (event) => {
          if (!this.sourceIsCurrent(generation)) return;
          this.dispatch({ type: "open", at: this.now() });
          const context = this.connectionContext(generation);
          for (const subscriber of this.subscribers) {
            subscriber.onOpen?.(event, context);
          }
        },
        onError: (event) => {
          if (!this.sourceIsCurrent(generation)) return;
          for (const subscriber of this.subscribers) subscriber.onError?.(event);
          this.dispatch({ type: "error", at: this.now() });
        },
        onEvent: (name, data, eventCursor) => {
          if (!this.sourceIsCurrent(generation)) return;
          this.onStreamEvent(name, data, eventCursor);
        },
      },
    );
  }

  private sourceIsCurrent(generation: number): boolean {
    return (
      this.started &&
      this.leader &&
      this.subscribers.size > 0 &&
      this.activeSourceGeneration === generation &&
      this.connectionGeneration === generation
    );
  }

  private connectionContext(
    generation: number,
    isCurrent = () => this.sourceIsCurrent(generation),
  ): SnapshotExecutionContext {
    return {
      connectionGeneration: generation,
      userScope: this.channelKey,
      isCurrent,
    };
  }

  private sourceNeedsEventNames(subscriber: RuntimeSubscriber): boolean {
    if (!this.source) return false;
    return Object.keys(subscriber.handlers).some(
      (name) => !this.sourceEventNames.has(name),
    );
  }

  private onStreamEvent(
    name: string,
    data: unknown,
    cursor?: string,
  ): void {
    const allowed = new Set<string>();
    for (const subscriber of this.subscribers) {
      for (const eventName of Object.keys(subscriber.handlers)) {
        allowed.add(eventName);
      }
    }
    const parsed = parseRealtimeEvent({
      name,
      data,
      cursor,
      allowedDomainEvents: allowed,
    });
    if (parsed.kind !== "event") return;
    if (parsed.event.kind === "control") {
      const recoveryId = recoveryReason(parsed.event)
        ? this.createRecoveryId()
        : undefined;
      this.handleControl(parsed.event, recoveryId);
      this.bus.post(
        { type: "control_event", event: parsed.event, recoveryId },
        this.now(),
      );
      return;
    }
    this.deliverDomain(parsed.event, true);
  }

  private handleControl(
    event: RealtimeControlEvent,
    recoveryId?: string,
  ): void {
    for (const subscriber of this.subscribers) subscriber.onControl?.(event);
    if (event.type === "auth_invalidated") {
      for (const subscriber of this.subscribers) {
        subscriber.onAuthInvalidated?.();
      }
      this.dispatch({ type: "unauthorized" });
      return;
    }
    const reason = recoveryReason(event);
    if (!reason) {
      if (event.cursor) this.dispatch({ type: "cursor", cursor: event.cursor });
      return;
    }
    this.pendingRecoveryId = recoveryId ?? this.createRecoveryId();
    try {
      if (reason.kind === "server_epoch_changed") {
        this.dispatch({
          type: "epoch_change",
          epoch: reason.epoch,
          cursor: reason.cursor,
        });
        return;
      }
      this.dispatch({
        type:
          reason.kind === "recovery_required"
            ? "recovery_required"
            : "replay_gap",
        reason: reason.reason,
        cursor: reason.cursor,
      });
    } finally {
      this.pendingRecoveryId = null;
    }
  }

  private createRecoveryId(): string {
    this.recoverySequence += 1;
    return `${this.id}:${this.recoverySequence}`;
  }

  private recover(
    reason: RecoveryReason,
    recoveryId: string,
  ): Promise<void> {
    if (this.recoveryFlight && this.recoveryId === recoveryId) {
      return this.recoveryFlight;
    }
    if (this.recoveryId && this.recoveryId !== recoveryId) {
      this.cancelRecovery();
    }
    const generation = this.recoveryGeneration + 1;
    this.recoveryGeneration = generation;
    this.recoveryId = recoveryId;
    this.localRecoveryResult = null;
    this.leaderRecoveryComplete = null;
    const connectionGeneration = this.connectionGeneration;
    const controller = new AbortController();
    this.recoveryAbort = controller;
    const flight = this.performRecovery(
      reason,
      recoveryId,
      generation,
      connectionGeneration,
      controller.signal,
    ).finally(() => {
      if (this.recoveryFlight === flight) this.recoveryFlight = null;
      if (
        this.recoveryGeneration === generation &&
        this.recoveryAbort === controller
      ) {
        this.recoveryAbort = null;
      }
    });
    this.recoveryFlight = flight;
    return flight;
  }

  private cancelRecovery(): void {
    if (!this.recoveryFlight && !this.recoveryAbort && !this.recoveryId) return;
    this.recoveryGeneration += 1;
    this.recoveryAbort?.abort();
    this.recoveryAbort = null;
    this.recoveryFlight = null;
    this.recoveryId = null;
    this.localRecoveryResult = null;
    this.leaderRecoveryComplete = null;
  }

  private recoveryIsCurrent(
    recoveryId: string,
    generation: number,
    connectionGeneration: number,
    signal: AbortSignal,
  ): boolean {
    return (
      !signal.aborted &&
      this.started &&
      this.subscribers.size > 0 &&
      this.state.kind === "recovering" &&
      this.recoveryId === recoveryId &&
      this.recoveryGeneration === generation &&
      this.connectionGeneration === connectionGeneration
    );
  }

  private async performRecovery(
    reason: RecoveryReason,
    recoveryId: string,
    generation: number,
    connectionGeneration: number,
    signal: AbortSignal,
  ): Promise<void> {
    const adapters = [
      ...new Set(
        [...this.subscribers]
          .map((subscriber) => subscriber.recoverSnapshot)
          .filter((adapter): adapter is SnapshotAdapter => Boolean(adapter)),
      ),
    ];
    if (adapters.length === 0) {
      if (
        this.recoveryIsCurrent(
          recoveryId,
          generation,
          connectionGeneration,
          signal,
        )
      ) {
        this.dispatch({ type: "snapshot_failure" });
      }
      return;
    }
    const context = this.connectionContext(
      connectionGeneration,
      () =>
        this.recoveryIsCurrent(
          recoveryId,
          generation,
          connectionGeneration,
          signal,
        ),
    );
    const coordinator = new ReplayCoordinator(
      async (scopes, currentReason, currentSignal, currentContext) => {
        currentSignal.throwIfAborted();
        const results = await Promise.all(
          adapters.map((adapter) =>
            adapter(
              scopes,
              currentReason,
              currentSignal,
              currentContext,
            ),
          ),
        );
        currentSignal.throwIfAborted();
        return {
          cursor:
            results.find((result) => result.cursor)?.cursor ??
            ("cursor" in currentReason ? currentReason.cursor : undefined),
          syncedAt: this.now(),
        };
      },
    );
    try {
      const result = await coordinator.recover(reason, signal, context);
      if (!context.isCurrent()) return;
      this.localRecoveryResult = result;
      if (this.leader) {
        this.completeLeaderRecovery(recoveryId, result);
      } else {
        this.completeFollowerRecoveryIfReady();
      }
    } catch (error) {
      if (!context.isCurrent()) return;
      if (this.leader) {
        this.bus.post(
          {
            type: "recovery_failed",
            recoveryId,
            reason: error instanceof Error ? error.message : "snapshot_failed",
          },
          this.now(),
        );
      }
      this.dispatch({ type: "snapshot_failure" });
      this.recoveryId = null;
      this.localRecoveryResult = null;
      this.leaderRecoveryComplete = null;
    }
  }

  private completeLeaderRecovery(
    recoveryId: string,
    result: SnapshotResult,
  ): void {
    this.bus.post(
      {
        type: "recovery_complete",
        recoveryId,
        cursor: result.cursor,
        syncedAt: result.syncedAt ?? this.now(),
      },
      this.now(),
    );
    this.dispatch({ type: "snapshot_success", cursor: result.cursor });
    this.recoveryId = null;
    this.localRecoveryResult = null;
    this.leaderRecoveryComplete = null;
  }

  private completeFollowerRecoveryIfReady(): void {
    const complete = this.leaderRecoveryComplete;
    if (
      this.leader ||
      !complete ||
      complete.recoveryId !== this.recoveryId ||
      !this.localRecoveryResult
    ) {
      return;
    }
    this.state = {
      kind: "open",
      cursor: complete.cursor ?? this.localRecoveryResult.cursor,
      openedAt: complete.syncedAt,
    };
    this.notifyStatus("open");
    this.recoveryId = null;
    this.localRecoveryResult = null;
    this.leaderRecoveryComplete = null;
  }

  private deliverDomain(event: RealtimeDomainEvent, broadcast: boolean): void {
    if (event.cursor && !this.markSeen(event.cursor)) return;
    for (const subscriber of this.subscribers) {
      subscriber.handlers[event.type]?.(event.payload, event.cursor ?? "");
    }
    if (broadcast && event.cursor) {
      this.bus.post({ type: "domain_event", event }, this.now());
    }
  }

  private markSeen(id: string): boolean {
    if (this.seen.has(id)) return false;
    this.seen.add(id);
    this.seenQueue.push(id);
    while (this.seenQueue.length > MAX_SEEN_EVENT_IDS) {
      const stale = this.seenQueue.shift();
      if (stale) this.seen.delete(stale);
    }
    return true;
  }

  private onBusMessage(message: CrossTabMessage): void {
    if (message.type === "manual_reconnect") {
      if (this.leader) this.dispatch({ type: "manual_reconnect" });
      return;
    }
    if (message.type === "domain_event") {
      this.deliverDomain(message.event, false);
      return;
    }
    if (message.type === "control_event") {
      this.handleControl(
        message.event,
        message.recoveryId ?? `${message.sender}:${message.sentAt}`,
      );
      return;
    }
    this.handleBusStateMessage(message);
  }

  private handleBusStateMessage(message: CrossTabMessage): void {
    if (message.type === "recovery_complete") {
      this.handleFollowerRecoveryComplete(message);
      return;
    }
    if (message.type === "recovery_failed") {
      this.handleFollowerRecoveryFailed(message);
      return;
    }
    if (message.type === "status" && !this.leader) {
      this.followLeaderStatus(message.status);
    }
  }

  private handleFollowerRecoveryComplete(
    message: Extract<CrossTabMessage, { type: "recovery_complete" }>,
  ): void {
    if (this.leader || message.recoveryId !== this.recoveryId) return;
    this.leaderRecoveryComplete = message;
    this.completeFollowerRecoveryIfReady();
  }

  private handleFollowerRecoveryFailed(
    message: Extract<CrossTabMessage, { type: "recovery_failed" }>,
  ): void {
    if (this.leader || message.recoveryId !== this.recoveryId) return;
    this.cancelRecovery();
    this.notifyStatus("error");
  }

  private followLeaderStatus(status: string): void {
    if (
      this.state.kind === "recovering" &&
      this.recoveryId &&
      status !== "error"
    ) {
      return;
    }
    if (
      status === "open" ||
      status === "closed" ||
      status === "connecting" ||
      status === "error"
    ) {
      this.notifyStatus(status);
    }
  }

  private publishStatus(): void {
    const status = statusFor(this.state);
    this.notifyStatus(status);
    if (this.leader) {
      this.bus.post({ type: "status", status }, this.now());
    }
  }

  private notifyStatus(status: RealtimeStatus): void {
    for (const subscriber of this.subscribers) subscriber.setStatus(status);
  }
}
