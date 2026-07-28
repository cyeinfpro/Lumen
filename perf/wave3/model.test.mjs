import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FIXTURE,
  FIXED_THRESHOLDS,
  buildAssets,
  filterAssets,
  pageForIndex,
  summarizeScenarios,
  targetAcceptance,
} from "./model.mjs";

test("1000 asset fixture has exact Wave 3 fault distribution", () => {
  const assets = buildAssets();
  assert.equal(assets.length, 1000);
  assert.deepEqual(summarizeScenarios(assets), {
    missing_thumb: 50,
    pending: 10,
    ready: 920,
    thumb_404: 20,
  });
});

test("search target is on page 20 and can be found without prior pages", () => {
  const assets = buildAssets();
  const matches = filterAssets(assets, DEFAULT_FIXTURE.searchQuery);
  assert.equal(pageForIndex(DEFAULT_FIXTURE.searchTargetIndex), 20);
  assert.deepEqual(
    matches.map((asset) => asset.id),
    ["asset-975"],
  );
});

test("fixed target acceptance rejects unbounded current behavior", () => {
  const result = {
    network: { binaryRequests: 1 },
    page: {
      diagnostics: {
        displayRequestsByReason: { hover: 100 },
        failedThumbStillInSrcSet: 20,
        prewarmMaxQueueDepth: 500,
        repeatedFailedThumbRequests: 20,
      },
      maxMountedTiles: 1000,
      search: {
        loadedPagesBeforeSearch: 20,
        resultIds: ["asset-975"],
      },
    },
  };
  const acceptance = targetAcceptance(result);
  assert.equal(acceptance.mounted_tiles.limit, FIXED_THRESHOLDS.desktopMountedTiles);
  assert.ok(
    Object.values(acceptance).every((measurement) => measurement.status === "not_met"),
  );
});

test("fixed target acceptance permits bounded target behavior", () => {
  const result = {
    network: { binaryRequests: 0 },
    page: {
      diagnostics: {
        displayRequestsByReason: { hover: 0 },
        failedThumbStillInSrcSet: 0,
        prewarmMaxQueueDepth: 32,
        repeatedFailedThumbRequests: 0,
      },
      maxMountedTiles: 72,
      search: {
        loadedPagesBeforeSearch: 1,
        resultIds: ["asset-975"],
      },
    },
  };
  const acceptance = targetAcceptance(result);
  assert.ok(
    Object.values(acceptance).every((measurement) => measurement.status === "met"),
  );
});

