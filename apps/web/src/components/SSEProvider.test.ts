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
const lumenHook = readFileSync(
  new URL("../features/realtime/model/useLumenRealtime.ts", import.meta.url),
  "utf8",
);

test("provider is a thin runtime boundary without business routing", () => {
  ok(provider.trimEnd().split("\n").length < 250);
  match(provider, /useLumenRealtime\(\)/);
  doesNotMatch(provider, /switch\s*\(|invalidateQueries|BroadcastChannel|EventSource/);
});

test("feature hook leases shared realtime and keeps polling fallback", () => {
  ok(hook.trimEnd().split("\n").length < 200);
  match(hook, /REALTIME_TRANSPORT_MODE = "event-source-with-polling-fallback"/);
  match(hook, /acquireRealtimeRuntime/);
  match(hook, /releaseRealtimeRuntime/);
  match(hook, /runtimeRef/);
  match(lumenHook, /const POLLING_INTERVAL_MS = 8_000/);
  match(lumenHook, /hydrateActiveTasks/);
  match(lumenHook, /pollInflightTasks/);
  match(lumenHook, /setRealtimeRuntimeStatus\(channels\.length > 0 \? status : "idle"\)/);
  doesNotMatch(hook, /new Map/);
  doesNotMatch(hook, /function isSSEScopeCurrent/);
  match(hook, /createSSESubscriber/);
  doesNotMatch(hook, /class SharedSSEConnection/);
});
