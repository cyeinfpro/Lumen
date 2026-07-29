import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

import type { CanvasGraph } from "./types";

const cleanupPolicyUrl = new URL(
  "./staleUploadCleanupPolicy.ts",
  import.meta.url,
);
const {
  executeStaleCanvasUploadCleanup,
  shouldCleanupStaleCanvasUpload,
} = (await import(
  cleanupPolicyUrl.href
)) as typeof import("./staleUploadCleanupPolicy");

function graphWithAsset(
  field: "image_id" | "video_id",
  assetId: string,
): CanvasGraph {
  return {
    schema_version: 1,
    nodes: [
      {
        id: "node-1",
        type: field === "video_id" ? "video_asset" : "image_asset",
        schema_version: 1,
        title: "Asset",
        position: { x: 0, y: 0 },
        config: { [field]: assetId },
        ui: {},
      },
    ],
    edges: [],
    frames: [],
    settings: { snap_to_grid: true, grid_size: 16 },
  };
}

const emptyGraph: CanvasGraph = {
  schema_version: 1,
  nodes: [],
  edges: [],
  frames: [],
  settings: { snap_to_grid: true, grid_size: 16 },
};

test("stale upload cleanup deletes only assets created by this request", async () => {
  const deleted: Array<["image" | "video", string]> = [];
  const deleteUpload = async (kind: "image" | "video", assetId: string) => {
    deleted.push([kind, assetId]);
  };

  equal(
    await executeStaleCanvasUploadCleanup(
      {
        graph: emptyGraph,
        kind: "image",
        uploadedAsset: { id: "image-new", created: true },
        initialAssetId: "image-old",
      },
      deleteUpload,
    ),
    true,
  );
  equal(
    await executeStaleCanvasUploadCleanup(
      {
        graph: emptyGraph,
        kind: "video",
        uploadedAsset: { id: "video-existing", created: false },
        initialAssetId: null,
      },
      deleteUpload,
    ),
    false,
  );
  deepEqual(deleted, [["image", "image-new"]]);
});

test("stale upload cleanup preserves assets referenced anywhere in the graph", () => {
  equal(
    shouldCleanupStaleCanvasUpload({
      graph: graphWithAsset("image_id", "shared-image"),
      kind: "mask",
      uploadedAsset: { id: "shared-image", created: true },
      initialAssetId: null,
    }),
    false,
  );
  equal(
    shouldCleanupStaleCanvasUpload({
      graph: graphWithAsset("video_id", "shared-video"),
      kind: "video",
      uploadedAsset: { id: "shared-video", created: true },
      initialAssetId: null,
    }),
    false,
  );
});

test("stale upload cleanup does not delete the request's original asset id", () => {
  equal(
    shouldCleanupStaleCanvasUpload({
      graph: emptyGraph,
      kind: "image",
      uploadedAsset: { id: "image-original", created: true },
      initialAssetId: "image-original",
    }),
    false,
  );
});
