import assert from "node:assert/strict";
import test from "node:test";

const { activeOutputsByNode } = await import(
  new URL("./runtime.ts", import.meta.url).href,
);

test("active output uses the selected execution instead of the latest successful output", () => {
  const selectedOutput = { type: "image" as const, image_id: "selected-image" };
  const latestOutput = { type: "image" as const, image_id: "latest-image" };

  const outputs = activeOutputsByNode({
    graph: {
      schema_version: 1,
      nodes: [
        {
          id: "image-generate-1",
          type: "image_generate",
          schema_version: 1,
          title: "生成",
          position: { x: 0, y: 0 },
          config: {},
          ui: {},
        },
      ],
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
    selections: [
      {
        node_id: "image-generate-1",
        execution_id: "execution-selected",
        output_index: 0,
      },
    ],
    recent_executions: [
      {
        id: "execution-latest",
        node_id: "image-generate-1",
        node_type: "image_generate",
        status: "succeeded",
        outputs: [latestOutput],
      },
      {
        id: "execution-selected",
        node_id: "image-generate-1",
        node_type: "image_generate",
        status: "succeeded",
        outputs: [selectedOutput],
      },
    ],
  });

  assert.deepEqual(outputs.get("image-generate-1"), selectedOutput);
});

test("generation nodes without a selection have no active output", () => {
  const outputs = activeOutputsByNode({
    graph: {
      schema_version: 1,
      nodes: [
        {
          id: "image-generate-1",
          type: "image_generate",
          schema_version: 1,
          title: "生成",
          position: { x: 0, y: 0 },
          config: {},
          ui: {},
        },
      ],
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
    selections: [
      {
        node_id: "image-generate-1",
        execution_id: null,
        output_index: 0,
      },
    ],
    recent_executions: [
      {
        id: "execution-1",
        node_id: "image-generate-1",
        node_type: "image_generate",
        status: "succeeded",
        outputs: [{ type: "image", image_id: "recent-image" }],
      },
    ],
  });

  assert.equal(outputs.has("image-generate-1"), false);
});

test("asset nodes expose their configured media without a selection", () => {
  const outputs = activeOutputsByNode({
    graph: {
      schema_version: 1,
      nodes: [
        {
          id: "image-asset-1",
          type: "image_asset",
          schema_version: 1,
          title: "图片素材",
          position: { x: 0, y: 0 },
          config: { image_id: "asset-image" },
          ui: {},
        },
        {
          id: "video-asset-1",
          type: "video_asset",
          schema_version: 1,
          title: "视频素材",
          position: { x: 0, y: 0 },
          config: { video_id: "asset-video" },
          ui: {},
        },
        {
          id: "mask-asset-1",
          type: "mask_asset",
          schema_version: 1,
          title: "遮罩素材",
          position: { x: 0, y: 0 },
          config: { image_id: "asset-mask" },
          ui: {},
        },
      ],
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
    selections: [],
    recent_executions: [],
  });

  assert.equal(outputs.get("image-asset-1")?.image_id, "asset-image");
  assert.equal(outputs.get("video-asset-1")?.video_id, "asset-video");
  assert.equal(outputs.get("mask-asset-1")?.image_id, "asset-mask");
});
