import { equal } from "node:assert/strict";
import { test } from "node:test";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";
import type {
  EventSourceLike,
  EventStreamSink,
  OpenStreamInput,
  StreamHandle,
} from "./eventSourceTransport";

const { BrowserEventSourceTransport } = loadTsModule(
  new URL("./eventSourceTransport.ts", import.meta.url),
  {
    "@/shared/realtime/browser": {
      createEventSource() {
        throw new Error("test must inject an EventSource factory");
      },
    },
  },
) as {
  BrowserEventSourceTransport: new (
    factory: () => EventSourceLike,
  ) => {
    open(input: OpenStreamInput, sink: EventStreamSink): StreamHandle;
  };
};

class FakeEventSource implements EventSourceLike {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = 0;
  listeners = new Map<string, Set<(event: MessageEvent) => void>>();

  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    const listeners = this.listeners.get(name) ?? new Set();
    listeners.add(listener);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name: string, listener: (event: MessageEvent) => void) {
    this.listeners.get(name)?.delete(listener);
  }

  close() {
    this.closed += 1;
  }

  emit(name: string, data: unknown, lastEventId = "") {
    const event = {
      data,
      lastEventId,
    } as MessageEvent;
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

test("EventSource adapter ignores stale callbacks and closes idempotently", () => {
  const sources: FakeEventSource[] = [];
  const transport = new BrowserEventSourceTransport(() => {
    const source = new FakeEventSource();
    sources.push(source);
    return source;
  });
  const delivered: string[] = [];
  const first = transport.open(
    { url: "/events?last_event_id=1-0", eventNames: ["task.updated"] },
    {
      onOpen() {},
      onError() {},
      onEvent(_name, _data, cursor) {
        delivered.push(cursor ?? "");
      },
    },
  );
  const second = transport.open(
    { url: "/events?last_event_id=2-0", eventNames: ["task.updated"] },
    {
      onOpen() {},
      onError() {},
      onEvent(_name, _data, cursor) {
        delivered.push(cursor ?? "");
      },
    },
  );
  sources[0].emit("task.updated", {}, "stale");
  sources[1].emit("task.updated", {}, "fresh");
  first.close();
  first.close();
  second.close();
  second.close();
  equal(delivered.join(","), "fresh");
  equal(sources[0].closed, 1);
  equal(sources[1].closed, 1);
  equal(sources[1].listeners.get("task.updated")?.size, 0);
});
