import assert from "node:assert/strict";
import test from "node:test";
import type { CanvasAssetDescriptor, CanvasDocument } from "./types";
const { normalizeCanvasAssets, mergeCanvasAssets, projectCanvasAsset, assetKey, hasPreparingCanvasAssets } =
  await import(new URL("./assets.ts", import.meta.url).href);
const { activeOutputsByNode } = await import(new URL("./runtime.ts", import.meta.url).href);
const { mergeCanvasDocumentByRevision } = await import("#canvas-document-merge");

function asset(overrides: Partial<CanvasAssetDescriptor> = {}): CanvasAssetDescriptor {
  return { schema_version: 1, asset_id: "asset", kind: "video", source_sha256: "a".repeat(64),
    mime: "video/mp4", size_bytes: 100, width: 1280, height: 720, duration_ms: 3000,
    preparation_state: "ready", preparation_revision: 2, updated_at: null,
    locators: { original: "/api/videos/asset/binary", preview: "/api/videos/asset/binary", thumb: null },
    ...overrides };
}
function document(assets?: CanvasAssetDescriptor[]): CanvasDocument {
  return { id: "canvas", title: "测试", revision: 1, created_at: "", updated_at: "",
    graph: { schema_version: 1, nodes: [], edges: [], frames: [],
      settings: { snap_to_grid: false, grid_size: 16 } },
    selections: [], recent_executions: [], active_runs: [], ...(assets ? { assets } : {}) };
}

test("asset normalization is optional, versioned, and never fabricates dimensions", () => {
  assert.equal(normalizeCanvasAssets(undefined), undefined);
  assert.deepEqual(normalizeCanvasAssets([{ ...asset(), schema_version: 2 }]), []);
  const [value] = normalizeCanvasAssets([{ ...asset(), width: 0, height: Infinity, duration_ms: -1 }])!;
  assert.equal(value.width, null); assert.equal(value.height, null); assert.equal(value.duration_ms, null);
});

test("only authenticated same-origin API asset locators survive normalization", () => {
  for (const unsafe of ["https://evil.test/a", "//evil.test/a", "javascript:alert(1)",
    "/api/\\\\evil.test/a", "/api/\n/a"]) {
    const [value] = normalizeCanvasAssets([{ ...asset(), locators: { original: unsafe,
      preview: unsafe, thumb: unsafe } }])!;
    assert.deepEqual(value.locators, { original: null, preview: null, thumb: null });
  }
});

test("same-content older preparation cannot overwrite a newer projection", () => {
  const newer = asset();
  const old = asset({ preparation_revision: 1, preparation_state: "preparing", width: null });
  assert.equal(mergeCanvasAssets([newer], [old])![0], newer);
  const changed = asset({ source_sha256: "b".repeat(64), preparation_revision: 0 });
  assert.equal(mergeCanvasAssets([newer], [changed])![0], changed);
  assert.deepEqual(mergeCanvasAssets([newer], []), []);
  assert.deepEqual(mergeCanvasAssets([newer], undefined), [newer]);
});

test("graph revisions do not make stale asset metadata authoritative", () => {
  const current = document([asset()]);
  const incoming = { ...document([asset({ preparation_revision: 1 })]), revision: 2 };
  assert.equal(mergeCanvasDocumentByRevision(current, incoming).assets?.[0], current.assets?.[0]);
});

test("video binary locator never becomes an image poster; ready may have no poster", () => {
  const value = asset();
  const output = projectCanvasAsset({ type: "video", video_id: "asset" }, new Map([[assetKey(value), value]]));
  assert.equal(output.width, 1280); assert.equal(output.duration_ms, 3000);
  assert.equal(output.poster_url, null); assert.equal(output.preview_url, null);
  assert.equal(output.thumbnail_url, null);
  assert.equal(output.url, "/api/videos/asset/binary");
});

test("same ID across media kinds cannot cross-wire metadata", () => {
  const image = asset({ kind: "image", width: 400 });
  const video = asset({ width: 1920 });
  const values = new Map([[assetKey(image), image], [assetKey(video), video]]);
  assert.equal(projectCanvasAsset({ type: "image", image_id: "asset" }, values).width, 400);
  assert.equal(projectCanvasAsset({ type: "video", video_id: "asset" }, values).width, 1920);
});

test("missing descriptor retains compatibility without asserting removal", () => {
  const output = { type: "image" as const, image_id: "unknown", width: 200 };
  assert.equal(projectCanvasAsset(output, new Map()), output);
  assert.equal(hasPreparingCanvasAssets(undefined), false);
  assert.equal(hasPreparingCanvasAssets([asset({ preparation_state: "pending" })]), true);
  assert.equal(hasPreparingCanvasAssets([asset({ preparation_state: "preparing" })]), true);
  assert.equal(hasPreparingCanvasAssets([asset({ preparation_state: "failed" })]), false);
});

test("selection indexing preserves first non-null selection and does not scan per node", () => {
  const value = document();
  value.graph.nodes = [{ id: "node", type: "image_generate", schema_version: 1, title: "",
    position: { x: 0, y: 0 }, config: {}, ui: {} }];
  value.selections = [{ node_id: "node", execution_id: null, output_index: 0 },
    { node_id: "node", execution_id: "chosen", output_index: 0 },
    { node_id: "node", execution_id: "later", output_index: 0 }];
  value.selections.find = () => { throw new Error("Per-node linear selection scan"); };
  value.recent_executions = ["chosen", "later"].map((id) => ({ id, node_id: "node",
    node_type: "image_generate", status: "succeeded", outputs: [{ type: "image", image_id: id }] }));
  assert.equal(activeOutputsByNode(value).get("node")?.image_id, "chosen");
});

test("asset outputs carry metadata without modifying the graph or execution output", () => {
  const value = document([asset()]);
  const node = { id: "node", type: "video_asset" as const, schema_version: 1, title: "",
    position: { x: 0, y: 0 }, config: { video_id: "asset" }, ui: {} };
  value.graph.nodes = [node];
  const before = JSON.stringify(value);
  assert.equal(activeOutputsByNode(value).get("node")?.width, 1280);
  assert.equal(JSON.stringify(value), before);
});
