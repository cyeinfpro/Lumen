import { deepEqual, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("./StoryboardPages.tsx", import.meta.url),
  "utf8",
);

function storyboardSSEHandlerSource(): string {
  const handlerStart = source.indexOf("useSSE(");
  const handlerEnd = source.indexOf("const run = query.data;", handlerStart);
  ok(handlerStart >= 0 && handlerEnd > handlerStart, "missing storyboard SSE handler");
  return source.slice(handlerStart, handlerEnd);
}

function callbackBody(handlerSource: string, callbackName: string): string {
  const callbackMatch = handlerSource.match(
    new RegExp(
      `const ${callbackName} = \\(\\) => \\{(?<body>[\\s\\S]*?)\\n\\s*\\};`,
    ),
  );
  ok(callbackMatch?.groups?.body, `missing ${callbackName} callback`);
  return callbackMatch.groups.body;
}

function invalidatedQueryKeys(callbackSource: string): string[] {
  return Array.from(
    callbackSource.matchAll(
      /qc\.invalidateQueries\(\{\s*queryKey:\s*(?<key>[^}]+?)\s*\}\);/g,
    ),
    (entry) => (entry.groups?.key ?? "").replace(/\s+/g, " ").trim(),
  );
}

function storyboardSSEEventEntries(
  handlerSource: string,
): Array<[string, string]> {
  const eventMapMatch = handlerSource.match(
    /return \{(?<events>[\s\S]*?)\n\s*\};/,
  );
  ok(eventMapMatch?.groups?.events, "missing storyboard SSE event map");

  return Array.from(
    eventMapMatch.groups.events.matchAll(
      /"(?<event>[^"]+)":\s*(?<handler>[^,\n]+),/g,
    ),
    (entry): [string, string] => [
      entry.groups?.event ?? "",
      entry.groups?.handler.trim() ?? "",
    ],
  );
}

test("storyboard SSE handles deletion without dropping existing events", () => {
  const eventEntries = storyboardSSEEventEntries(storyboardSSEHandlerSource());

  deepEqual(eventEntries, [
    ["storyboard.updated", "refreshDetailAndList"],
    ["storyboard.deleted", "refreshDetailAndList"],
    ["storyboard.asset_generating", "refreshDetailAndList"],
    ["storyboard.asset_ready", "refreshDetailAndList"],
    ["storyboard.keyframe_generating", "refreshDetailAndList"],
    ["storyboard.keyframe_ready", "refreshDetailAndList"],
    ["storyboard.shot_submitted", "refreshDetailAndList"],
    ["storyboard.shot_done", "refreshDetailAndList"],
    ["storyboard.assembling", "refreshDetailAndList"],
    ["storyboard.assembled", "refreshDetailAndList"],
    ["storyboard.assembly_failed", "refreshDetailAndList"],
    ["generation.succeeded", "refreshDetailAndList"],
    ["generation.failed", "refreshDetailAndList"],
    ["generation.canceled", "refreshDetailAndList"],
    ["video.progress", "refreshDetail"],
    ["video.fetching", "refreshDetailAndList"],
    ["video.succeeded", "refreshDetailAndList"],
    ["video.failed", "refreshDetailAndList"],
    ["video.canceled", "refreshDetailAndList"],
  ]);
});

test("video progress refreshes current detail without refetching the list", () => {
  const handlerSource = storyboardSSEHandlerSource();
  const refreshDetailSource = callbackBody(handlerSource, "refreshDetail");
  const refreshDetailAndListSource = callbackBody(
    handlerSource,
    "refreshDetailAndList",
  );

  deepEqual(invalidatedQueryKeys(refreshDetailSource), [
    "userKeys.storyboard(storyboardId)",
  ]);
  ok(
    refreshDetailAndListSource.includes("refreshDetail();"),
    "list refresh must also refresh the current storyboard detail",
  );
  deepEqual(invalidatedQueryKeys(refreshDetailAndListSource), [
    "userKeys.storyboardsAll()",
  ]);

  const eventHandlers = new Map(storyboardSSEEventEntries(handlerSource));
  deepEqual(eventHandlers.get("video.progress"), "refreshDetail");
  deepEqual(eventHandlers.get("storyboard.deleted"), "refreshDetailAndList");
  deepEqual(eventHandlers.get("video.succeeded"), "refreshDetailAndList");
  deepEqual(eventHandlers.get("video.failed"), "refreshDetailAndList");
  deepEqual(eventHandlers.get("video.canceled"), "refreshDetailAndList");
});
