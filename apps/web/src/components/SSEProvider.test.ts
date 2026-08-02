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
  new URL("../features/realtime/model/useSSE.ts", import.meta.url),
  "utf8",
);
const subscription = readFileSync(
  new URL(
    "../features/realtime/model/sseSubscription.ts",
    import.meta.url,
  ),
  "utf8",
);
const registry = readFileSync(
  new URL("../shared/realtime/runtimeRegistry.ts", import.meta.url),
  "utf8",
);

test("provider is a thin runtime boundary without business routing", () => {
  ok(provider.trimEnd().split("\n").length < 250);
  match(provider, /useLumenRealtime\(\)/);
  doesNotMatch(provider, /switch\s*\(|invalidateQueries|BroadcastChannel|EventSource/);
});

test("feature hook delegates runtime ownership and exposes control recovery", () => {
  ok(hook.trimEnd().split("\n").length < 200);
  match(hook, /acquireRealtimeRuntime/);
  match(hook, /releaseRealtimeRuntime/);
  match(hook, /recoverSnapshot/);
  match(hook, /onControl/);
  doesNotMatch(hook, /new Map/);
  doesNotMatch(hook, /function isSSEScopeCurrent/);
  doesNotMatch(hook, /Object\.fromEntries/);
  match(subscription, /export function isSSEScopeCurrent/);
  match(subscription, /export function dispatchSSECallbackForScope/);
  match(subscription, /export function createSSESubscriber/);
  match(registry, /const runtimes = new Map<string, RealtimeRuntime>\(\)/);
  doesNotMatch(hook, /class SharedSSEConnection/);
});
