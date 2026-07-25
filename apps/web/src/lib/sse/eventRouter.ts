import type { RealtimeDomainEvent } from "./contracts";

export interface RealtimeEffectContext {
  eventId?: string;
}

export interface RealtimeEffect<
  TEvent extends RealtimeDomainEvent = RealtimeDomainEvent,
> {
  apply(
    event: TEvent,
    context: RealtimeEffectContext,
  ): void | Promise<void>;
}

export type RealtimeEffectRegistry = Record<string, RealtimeEffect>;

export class EventRouter {
  private readonly registry: RealtimeEffectRegistry;

  constructor(registry: RealtimeEffectRegistry) {
    this.registry = registry;
  }

  route(event: RealtimeDomainEvent): void {
    const effect = this.registry[event.type];
    if (!effect) return;
    void effect.apply(event, { eventId: event.cursor });
  }
}
