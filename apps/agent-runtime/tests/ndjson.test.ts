import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import { describe, expect, it } from "vitest";

import { NdjsonEventWriter } from "../src/ndjson.js";

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
    maxStreamBytes?: number;
    maxEvents?: number;
    drainTimeoutMs?: number;
  } = {},
): NdjsonEventWriter {
  return new NdjsonEventWriter(
    response as unknown as ServerResponse,
    "run-1",
    1,
    options.maxLineBytes ?? 1024,
    options.maxStreamBytes ?? 64 * 1024,
    options.maxEvents ?? 100,
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

  it("reserves one event and one maximum line for the terminal frame", async () => {
    const response = new FakeResponse(true);
    const output = writer(response, {
      maxLineBytes: 512,
      maxStreamBytes: 1024,
      maxEvents: 3,
    });

    await expect(output.emit("run.started")).resolves.toBe(true);
    await expect(output.emit("run.heartbeat")).resolves.toBe(true);
    await expect(output.emit("run.heartbeat")).resolves.toBe(false);
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
    expect(output.bytesWritten).toBeLessThanOrEqual(1024);
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
});
