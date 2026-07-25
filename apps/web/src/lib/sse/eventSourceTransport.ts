export interface EventSourceLike {
  readyState: number;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
  addEventListener(
    name: string,
    listener: (event: MessageEvent) => void,
  ): void;
  removeEventListener(
    name: string,
    listener: (event: MessageEvent) => void,
  ): void;
}

export type EventSourceFactory = (
  url: string,
  init: EventSourceInit,
) => EventSourceLike;

export type OpenStreamInput = {
  url: string;
  eventNames: readonly string[];
};

export type EventStreamSink = {
  onOpen(event: Event): void;
  onError(event: Event): void;
  onEvent(name: string, data: unknown, cursor?: string): void;
};

export interface StreamHandle {
  close(): void;
}

export interface EventStreamTransport {
  open(input: OpenStreamInput, sink: EventStreamSink): StreamHandle;
}

export class BrowserEventSourceTransport implements EventStreamTransport {
  private sequence = 0;
  private readonly factory: EventSourceFactory;

  constructor(
    factory: EventSourceFactory = (url, init) => new EventSource(url, init),
  ) {
    this.factory = factory;
  }

  open(input: OpenStreamInput, sink: EventStreamSink): StreamHandle {
    const sequence = ++this.sequence;
    const source = this.factory(input.url, { withCredentials: true });
    const listeners = new Map<string, (event: MessageEvent) => void>();
    let closed = false;
    const active = () => !closed && sequence === this.sequence;

    source.onopen = (event) => {
      if (active()) sink.onOpen(event);
    };
    source.onerror = (event) => {
      if (active()) sink.onError(event);
    };
    for (const name of input.eventNames) {
      const listener = (event: MessageEvent) => {
        if (!active()) return;
        sink.onEvent(name, event.data, event.lastEventId || undefined);
      };
      listeners.set(name, listener);
      source.addEventListener(name, listener);
    }

    return {
      close: () => {
        if (closed) return;
        closed = true;
        if (sequence === this.sequence) this.sequence += 1;
        source.onopen = null;
        source.onerror = null;
        for (const [name, listener] of listeners) {
          source.removeEventListener(name, listener);
        }
        listeners.clear();
        source.close();
      },
    };
  }
}
