import type { RealtimeDomainEvent } from "./contracts";

type TimerHandle = ReturnType<typeof setTimeout>;
type Schedule = (callback: () => void, delayMs: number) => TimerHandle;
type Cancel = (handle: TimerHandle) => void;

const PROGRESS_EVENTS = new Set([
  "generation.progress",
  "completion.progress",
]);

const BARRIER_EVENTS = new Set([
  "generation.queued",
  "generation.started",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
  "completion.queued",
  "completion.started",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
]);

function taskKey(event: RealtimeDomainEvent): string {
  for (const field of [
    "generation_id",
    "completion_id",
    "task_id",
  ] as const) {
    const value = event.payload[field];
    if (typeof value === "string" && value) return value;
  }
  return event.type;
}

export class ProgressEventCoalescer {
  private readonly dispatch: (event: RealtimeDomainEvent) => void;
  private readonly schedule: Schedule;
  private readonly cancel: Cancel;
  private readonly intervalMs: number;
  private readonly pending = new Map<string, RealtimeDomainEvent>();
  private timer: TimerHandle | null = null;

  constructor(
    dispatch: (event: RealtimeDomainEvent) => void,
    {
      schedule = (callback, delayMs) => setTimeout(callback, delayMs),
      cancel = (handle) => clearTimeout(handle),
      intervalMs = 100,
    }: {
      schedule?: Schedule;
      cancel?: Cancel;
      intervalMs?: number;
    } = {},
  ) {
    this.dispatch = dispatch;
    this.schedule = schedule;
    this.cancel = cancel;
    this.intervalMs = intervalMs;
  }

  route(event: RealtimeDomainEvent): void {
    const key = taskKey(event);
    if (PROGRESS_EVENTS.has(event.type)) {
      this.pending.set(key, event);
      this.ensureScheduled();
      return;
    }
    if (BARRIER_EVENTS.has(event.type)) {
      this.pending.delete(key);
    }
    this.dispatch(event);
  }

  flush(): void {
    this.timer = null;
    const events = [...this.pending.values()];
    this.pending.clear();
    for (const event of events) this.dispatch(event);
  }

  dispose(): void {
    if (this.timer !== null) {
      this.cancel(this.timer);
      this.timer = null;
    }
    this.pending.clear();
  }

  private ensureScheduled(): void {
    if (this.timer !== null) return;
    this.timer = this.schedule(() => this.flush(), this.intervalMs);
  }
}
