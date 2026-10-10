import { expect, test, type Page, type Route } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";
import type { CanvasEdgeDefinition, CanvasGraph, CanvasHistoricalExecution, CanvasOperation } from "../src/lib/canvas/types";

const NOW = "2026-10-10T13:00:00Z";
const CANVAS = "canvas-history-review";
const CURSOR = "opaque+history|cursor&2";
const mobile = (page: Page) => (page.viewportSize()?.width ?? 1440) < 768;
const node = (page: Page, id: string) => page.locator('.react-flow__node[data-id="' + id + '"]');

function initialGraph(): CanvasGraph {
  return { schema_version: 1, nodes: [
    { id: "prompt", type: "prompt", schema_version: 1, title: "当前提示词", position: { x: 0, y: 0 }, config: { text: "CURRENT prompt" }, ui: {} },
    { id: "asset", type: "image_asset", schema_version: 1, title: "当前图片", position: { x: 0, y: 230 }, config: { image_id: "current-image" }, ui: {} },
    { id: "source", type: "image_generate", schema_version: 1, title: "上游生成", position: { x: 320, y: 230 }, config: { count: 1 }, ui: {} },
    { id: "target", type: "image_generate", schema_version: 1, title: "历史检查", position: { x: 320, y: 0 }, config: { count: 1 }, ui: {} },
    { id: "video", type: "video_asset", schema_version: 1, title: "待准备视频", position: { x: 640, y: 230 }, config: { video_id: "history-video" }, ui: {} },
  ], edges: [
    { id: "current-prompt", source_node_id: "prompt", source_handle: "text", target_node_id: "target", target_handle: "prompt", data_type: "text", binding_mode: "follow_active", order: 0 },
    { id: "current-image", source_node_id: "asset", source_handle: "image", target_node_id: "target", target_handle: "references", data_type: "image", binding_mode: "follow_active", order: 0 },
  ], frames: [], settings: { snap_to_grid: false, grid_size: 16 } };
}
function historical(id: string, prompt = "SAVED prompt"): CanvasHistoricalExecution {
  return { id, node_id: "target", node_type: "image_generate", status: "succeeded", outputs: [], created_at: NOW, updated_at: NOW,
    definition_hash: "definition-" + id, input_hash: "input-" + id, processor_version: "saved-processor-1",
    config_snapshot: { count: 2, quality: "2k", aspect_ratio: "1:1" },
    input_snapshot: { prompt, bindings: [
      { edge_id: "saved-text", source_node_id: "prompt", target_handle: "prompt", role: null, order: 0, binding_mode: "follow_active", text: prompt },
      { edge_id: "saved-image", source_node_id: "asset", target_handle: "references", role: "reference", order: 0, binding_mode: "follow_active",
        asset: { image_id: "SAVED-image", sha256: "a".repeat(64) } },
      { edge_id: "saved-source", source_node_id: "source", target_handle: "references", role: "style", order: 1, binding_mode: "follow_active",
        source_execution_id: "SAVED-source-execution", output_index: 2,
        asset: { image_id: "SAVED-generated-image", sha256: "b".repeat(64), source_execution_id: "SAVED-source-execution", output_index: 2 } },
    ] } };
}
function applyOperations(graph: CanvasGraph, operations: CanvasOperation[]) {
  for (const operation of operations) {
    if (operation.op === "add_node") graph.nodes.push(structuredClone(operation.node));
    if (operation.op === "add_edge") graph.edges.push(structuredClone(operation.edge));
    if (operation.op === "remove_nodes") {
      graph.nodes = graph.nodes.filter((item) => !operation.node_ids.includes(item.id));
      graph.edges = graph.edges.filter((edge) => !operation.node_ids.includes(edge.source_node_id) && !operation.node_ids.includes(edge.target_node_id));
    }
    if (operation.op === "remove_edges") graph.edges = graph.edges.filter((edge) => !operation.edge_ids.includes(edge.id));
    if (operation.op === "update_node_config") {
      const target = graph.nodes.find((item) => item.id === operation.node_id);
      if (target) target.config = structuredClone(operation.config);
    }
  }
}
async function fixture(page: Page) {
  await installAgentFixture(page, { canvasEnabled: true });
  const graph = initialGraph(), requests: { cursor: string | null; limit: string | null; nodeId: string }[] = [];
  const mutationBodies: { operations: CanvasOperation[]; mutation_id: string }[] = [], accepted = new Set<string>();
  const preparation: { body: Record<string, unknown>; key: string | null }[] = [], paid: string[] = [];
  const first = historical("historical-new"), older = historical("historical-older", "SAVED older prompt");
  const items = [first, ...Array.from({ length: 29 }, (_, index) => historical("historical-" + index))];
  const asset = { schema_version: 1, asset_id: "history-video", kind: "video", source_sha256: "c".repeat(64),
    mime: "video/mp4", size_bytes: 100, width: 640, height: 360, duration_ms: 1000,
    preparation_state: "failed", preparation_revision: 5, updated_at: NOW,
    locators: { original: "/api/videos/history-video/binary", preview: null, thumb: null } };
  let revision = 1, failNext = false, binary = 0, releasePreparation: (() => void) | undefined;
  const waitPreparation = new Promise<void>((resolve) => { releasePreparation = resolve; });
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/(?:execute|plans\/run|plans\/retry-failed|generations)(?:\?|$|\/)/u.test(new URL(request.url()).pathname)) paid.push(request.url());
  });
  async function historyPage(route: Route, url: URL) {
    requests.push({ cursor: url.searchParams.get("cursor"), limit: url.searchParams.get("limit"), nodeId: url.pathname.split("/").at(-2)! });
    if (url.searchParams.has("cursor") && failNext) {
      failNext = false;
      return route.fulfill({ status: 400, json: { detail: { error: { code: "history_unavailable", message: "history-page-unavailable" } } } });
    }
    return route.fulfill({ json: url.searchParams.has("cursor") ? { items: [older], next_cursor: null } : { items, next_cursor: CURSOR } });
  }
  await page.route("**/api/canvases/" + CANVAS + "**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith("/history")) return historyPage(route, url);
    if (url.pathname.endsWith("/mutations")) {
      const body = request.postDataJSON(); mutationBodies.push(body);
      if (!accepted.has(body.mutation_id)) { applyOperations(graph, body.operations); accepted.add(body.mutation_id); revision += 1; }
      return route.fulfill({ json: { revision } });
    }
    if (request.method() !== "GET") return route.fulfill({ status: 400, json: { detail: "Unexpected action" } });
    return route.fulfill({ json: { id: CANVAS, title: "历史审查", revision, graph, assets: [asset],
      selections: [{ node_id: "source", execution_id: "CURRENT-source-execution", output_index: 0, revision: 1 }],
      recent_executions: [first], execution_freshness: { "historical-new": { state: "fresh", reason: null } },
      active_runs: [], created_at: NOW, updated_at: NOW } });
  });
  await page.route("**/api/videos/history-video/preparation/retry", async (route) => {
    preparation.push({ body: route.request().postDataJSON(), key: route.request().headers()["idempotency-key"] ?? null });
    await waitPreparation;
    asset.preparation_revision = 6; asset.preparation_state = "ready";
    return route.fulfill({ json: { asset } });
  });
  await page.route("**/api/videos/history-video/binary", (route) => { binary += 1; return route.abort("connectionfailed"); });
  return { graph, first, older, requests, mutationBodies, preparation, paid,
    failNextPage() { failNext = true; }, finishPreparation() { releasePreparation?.(); }, binary: () => binary };
}
async function openInspector(page: Page, id = "target") {
  await expect(node(page, id)).toBeVisible();
  await node(page, id).locator("header").click({ position: { x: 20, y: 12 } });
  if (mobile(page)) await page.getByRole("button", { name: "打开检查器", exact: true }).click();
}
async function closeMobileInspector(page: Page) {
  if (mobile(page)) await page.getByRole("button", { name: "关闭检查器", exact: true }).click();
}
async function openHistory(page: Page) {
  await openInspector(page);
  await page.getByRole("button", { name: "查看完整执行历史", exact: true }).click();
  await expect(page.getByRole("button", { name: "比较 A", exact: true })).toHaveCount(30);
}

test("real history keeps A/B across bounded pages and failed-page retry; changing node clears comparisons", async ({ page }, info) => {
  const control = await fixture(page);
  await page.goto("/projects/canvas/" + CANVAS); await openInspector(page);
  expect(control.requests).toHaveLength(0);
  await page.getByRole("button", { name: "查看完整执行历史", exact: true }).click();
  await expect(page.getByRole("button", { name: "比较 A", exact: true })).toHaveCount(30);
  await page.getByRole("button", { name: "比较 A", exact: true }).first().click();
  const comparison = page.getByRole("region", { name: "历史 A/B 比较" });
  await expect(comparison).toContainText("historical-new");
  control.failNextPage();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByRole("button", { name: "重试本页", exact: true })).toBeVisible();
  await expect(comparison).toContainText("historical-new");
  await page.getByRole("button", { name: "重试本页", exact: true }).click();
  await expect(page.getByRole("button", { name: "比较 B", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "比较 B", exact: true }).click();
  await expect(comparison).toContainText("historical-older");
  await expect(comparison).toContainText("SAVED older prompt");
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await expect(page.getByRole("button", { name: "比较 A", exact: true })).toHaveCount(30);
  await expect(comparison).toContainText("historical-older");
  expect(control.requests.every((request) => request.limit === "30" && request.nodeId === "target")).toBe(true);
  expect(control.requests.map((request) => request.cursor)).toEqual([null, CURSOR, CURSOR, null]);
  await info.attach("history-immutable-cross-page-comparison", { body: await page.screenshot(), contentType: "image/png" });
  await closeMobileInspector(page); await openInspector(page, "source");
  await expect(page.getByRole("region", { name: "历史 A/B 比较" })).toHaveCount(0);
  expect(control.paid).toHaveLength(0);
});

test("real historical branch saves exact pins and literals in one transaction; undo removes it with zero execute requests", async ({ page }, info) => {
  const control = await fixture(page), originalCount = control.graph.nodes.length;
  await page.goto("/projects/canvas/" + CANVAS); await openHistory(page);
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.getByRole("button", { name: "从此记录分支", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "从此记录分支", exact: true }).click();
  await expect.poll(() => control.mutationBodies.length).toBe(1);
  const operations = control.mutationBodies[0].operations;
  const addedNodes = operations.flatMap((op) => op.op === "add_node" ? [op.node] : []);
  const addedEdges = operations.flatMap((op) => op.op === "add_edge" ? [op.edge] : []);
  expect(operations.every((op) => ["add_node", "add_edge"].includes(op.op))).toBe(true);
  expect(addedNodes).toHaveLength(3);
  expect(addedNodes.find((item) => item.type === "image_generate")?.config).toEqual(control.older.config_snapshot);
  expect(addedNodes.find((item) => item.type === "prompt")?.config.text).toBe("SAVED older prompt");
  expect(addedNodes.find((item) => item.type === "image_asset")?.config.image_id).toBe("SAVED-image");
  const pinned = addedEdges.find((edge) => edge.binding_mode === "pinned") as CanvasEdgeDefinition;
  expect(pinned.source_node_id).toBe("source"); expect(pinned.pinned_execution_id).toBe("SAVED-source-execution"); expect(pinned.pinned_output_index).toBe(2);
  await expect.poll(() => control.graph.nodes.length).toBe(originalCount + 3);
  await closeMobileInspector(page);
  if (mobile(page)) {
    await page.getByRole("button", { name: "更多画布操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "撤销", exact: true }).click();
  } else await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect.poll(() => control.graph.nodes.length).toBe(originalCount);
  expect(control.paid).toHaveLength(0);
  await info.attach("historical-branch-undone-no-generation", { body: await page.screenshot(), contentType: "image/png" });
});

test("real preparation retry fences double click and sends only saved hash/revision to preparation endpoint", async ({ page }, info) => {
  const control = await fixture(page);
  await page.goto("/projects/canvas/" + CANVAS); await openInspector(page, "video");
  const retry = page.getByRole("button", { name: "重试素材准备", exact: true });
  await expect(retry).toBeEnabled();
  await retry.evaluate((element) => { (element as HTMLButtonElement).click(); (element as HTMLButtonElement).click(); });
  await expect.poll(() => control.preparation.length).toBe(1); await expect(retry).toBeDisabled();
  expect(control.preparation[0].key).toBeTruthy();
  expect(control.preparation[0].body).toEqual({ expected_source_sha256: "c".repeat(64), expected_preparation_revision: 5,
    idempotency_key: control.preparation[0].key });
  control.finishPreparation();
  await expect(retry).toHaveCount(0);
  expect(control.preparation).toHaveLength(1); expect(control.paid).toHaveLength(0); expect(control.binary()).toBe(0);
  await expect(page.locator("video")).toHaveCount(0);
  await info.attach("preparation-retry-existing-video-only", { body: await page.screenshot(), contentType: "image/png" });
});
