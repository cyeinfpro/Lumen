import { deepEqual, equal, match, doesNotMatch } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const {
  assetCandidateSrcSet,
  assetCandidateVersion,
  failedAssetCandidateSource,
  gridAssetCandidates,
  healthyAssetCandidates,
  hoverPrewarmCandidates,
} = loadTsModule(new URL("./sourceCandidates.ts", import.meta.url)) as {
  assetCandidateSrcSet(
    candidates: Array<{ kind: string; src: string; width: number }>,
  ): string | undefined;
  assetCandidateVersion(image: Record<string, unknown>): string;
  failedAssetCandidateSource(
    candidates: Array<{ kind: string; src: string; width: number }>,
    failedSource: string,
    baseUrl?: string,
  ): string | null;
  gridAssetCandidates(image: Record<string, unknown>): Array<{
    kind: string;
    src: string;
    width: number;
  }>;
  healthyAssetCandidates(
    candidates: Array<{ kind: string; src: string; width: number }>,
    failed: ReadonlySet<string>,
  ): Array<{ kind: string; src: string; width: number }>;
  hoverPrewarmCandidates(image: Record<string, unknown>): Array<{
    kind: string;
    src: string;
  }>;
};

const image = {
  id: "img-1",
  url: "/api/images/img-1/binary",
  thumb_url: "/api/images/img-1/variants/thumb256",
  preview_url: "/api/images/img-1/variants/preview1024",
  display_url: "/api/images/img-1/variants/display2048",
};

test("grid candidates never include original or display routes", () => {
  const candidates = gridAssetCandidates(image);
  deepEqual(
    candidates.map((candidate) => candidate.kind),
    ["thumb256", "preview1024"],
  );
  const sources = candidates.map((candidate) => candidate.src).join(" ");
  doesNotMatch(sources, /binary|display2048/);
  equal(
    gridAssetCandidates({
      ...image,
      thumb_url: "/api/images/img-1/binary",
      preview_url: null,
    }).length,
    0,
  );
});

test("failed source is removed from both active source set and srcSet", () => {
  const candidates = gridAssetCandidates(image);
  const healthy = healthyAssetCandidates(
    candidates,
    new Set([image.thumb_url]),
  );
  equal(healthy[0]?.src, image.preview_url);
  const srcSet = assetCandidateSrcSet(healthy) ?? "";
  doesNotMatch(srcSet, /thumb256/);
  match(srcSet, /preview1024 1024w/);
});

test("absolute currentSrc maps back to the relative failed candidate", () => {
  const candidates = gridAssetCandidates(image);
  equal(
    failedAssetCandidateSource(
      candidates,
      "https://lumen.test/api/images/img-1/variants/thumb256",
      "https://lumen.test/stream",
    ),
    image.thumb_url,
  );
});

test("variant readiness and version changes reset candidate identity", () => {
  const pendingThumb = {
    ...image,
    variant_version: "v1",
    variants: {
      thumb256: "pending",
      preview1024: "ready",
      display2048: "ready",
    },
  };
  deepEqual(
    gridAssetCandidates(pendingThumb).map((candidate) => candidate.kind),
    ["preview1024"],
  );
  const before = assetCandidateVersion(pendingThumb);
  const after = assetCandidateVersion({
    ...pendingThumb,
    variant_version: "v2",
    variants: { ...pendingThumb.variants, thumb256: "ready" },
  });
  equal(before === after, false);
});

test("hover only prewarms a ready preview and never display", () => {
  const candidates = hoverPrewarmCandidates({
    ...image,
    variants: {
      thumb256: "ready",
      preview1024: "ready",
      display2048: "ready",
    },
  });
  deepEqual(
    candidates.map((candidate) => candidate.kind),
    ["preview1024"],
  );
});
