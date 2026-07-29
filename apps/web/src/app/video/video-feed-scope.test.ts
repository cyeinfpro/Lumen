import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

const scopeModuleUrl = new URL("./video-feed-scope.ts", import.meta.url);
const {
  createVideoFeedRuntime,
  isVideoFeedScopeTokenCurrent,
  resetVideoFeedRuntime,
  videoFeedChannels,
  videoFeedRuntimeSnapshot,
  videoFeedScopeToken,
} = (await import(
  scopeModuleUrl.href
)) as typeof import("./video-feed-scope");

test("video feed account changes abort and clear the entire old-user runtime", () => {
  const runtime = createVideoFeedRuntime("user-a");
  const request = { controller: new AbortController() };
  const clearedTimers: number[] = [];
  const oldScope = videoFeedScopeToken(runtime);

  runtime.generationRefreshRequests.set("task-a", request);
  runtime.scheduledRefreshTimers.set("task-a", 17);
  runtime.terminalHistorySynced.add("task-a");
  runtime.generationRefreshEpochs.set("task-a", 3);
  runtime.pendingHistoryRefreshes.add("task-a");
  runtime.lastRefreshAt.set("task-a", 100);
  runtime.refreshBackoffUntil.set("task-a", 200);
  runtime.refreshFailureCounts.set("task-a", 2);

  equal(resetVideoFeedRuntime(runtime, "user-b", (timer) => {
    clearedTimers.push(timer);
  }), true);

  equal(request.controller.signal.aborted, true);
  deepEqual(clearedTimers, [17]);
  deepEqual(videoFeedRuntimeSnapshot(runtime), {
    userId: "user-b",
    generation: 1,
    requests: 0,
    timers: 0,
    terminalHistorySynced: 0,
    pendingHistoryRefreshes: 0,
    refreshEpochs: 0,
    refreshTimestamps: 0,
    refreshBackoffs: 0,
    refreshFailures: 0,
  });
  equal(isVideoFeedScopeTokenCurrent(runtime, oldScope), false);
});

test("video feed channels are empty until active items match the current user scope", () => {
  const runtime = createVideoFeedRuntime("user-a");

  deepEqual(
    videoFeedChannels(runtime, "user-a", [
      { id: "task-a" },
      { id: "task-a" },
      { id: "task-b" },
    ]),
    ["task:task-a", "task:task-b"],
  );
  deepEqual(videoFeedChannels(runtime, "user-b", [{ id: "task-a" }]), []);

  resetVideoFeedRuntime(runtime, "user-b", () => {});
  deepEqual(videoFeedChannels(runtime, "user-a", [{ id: "task-a" }]), []);
  deepEqual(videoFeedChannels(runtime, "user-b", []), []);
});
