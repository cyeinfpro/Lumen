import { EventEmitter } from "node:events";
import type { ServerResponse } from "node:http";
import { describe, expect, it } from "vitest";

import { NdjsonEventWriter } from "../src/ndjson.js";
import { StreamingTextGuard } from "../src/text-guard.js";

class ResponseSink extends EventEmitter {
  destroyed = false;
  writableEnded = false;
  writableNeedDrain = false;
  readonly lines: string[] = [];

  write(line: string): boolean {
    this.lines.push(line);
    return true;
  }

  destroy(): void {
    this.destroyed = true;
  }
}

function scanChunks(value: string, split: number): {
  visible: string;
  violation: string | null;
} {
  const guard = new StreamingTextGuard();
  const results = [guard.push(value.slice(0, split)), guard.push(value.slice(split)), guard.finish()];
  return {
    visible: results.map((result) => result.delta).join(""),
    violation: results.find((result) => result.violation !== null)?.violation ?? null,
  };
}

describe("stream boundary audit regressions", () => {
  it("serializes each emitted payload once and accounts for the actual wire bytes", async () => {
    const response = new ResponseSink();
    const writer = new NdjsonEventWriter(response as unknown as ServerResponse, "run-1", 1, 4096);
    let calls = 0;
    await writer.emit("text.delta", {
      delta: { toJSON: () => { calls += 1; return "x".repeat(calls); } },
    });
    expect(calls).toBe(1);
    expect(writer.bytesWritten).toBe(Buffer.byteLength(response.lines.join(""), "utf8"));
  });

  it("does not wait for a drain that cannot arrive after a synchronous close", async () => {
    const response = new ResponseSink();
    response.write = () => {
      response.destroyed = true;
      response.emit("close");
      return false;
    };
    const writer = new NdjsonEventWriter(response as unknown as ServerResponse, "run-1", 1, 4096, 25);
    await expect(writer.emit("run.heartbeat")).rejects.toThrow(/closed|not writable/u);
    expect(response.listenerCount("drain")).toBe(0);
  });

  it("accepts a drain that was already completed synchronously by write", async () => {
    const response = new ResponseSink();
    response.write = (line: string) => {
      response.lines.push(line);
      response.emit("drain");
      return false;
    };
    const writer = new NdjsonEventWriter(response as unknown as ServerResponse, "run-1", 1, 4096, 25);
    await expect(writer.emit("run.heartbeat")).resolves.toBe(true);
    expect(writer.sequence).toBe(1);
    expect(response.listenerCount("drain")).toBe(0);
  });

  for (const value of [
    "prefix <<tool_call> suffix",
    "prefix <function=public<tool_call> suffix",
    `prefix <function=${" ".repeat(140)}bash> suffix`,
  ]) {
    it(`blocks a reserved marker without a chunk-boundary escape: ${value.slice(0, 48)}`, () => {
      for (let split = 0; split <= value.length; split += 1) {
        const result = scanChunks(value, split);
        expect(result.violation, `split ${String(split)}`).toBe("agent_provider_protocol_error");
        expect(result.visible).not.toContain("<tool_call>");
      }
    });
  }

  for (const value of [
    "ordinary <\n> quoted <tool_call> example\nend",
    "ordinary <`<tool_call>` example",
    "ordinary <<section>still XML</section>",
    "```xml\n`` `\n<tool_call>still a fenced literal\n```\nend",
  ]) {
    it(`preserves literal text across scanner transitions: ${value.slice(0, 48)}`, () => {
      for (let split = 0; split <= value.length; split += 1) {
        const result = scanChunks(value, split);
        expect(result.violation, `split ${String(split)}`).toBeNull();
        expect(result.visible).toBe(value);
      }
    });
  }
});
