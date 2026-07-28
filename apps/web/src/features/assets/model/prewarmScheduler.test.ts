import { equal, ok } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const { PrewarmScheduler } = loadTsModule(
  new URL("./prewarmScheduler.ts", import.meta.url),
) as {
  PrewarmScheduler: new (options?: Record<string, unknown>) => {
    scheduleImage(
      src: string,
      request: { priority: string; assetKind: string },
    ): { cancel(): void };
    snapshot(): {
      queueDepth: number;
      activeImages: number;
      completed: number;
      timedOut: number;
      dropped: Record<string, number>;
    };
    destroy(): void;
  };
};

const normalEnvironment = () => ({
  hidden: false,
  saveData: false,
  effectiveType: "4g",
});

test("queue remains bounded while sweeping across 500 assets", () => {
  const scheduler = new PrewarmScheduler({
    maxQueue: 32,
    imageConcurrency: 0,
    environment: normalEnvironment,
  });
  for (let index = 0; index < 500; index += 1) {
    scheduler.scheduleImage(`/preview/${index}`, {
      priority: "hover",
      assetKind: "preview",
    });
  }
  const metrics = scheduler.snapshot();
  equal(metrics.queueDepth, 32);
  equal(metrics.dropped.queue_full, 468);
  scheduler.destroy();
});

test("cancelling an unstarted hover removes it from the queue", () => {
  const scheduler = new PrewarmScheduler({
    imageConcurrency: 0,
    environment: normalEnvironment,
  });
  const handle = scheduler.scheduleImage("/preview/cancel", {
    priority: "hover",
    assetKind: "preview",
  });
  equal(scheduler.snapshot().queueDepth, 1);
  handle.cancel();
  equal(scheduler.snapshot().queueDepth, 0);
  equal(scheduler.snapshot().dropped.cancelled, 1);
  scheduler.destroy();
});

test("timeout releases an active image slot for the next job", async () => {
  let calls = 0;
  const scheduler = new PrewarmScheduler({
    imageConcurrency: 1,
    environment: normalEnvironment,
    imageLoader: async () => {
      calls += 1;
      if (calls === 1) throw new Error("image_prewarm_timeout");
    },
  });
  scheduler.scheduleImage("/preview/timeout", {
    priority: "visible",
    assetKind: "preview",
  });
  scheduler.scheduleImage("/preview/next", {
    priority: "visible",
    assetKind: "preview",
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const metrics = scheduler.snapshot();
  equal(calls, 2);
  equal(metrics.timedOut, 1);
  equal(metrics.completed, 1);
  equal(metrics.activeImages, 0);
  scheduler.destroy();
});

test("Save-Data drops hover and display prewarm", () => {
  const scheduler = new PrewarmScheduler({
    imageConcurrency: 0,
    environment: () => ({
      hidden: false,
      saveData: true,
      effectiveType: "4g",
    }),
  });
  scheduler.scheduleImage("/preview/hover", {
    priority: "hover",
    assetKind: "preview",
  });
  scheduler.scheduleImage("/display/open", {
    priority: "open-intent",
    assetKind: "display",
  });
  scheduler.scheduleImage("/thumb/visible", {
    priority: "visible",
    assetKind: "thumb",
  });
  const metrics = scheduler.snapshot();
  equal(metrics.dropped.constrained_network, 2);
  equal(metrics.queueDepth, 1);
  ok(metrics.queueDepth <= 32);
  scheduler.destroy();
});
