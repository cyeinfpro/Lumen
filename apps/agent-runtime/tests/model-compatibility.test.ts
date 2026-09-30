import { describe, expect, it } from "vitest";
import { gpt6ModelFamily, normalizeGpt6Payload } from "../src/providers/model-compatibility.js";

describe("GPT-6 wire compatibility", () => {
  it.each(["astra", "sol", "luna"])("recognizes %s and dated gateway IDs", (family) => {
    expect(gpt6ModelFamily(`gateway:openai/gpt-6-${family}-2026-09-29`)).toBe(family);
    expect(gpt6ModelFamily(`gpt-6-${family}-unknown`)).toBeNull();
  });
  it.each(["none", "off", "minimal"])("migrates Astra %s without mutating input", (effort) => {
    const payload = { reasoning: { effort, summary: "auto" }, temperature: 0.7, top_p: 1 };
    expect(normalizeGpt6Payload("gpt-6-astra", payload)).toEqual({ reasoning: { effort: "low", summary: "auto" } });
    expect(payload.reasoning.effort).toBe(effort);
    expect(payload.temperature).toBe(0.7);
  });
  it("keeps Auto as omission and strips incompatible sampling", () => {
    expect(normalizeGpt6Payload("gpt-6-astra", { temperature: 1, include: ["reasoning.encrypted_content", "message.output_text.logprobs"] }))
      .toEqual({ include: ["reasoning.encrypted_content"] });
  });
  it("preserves Sol non-reasoning sampling and custom model payloads", () => {
    const payload = { reasoning: { effort: "none" }, temperature: 1 };
    expect(normalizeGpt6Payload("gpt-6-sol", payload)).toEqual(payload);
    expect(normalizeGpt6Payload("custom-model", payload)).toBe(payload);
  });
  it.each(["low", "medium", "high", "xhigh", "max"])("preserves explicit %s", (effort) => {
    expect(normalizeGpt6Payload("gpt-6-astra", { reasoning: { effort } })).toEqual({ reasoning: { effort } });
  });
});
