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

test("exportMyData retries once after refreshing a stale csrf token", () => {
  match(source, /async function exportApiErrorFromResponse\(res: Response\)/);
  match(source, /if \(res\.status === 403\)/);
  match(source, /err\.code !== "csrf_failed"/);
  match(source, /refreshCsrfToken\(\)\.catch\(\(\) => null\)/);
  match(source, /res = await doFetch\(fresh\)/);
});
