import { ok, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("./SSEProvider.tsx", import.meta.url),
  "utf8",
);

test("idless SSE events are not rebroadcast across tabs", () => {
  const untrackedGuard = source.indexOf(
    'if (seenResult === "untracked") return;',
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
    /seenResult === "untracked" && opts\?\.source === "broadcast"[\s\S]*?return;/,
  );
});

test("SSE event identity accepts payload ids and EventSource ids", () => {
  match(source, /event_id\?: unknown; msg_id\?: unknown/);
  match(source, /return eventId \|\| null;/);
});
