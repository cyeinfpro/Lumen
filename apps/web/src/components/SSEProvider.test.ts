import { ok, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("./SSEProvider.tsx", import.meta.url),
  "utf8",
);

test("idless SSE events are not rebroadcast across tabs", () => {
  const untrackedGuard = source.indexOf(
    "if (seenResult === null) return;",
  );
  const broadcastPost = source.indexOf("postMessage({");

  ok(untrackedGuard >= 0, "missing untracked-event broadcast guard");
  ok(broadcastPost >= 0, "missing BroadcastChannel postMessage call");
  ok(
    untrackedGuard < broadcastPost,
    "untracked events must return before BroadcastChannel postMessage",
  );
});

test("idless BroadcastChannel messages are dropped before store delivery", () => {
  match(
    source,
    /seenResult === null && opts\?\.source === "broadcast"[\s\S]*?return;/,
  );
});

test("SSE event identity accepts payload ids, sse ids, and EventSource ids", () => {
  match(source, /event_id\?: unknown;[\s\S]*?sse_id\?: unknown;[\s\S]*?msg_id\?: unknown/);
  match(source, /typeof raw === "number" && Number\.isFinite\(raw\)[\s\S]*?return String\(raw\)/);
  match(source, /typeof sseId === "number" && Number\.isFinite\(sseId\)[\s\S]*?return String\(sseId\)/);
  match(source, /typeof msgId === "number" && Number\.isFinite\(msgId\)[\s\S]*?return String\(msgId\)/);
  match(source, /return eventId \|\| null;/);
});

test("BroadcastChannel cleanup only clears its own channel instance", () => {
  match(source, /const channel = new BroadcastChannel\(SSE_BROADCAST_CHANNEL\)/);
  match(source, /if \(broadcastRef\.current === channel\) \{[\s\S]*?broadcastRef\.current = null;/);
});

test("chat store reset clears the local seen event id cache", () => {
  match(source, /lumen:chat-store-reset/);
  match(source, /seenEventIdsRef\.current\.clear\(\)/);
  match(source, /seenEventIdQueueRef\.current = \[\]/);
});
