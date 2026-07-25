import {
  doesNotMatch,
  match,
  ok,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const provider = readFileSync(
  new URL("./SSEProvider.tsx", import.meta.url),
  "utf8",
);
const hook = readFileSync(
  new URL("../lib/useSSE.ts", import.meta.url),
  "utf8",
);

test("provider is a thin runtime boundary without business routing", () => {
  ok(provider.trimEnd().split("\n").length < 250);
  match(provider, /useLumenRealtime\(\)/);
  doesNotMatch(provider, /switch\s*\(|invalidateQueries|BroadcastChannel|EventSource/);
});

test("compatibility hook delegates to the realtime runtime and exposes control recovery", () => {
  ok(hook.trimEnd().split("\n").length < 200);
  match(hook, /new RealtimeRuntime/);
  match(hook, /recoverSnapshot/);
  match(hook, /onControl/);
  doesNotMatch(hook, /class SharedSSEConnection/);
});
