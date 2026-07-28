import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../test-support/load-ts-module.mjs";
import type { RealtimeDomainEvent } from "./contracts";

type ProgressEventCoalescerType = {
  route(event: RealtimeDomainEvent): void;
  dispose(): void;
};

const { ProgressEventCoalescer } = loadTsModule(
  new URL("./progressCoalescer.ts", import.meta.url),
) as {
  ProgressEventCoalescer: new (
    dispatch: (event: RealtimeDomainEvent) => void,
    options?: {
      schedule?: (
        callback: () => void,
        delayMs: number,
      ) => ReturnType<typeof setTimeout>;
      cancel?: (handle: ReturnType<typeof setTimeout>) => void;
      intervalMs?: number;
    },
  ) => ProgressEventCoalescerType;
};

function event(
  type: string,
  payload: Record<string, unknown>,
): RealtimeDomainEvent {
  return {
    kind: "domain",
    type,
    version: 1,
    payload,
  };
}

test("progress updates are latest-only per task", () => {
  const dispatched: RealtimeDomainEvent[] = [];
  const scheduled: { current?: () => void } = {};
  const coalescer = new ProgressEventCoalescer(
    (next) => dispatched.push(next),
    {
      schedule(callback) {
        scheduled.current = callback;
        return 1 as unknown as ReturnType<typeof setTimeout>;
      },
      cancel() {},
    },
  );

  coalescer.route(
    event("generation.progress", {
      generation_id: "gen-1",
      progress: 10,
    }),
  );
  coalescer.route(
    event("generation.progress", {
      generation_id: "gen-1",
      progress: 80,
    }),
  );
  coalescer.route(
    event("generation.progress", {
      generation_id: "gen-2",
      progress: 30,
    }),
  );

  equal(dispatched.length, 0);
  scheduled.current?.();
  deepEqual(
    dispatched.map((next) => next.payload.progress),
    [80, 30],
  );
});

test("terminal event drops pending progress and dispatches immediately", () => {
  const dispatched: RealtimeDomainEvent[] = [];
  const scheduled: { current?: () => void } = {};
  const coalescer = new ProgressEventCoalescer(
    (next) => dispatched.push(next),
    {
      schedule(callback) {
        scheduled.current = callback;
        return 1 as unknown as ReturnType<typeof setTimeout>;
      },
      cancel() {},
    },
  );

  coalescer.route(
    event("completion.progress", {
      completion_id: "completion-1",
      progress: 50,
    }),
  );
  coalescer.route(
    event("completion.succeeded", {
      completion_id: "completion-1",
    }),
  );
  scheduled.current?.();

  deepEqual(
    dispatched.map((next) => next.type),
    ["completion.succeeded"],
  );
});

test("delta events are never coalesced", () => {
  const dispatched: RealtimeDomainEvent[] = [];
  const coalescer = new ProgressEventCoalescer((next) =>
    dispatched.push(next),
  );

  coalescer.route(
    event("completion.delta", {
      completion_id: "completion-1",
      text_delta: "a",
    }),
  );
  coalescer.route(
    event("completion.delta", {
      completion_id: "completion-1",
      text_delta: "b",
    }),
  );

  deepEqual(
    dispatched.map((next) => next.payload.text_delta),
    ["a", "b"],
  );
  coalescer.dispose();
});
