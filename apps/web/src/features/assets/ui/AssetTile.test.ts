import { doesNotMatch, equal, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const tileSource = readFileSync(
  new URL("./AssetTile.tsx", import.meta.url),
  "utf8",
);
const modelSource = readFileSync(
  new URL("../model/tileModel.ts", import.meta.url),
  "utf8",
);

const { streamLightboxWindow } = loadTsModule(
  new URL("../model/lightbox.ts", import.meta.url),
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

test("desktop hover actions expose preview, reference, download, and authorized delete", () => {
  const actions = tileSource.slice(
    tileSource.indexOf("function GenerationTileDesktopActions"),
    tileSource.indexOf("function GenerationTileActionSheet"),
  );
  match(actions, /label="预览"/);
  match(actions, /label="用作参考图"/);
  match(actions, /label="下载原图"/);
  match(actions, /canDelete \? \(/);
  match(actions, /label="删除图片"/);
  match(actions, /group-focus-within:opacity-100/);
  match(tileSource, /<Tooltip content=\{label\} side="bottom">/);
  match(tileSource, /surface-card-v2/);
  match(tileSource, /focus-visible:shadow-\[var\(--ring\)\]/);
  match(tileSource, /motion-reduce:transform-none/);
  match(tileSource, /<ConfirmDialog[\s\S]*onConfirm=\{deleteImage\}/);
});

test("mobile long-press actions include authorized confirmed deletion", () => {
  const sheet = tileSource.slice(
    tileSource.indexOf("function GenerationTileActionSheet"),
    tileSource.indexOf("export const GenerationTile"),
  );
  for (const label of ["做参考图", "保存到相册", "复制提示词", "在对话中定位", "删除图片"]) {
    match(sheet, new RegExp(`label: "${label}"`));
  }
  match(sheet, /destructive: true/);
  match(sheet, /onSelect: onRequestDelete/);
  match(tileSource, /canDelete=\{Boolean\(onDeleteImage\)\}/);
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
