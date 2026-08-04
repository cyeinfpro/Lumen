import { createEventSource } from "@/shared/realtime/browser";

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
  private current: StreamHandle | null = null;
  private readonly factory: EventSourceFactory;

  constructor(factory: EventSourceFactory = createEventSource) {
    this.factory = factory;
  }

  open(input: OpenStreamInput, sink: EventStreamSink): StreamHandle {
    // 修复 sequence 竞态：旧实现用共享自增计数器判活，「被新连接顶掉」和「自己已关闭」
    // 两件事混在一个比较里 —— 被顶掉的旧流只是不再回调，底层 EventSource 从没关过，
    // 既漏浏览器连接又让服务端连接数虚高。改为开新流时先明确关闭上一条，判活只看
    // 本 handle 自己的 closed 标志，无跨 handle 比较，也就没有竞态窗口。
    this.current?.close();
    const source = this.factory(input.url, { withCredentials: true });
    const listeners = new Map<string, (event: MessageEvent) => void>();
    let closed = false;
    const active = () => !closed;

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

    const handle: StreamHandle = {
      close: () => {
        if (closed) return;
        closed = true;
        if (this.current === handle) this.current = null;
        source.onopen = null;
        source.onerror = null;
        for (const [name, listener] of listeners) {
          source.removeEventListener(name, listener);
        }
        listeners.clear();
        source.close();
      },
    };
    this.current = handle;
    return handle;
  }
}
