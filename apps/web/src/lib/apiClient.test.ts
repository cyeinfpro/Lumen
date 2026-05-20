import { match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("./apiClient.ts", import.meta.url), "utf8");

test("redeemCode sends an idempotency key generated from crypto.randomUUID with fallback", () => {
  match(source, /function createIdempotencyKey\(\): string/);
  match(source, /crypto\.randomUUID\(\)/);
  match(source, /return uuid\(\);/);
  match(source, /headers: \{ "Idempotency-Key": createIdempotencyKey\(\) \}/);
});
