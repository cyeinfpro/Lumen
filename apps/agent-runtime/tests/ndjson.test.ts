import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import { describe, expect, it } from "vitest";

import { CollectingEventWriter, NdjsonEventWriter } from "../src/ndjson.js";

class FakeResponse extends EventEmitter {
  destroyed = false;
  writableEnded = false;
  readonly lines: string[] = [];

  constructor(private readonly writable: boolean) {
    super();
  }

  write(line: string): boolean {
    this.lines.push(line);
    return this.writable;
  }

  destroy(): void {
    this.destroyed = true;
  }
}

function writer(
  response: FakeResponse,
  options: {
    maxLineBytes?: number;
    drainTimeoutMs?: number;
  } = {},
): NdjsonEventWriter {
  return new NdjsonEventWriter(
    response as unknown as ServerResponse,
    "run-1",
    1,
    options.maxLineBytes ?? 1024,
    options.drainTimeoutMs ?? 30_000,
  );
}

describe("NDJSON event writer", () => {
  it("serializes concurrent heartbeat and provider events", async () => {
    const response = new FakeResponse(true);
    const output = writer(response);

    await Promise.all(
      Array.from({ length: 20 }, (_, index) =>
        output.emit(index % 2 === 0 ? "run.heartbeat" : "text.delta", {
          delta: index % 2 === 0 ? undefined : String(index),
        }),
      ),
    );

    const events = response.lines.map(
      (line) => JSON.parse(line) as { seq: number },
    );
    expect(events.map((event) => event.seq)).toEqual(
      events.map((_event, index) => index + 1),
    );
  });

  it("does not split a Pi run at an aggregate event or byte budget", async () => {
    const response = new FakeResponse(true);
    const output = writer(response, { maxLineBytes: 512 });

    for (let index = 0; index < 10_000; index += 1) {
      await expect(output.emit("run.heartbeat")).resolves.toBe(true);
    }
    await expect(
      output.emit(
        "run.completed",
        {
          status: "succeeded",
          provider_dispatch_count: 0,
          provider_completed_count: 0,
        },
        true,
      ),
    ).resolves.toBe(true);
    expect(response.lines).toHaveLength(10_001);
  });

  it("latches backpressure failure and rejects later terminal writes immediately", async () => {
    const response = new FakeResponse(false);
    const output = writer(response, { drainTimeoutMs: 5 });

    await expect(output.emit("run.heartbeat")).rejects.toThrow(
      /backpressure timed out/u,
    );
    expect(response.destroyed).toBe(true);
    expect(response.lines).toHaveLength(1);

    await expect(
      output.emit("run.failed", { status: "failed" }, true),
    ).rejects.toThrow(/backpressure timed out/u);
    expect(response.lines).toHaveLength(1);
  });

  it("keeps every reserved envelope field authoritative", async () => {
    const payload = {
      version: 99,
      type: "run.failed",
      seq: 999,
      run_id: "forged-run",
      execution_epoch: 999,
      detail: "preserved",
    };
    const response = new FakeResponse(true);
    const streaming = writer(response);
    const collecting = new CollectingEventWriter("run-1", 1);

    await streaming.emit("run.heartbeat", payload);
    await collecting.emit("run.heartbeat", payload);

    for (const event of [JSON.parse(response.lines[0] ?? "{}"), collecting.events[0]]) {
      expect(event).toMatchObject({
        version: 1,
        type: "run.heartbeat",
        seq: 1,
        run_id: "run-1",
        execution_epoch: 1,
        detail: "preserved",
      });
    }
  });
});
