export const FIXED_THRESHOLDS = Object.freeze({
  desktopMountedTiles: 160,
  mobileMountedTiles: 80,
  prewarmQueueDepth: 32,
  heapGrowthPercent: 20,
  firstInteractiveRegressionPercent: 10,
});

export const DEFAULT_FIXTURE = Object.freeze({
  count: 1000,
  pageSize: 50,
  searchQuery: "page20-target",
  searchTargetIndex: 975,
});

export function assetScenario(index) {
  const percentile = index % 100;
  if (percentile < 1) return "pending";
  if (percentile < 3) return "thumb_404";
  if (percentile < 8) return "missing_thumb";
  return "ready";
}

export function buildAsset(index, options = {}) {
  const count = options.count ?? DEFAULT_FIXTURE.count;
  const searchTargetIndex =
    options.searchTargetIndex ?? DEFAULT_FIXTURE.searchTargetIndex;
  if (!Number.isInteger(index) || index < 0 || index >= count) {
    throw new RangeError(`asset index ${index} is outside 0..${count - 1}`);
  }
  const scenario = assetScenario(index);
  const searchToken =
    index === searchTargetIndex ? DEFAULT_FIXTURE.searchQuery : `asset-${index}`;
  return {
    id: `asset-${index}`,
    index,
    prompt: `${searchToken} ${index % 7 === 0 ? "portrait" : "scene"}`,
    ratio: index % 3 === 0 ? 4 / 3 : index % 3 === 1 ? 1 : 3 / 4,
    scenario,
    thumbReady: scenario === "ready" || scenario === "thumb_404",
    previewReady: scenario !== "pending",
    displayReady: scenario !== "pending" && index % 4 !== 0,
  };
}

export function buildAssets(options = {}) {
  const count = options.count ?? DEFAULT_FIXTURE.count;
  return Array.from({ length: count }, (_, index) =>
    buildAsset(index, { ...options, count }),
  );
}

export function summarizeScenarios(assets) {
  const counts = {
    missing_thumb: 0,
    pending: 0,
    ready: 0,
    thumb_404: 0,
  };
  for (const asset of assets) counts[asset.scenario] += 1;
  return counts;
}

export function pageForIndex(
  index,
  pageSize = DEFAULT_FIXTURE.pageSize,
) {
  return Math.floor(index / pageSize) + 1;
}

export function filterAssets(assets, rawQuery) {
  const query = String(rawQuery ?? "")
    .trim()
    .toLowerCase();
  if (!query) return assets;
  return assets.filter((asset) => asset.prompt.toLowerCase().includes(query));
}

export function targetAcceptance(result, viewportKind = "desktop") {
  const mountedLimit =
    viewportKind === "mobile"
      ? FIXED_THRESHOLDS.mobileMountedTiles
      : FIXED_THRESHOLDS.desktopMountedTiles;
  const diagnostics = result?.page?.diagnostics ?? {};
  const network = result?.network ?? {};
  const search = result?.page?.search ?? {};
  return {
    binary_requests: {
      limit: 0,
      measured: network.binaryRequests ?? null,
      status: network.binaryRequests === 0 ? "met" : "not_met",
    },
    hover_display_generation: {
      limit: 0,
      measured: diagnostics.displayRequestsByReason?.hover ?? null,
      status:
        diagnostics.displayRequestsByReason?.hover === 0 ? "met" : "not_met",
    },
    mounted_tiles: {
      limit: mountedLimit,
      measured: result?.page?.maxMountedTiles ?? null,
      status:
        Number.isFinite(result?.page?.maxMountedTiles) &&
        result.page.maxMountedTiles <= mountedLimit
          ? "met"
          : "not_met",
    },
    prewarm_queue: {
      limit: FIXED_THRESHOLDS.prewarmQueueDepth,
      measured: diagnostics.prewarmMaxQueueDepth ?? null,
      status:
        Number.isFinite(diagnostics.prewarmMaxQueueDepth) &&
        diagnostics.prewarmMaxQueueDepth <=
          FIXED_THRESHOLDS.prewarmQueueDepth
          ? "met"
          : "not_met",
    },
    server_search_page_20: {
      expectedPage: pageForIndex(DEFAULT_FIXTURE.searchTargetIndex),
      loadedPagesBeforeSearch: search.loadedPagesBeforeSearch ?? null,
      resultIds: search.resultIds ?? [],
      status:
        search.loadedPagesBeforeSearch === 1 &&
        (search.resultIds ?? []).includes(
          `asset-${DEFAULT_FIXTURE.searchTargetIndex}`,
        )
          ? "met"
          : "not_met",
    },
    thumb_fallback_srcset: {
      failedCandidateRequests:
        diagnostics.repeatedFailedThumbRequests ?? null,
      failedCandidateStillInSrcSet:
        diagnostics.failedThumbStillInSrcSet ?? null,
      status:
        diagnostics.repeatedFailedThumbRequests === 0 &&
        diagnostics.failedThumbStillInSrcSet === 0
          ? "met"
          : "not_met",
    },
  };
}

