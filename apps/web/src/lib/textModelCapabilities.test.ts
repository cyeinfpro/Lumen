import assert from "node:assert/strict";
import test from "node:test";
import { gpt6ModelFamily, normalizeTextReasoning, textModelLabel, textReasoningOptions } from "./textModelCapabilities.ts";

test("GPT-6 IDs are exact or dated snapshots, including gateway prefixes", () => {
  assert.equal(gpt6ModelFamily("gateway:openai/GPT-6-ASTRA-2026-09-29"), "astra");
  assert.equal(gpt6ModelFamily("gpt-6-astra-pro"), null);
  assert.equal(gpt6ModelFamily("gpt-6-unknown"), null);
  assert.equal(textModelLabel("gpt-6-astra"), "GPT-6 Astra");
  assert.equal(textModelLabel("gpt-6-luna-2026-09-29"), "GPT-6 Luna · 2026-09-29");
});

test("Astra cannot expose off/minimal and restores old draft settings safely", () => {
  assert.deepEqual(textReasoningOptions("gpt-6-astra").map((option) => option.value), ["auto", "low", "medium", "high", "xhigh", "max"]);
  assert.equal(normalizeTextReasoning("gpt-6-astra", "none"), "low");
  assert.equal(normalizeTextReasoning("gpt-6-astra", "minimal"), "low");
  assert.equal(normalizeTextReasoning("gpt-6-astra", "auto"), "auto");
  assert.equal(normalizeTextReasoning("gpt-6-sol", "none"), "none");
  assert.equal(normalizeTextReasoning("custom", "minimal"), "minimal");
});
