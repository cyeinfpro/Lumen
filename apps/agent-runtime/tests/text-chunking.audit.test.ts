import { describe, expect, it, vi } from "vitest";

import { runtimeEventLineBytes } from "../src/ndjson.js";
import { splitRuntimeTextDelta } from "../src/runtime.js";

const OPTIONS = {
  maxLineBytes: 512,
  firstSequence: 99,
  runId: "run-text-audit",
  executionEpoch: 2,
  turn: 1,
};

describe("long response chunking audit", () => {
  it("does not repeatedly scan and copy the entire unconsumed suffix", () => {
    const value = "图片🙂\\\"\n".repeat(4_000);
    const conversions = vi.spyOn(Array, "from");
    let chunks: string[];
    let convertedUnits: number;
    try {
      chunks = splitRuntimeTextDelta(value, OPTIONS);
      convertedUnits = conversions.mock.calls.reduce((total, [input]) =>
        total + (typeof input === "string" ? input.length : 0), 0);
    } finally {
      conversions.mockRestore();
    }
    expect(chunks.join("")).toBe(value);
    // Count actual scanned string units, not wall time: deterministic even on
    // a busy runner. Repeated full-suffix conversion grows quadratically.
    expect(convertedUnits).toBeLessThanOrEqual(value.length * 2);
    const largest = chunks.reduce((maximum, delta, index) => Math.max(maximum,
      runtimeEventLineBytes("text.delta", OPTIONS.firstSequence + index,
        OPTIONS.runId, OPTIONS.executionEpoch, { delta, turn: OPTIONS.turn })), 0);
    expect(largest).toBeLessThanOrEqual(OPTIONS.maxLineBytes);
  });

  it("still rejects a line budget too small to carry one complete code point", () => {
    expect(() => splitRuntimeTextDelta("🙂", { ...OPTIONS, maxLineBytes: 1 })).toThrow(/limit|large/u);
  });
});
