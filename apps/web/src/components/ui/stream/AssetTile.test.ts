import { doesNotMatch, equal, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const tileSource = readFileSync(
  new URL("./GenerationTile.tsx", import.meta.url),
  "utf8",
);
const modelSource = readFileSync(
  new URL("./generationTileModel.ts", import.meta.url),
  "utf8",
);

const { streamLightboxWindow } = loadTsModule(
  new URL("./lightbox.ts", import.meta.url),
  {
    "@/lib/apiClient": {
      imageVariantUrl: (id: string, kind: string) => `/${id}/${kind}`,
    },
  },
) as {
  streamLightboxWindow(
    items: Array<{ id: string; image: { id: string } }>,
    initialId: string,
    radius?: number,
  ): Array<{ id: string; image: { id: string } }>;
};

test("tile hover uses preview-only candidates and cancels on leave", () => {
  match(tileSource, /model\.hoverPrewarmSources/);
  match(tileSource, /priority:\s*"hover"/);
  match(tileSource, /assetKind:\s*"preview"/);
  match(tileSource, /hoverPrewarmRef\.current\?\.cancel\(\)/);
  doesNotMatch(
    tileSource.slice(
      tileSource.indexOf("const onPointerDown"),
      tileSource.indexOf("const onPreviewIntent"),
    ),
    /display2048|openPrewarmSources|scheduleImages/,
  );
});

test("grid model does not assemble image.url into candidates", () => {
  const createModel = modelSource.slice(
    modelSource.indexOf("export function createGenerationTileModel"),
    modelSource.indexOf("export function imageDownloadName"),
  );
  doesNotMatch(createModel, /item\.image\.url/);
  match(createModel, /gridAssetCandidates\(item\.image\)/);
});

test("lightbox materialization is bounded to current and neighbors", () => {
  const items = Array.from({ length: 1000 }, (_, index) => ({
    id: `generation-${index}`,
    image: { id: `image-${index}` },
  }));
  const windowItems = streamLightboxWindow(items, "generation-500", 12);
  equal(windowItems.length, 25);
  equal(windowItems[12]?.id, "generation-500");
  ok(windowItems.length < items.length);
});
