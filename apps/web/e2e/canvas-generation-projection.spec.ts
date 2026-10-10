import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";
const NOW = "2026-10-10T10:00:00Z";
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64");
type Source = EventTarget & { readyState: number; url: string };
type FixtureWindow = Window & { __canvasSources: Source[] };
async function installStream(page: Page) {
  await page.addInitScript(() => {
    const sources: Source[] = [];
    (window as unknown as FixtureWindow).__canvasSources = sources;
    class FixtureEventSource extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
      readyState = 0; url: string;
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      constructor(url: string) {
        super(); this.url = String(url); sources.push(this);
        queueMicrotask(() => { this.readyState = 1; this.onopen?.(new Event("open")); });
      }
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, "EventSource", { value: FixtureEventSource, configurable: true });
  });
}
async function emit(page: Page, seq: number) {
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as FixtureWindow).__canvasSources.filter((source) => source.readyState === 1).length)).toBeGreaterThan(0);
  await page.evaluate((sequence) => {
    const payload = { schema_version: 1, canvas_id: "canvas-b", run_id: "run-b", seq: sequence,
      execution_id: "execution-b", event_type: "canvas.execution.status_changed",
      event_id: "canvas-run:run-b:" + sequence };
    for (const source of (window as unknown as FixtureWindow).__canvasSources) {
      if (source.readyState === 1) source.dispatchEvent(new MessageEvent("canvas.run.updated",
        { data: JSON.stringify(payload), lastEventId: sequence * 1000 + "-0" }));
    }
  }, seq);
}
async function fixture(page: Page) {
  await installAgentFixture(page, { canvasEnabled: true });
  await installStream(page);
  const graph = { schema_version: 1, nodes: [
    { id: "prompt", type: "prompt", schema_version: 1, title: "提示词", position: { x: 0, y: 0 },
      config: { text: "Preserve this draft" }, ui: {} },
    { id: "asset", type: "video_asset", schema_version: 1, title: "素材", position: { x: 300, y: 0 },
      config: { video_id: "video-b" }, ui: {} },
    { id: "generated", type: "image_generate", schema_version: 1, title: "生成", position: { x: 0, y: 260 },
      config: {}, ui: {} },
  ], edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 } };
  const asset = { schema_version: 1, asset_id: "video-b", kind: "video", source_sha256: "a".repeat(64),
    mime: "video/mp4", size_bytes: 100, width: null as number | null, height: null as number | null,
    duration_ms: null as number | null, preparation_state: "pending", preparation_revision: 1, updated_at: NOW,
    locators: { original: "/api/videos/video-b/binary", preview: "/api/videos/video-b/binary", thumb: null as string | null } };
  let revision = 4, snapshots = 0, batches = 0, details = 0, binary = 0, submits = 0, gap = false, currentSeq = 0;
  const execution = () => ({ id: "execution-b", run_id: "run-b", node_id: "generated", node_type: "image_generate",
    status: "succeeded", outputs: [], updated_at: NOW, tasks: [] });
  await page.route("**/api/canvases/canvas-b**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.pathname.endsWith("/mutations")) {
      for (const operation of request.postDataJSON().operations ?? []) {
        const node = graph.nodes.find((item) => item.id === operation.node_id);
        if (node && operation.op === "update_node_config") node.config = operation.config;
      }
      return route.fulfill({ json: { revision: ++revision } });
    }
    if (request.method() !== "GET") { submits += 1; return route.fulfill({ status: 400, json: { detail: "No paid actions in fixture" } }); }
    if (url.pathname.endsWith("/event-batch")) {
      batches += 1; const after = Number(url.searchParams.get("after_seq"));
      return route.fulfill({ json: { items: [{ run_id: "run-b", seq: currentSeq, payload: {} }],
        after_seq: after, next_after_seq: currentSeq, last_event_seq: currentSeq, has_more: false, snapshot_required: gap } });
    }
    if (url.pathname.endsWith("/runs/run-b")) {
      details += 1; return route.fulfill({ json: { id: "run-b", status: "succeeded", last_event_seq: currentSeq,
        executions: [execution()] } });
    }
    snapshots += 1;
    return route.fulfill({ json: { id: "canvas-b", title: "资产与增量测试", revision, graph,
      assets: [asset], selections: [], recent_executions: [], active_runs: [], created_at: NOW, updated_at: NOW } });
  });
  await page.route("**/api/videos/video-b/binary", (route) => { binary += 1; return route.abort("connectionfailed"); });
  await page.route("**/api/fixture-poster.png", (route) => route.fulfill({ contentType: "image/png", body: PNG }));
  return { asset, setSeq(value: number, withGap = false) { currentSeq = value; gap = withGap; },
    counts: () => ({ snapshots, batches, details, binary, submits }) };
}
const node = (page: Page, id: string) => page.locator('.react-flow__node[data-id="' + id + '"]');

test("asset preparation refreshes without active runs and preserves a real edited Canvas", async ({ page }, info) => {
  const control = await fixture(page);
  await page.goto("/projects/canvas/canvas-b");
  const preview = node(page, "asset").locator("[data-canvas-preview-state]");
  await expect(preview).toHaveAttribute("data-canvas-preview-state", "processing");
  await expect(node(page, "asset")).toContainText("正在准备素材");
  expect(control.counts().binary).toBe(0);
  const editor = page.getByRole("textbox", { name: "编辑提示词内容" });
  const saved = page.waitForResponse((response) => response.url().endsWith("/mutations") && response.ok());
  const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/canvases/canvas-b" && response.request().method() === "GET" && response.ok());
  await editor.fill("A draft survives asset preparation"); await editor.blur();
  await saved;
  // Complete the save-triggered refresh before changing preparation state.
  await refreshed;
  await expect(preview).toHaveAttribute("data-canvas-preview-state", "processing");
  await editor.evaluate((element) => element.setAttribute("data-node-identity", "kept"));
  Object.assign(control.asset, { preparation_state: "ready", preparation_revision: 2,
    width: 640, height: 360, duration_ms: 5000 });
  await expect(preview).toHaveAttribute("data-canvas-asset-preparation", "ready", { timeout: 15_000 });
  await expect(node(page, "asset")).toContainText("暂无海报，点击播放视频");
  await expect(node(page, "asset").locator("img")).toHaveCount(0);
  await expect(editor).toHaveValue("A draft survives asset preparation");
  await expect(editor).toHaveAttribute("data-node-identity", "kept");
  expect(control.counts().submits).toBe(0); expect(control.counts().binary).toBe(0);
  await info.attach("asset-preparation-ready-no-poster", { body: await page.screenshot(), contentType: "image/png" });
});

test("existing global SSE applies run increments, deduplicates and recovers gaps without losing draft", async ({ page }, info) => {
  const control = await fixture(page);
  Object.assign(control.asset, { preparation_state: "ready", preparation_revision: 2 });
  await page.goto("/projects/canvas/canvas-b");
  const editor = page.getByRole("textbox", { name: "编辑提示词内容" });
  const savedSnapshot = page.waitForResponse(async (response) =>
    new URL(response.url()).pathname === "/api/canvases/canvas-b" &&
    response.request().method() === "GET" && response.ok() &&
    (await response.json()).revision >= 5);
  await expect(editor).toBeEditable(); await editor.fill("SSE keeps the in-progress draft");
  await editor.evaluate((element) => element.setAttribute("data-node-identity", "kept"));
  control.setSeq(1); await emit(page, 1);
  await expect.poll(() => control.counts().details).toBe(1);
  expect(control.counts().batches).toBe(1);
  await emit(page, 1); await page.waitForTimeout(150);
  expect(control.counts().batches).toBe(1);
  const before = control.counts().snapshots;
  Object.assign(control.asset, { source_sha256: "b".repeat(64), preparation_revision: 0,
    preparation_state: "ready", locators: { ...control.asset.locators, thumb: "/api/fixture-poster.png" } });
  control.setSeq(3, true); await emit(page, 3);
  await expect.poll(() => control.counts().snapshots).toBeGreaterThan(before);
  await expect(node(page, "asset").locator("img")).toHaveAttribute("src", "/api/fixture-poster.png");
  await expect(editor).toHaveValue("SSE keeps the in-progress draft");
  await expect(editor).toHaveAttribute("data-node-identity", "kept");
  expect(control.counts().submits).toBe(0); expect(control.counts().binary).toBe(0);
  await info.attach("canvas-sse-gap-restored", { body: await page.screenshot(), contentType: "image/png" });
  // Finish the legitimate draft save/refresh before measuring hidden notice reads.
  // Visibility loss also flushes drafts and must not be mistaken for SSE polling.
  await editor.blur(); await savedSnapshot;
  const hiddenBaseline = control.counts();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  Object.assign(control.asset, { source_sha256: "c".repeat(64), preparation_revision: 1,
    preparation_state: "pending" });
  control.setSeq(4); await emit(page, 4); await page.waitForTimeout(150);
  expect(control.counts().snapshots).toBe(hiddenBaseline.snapshots);
  expect(control.counts().batches).toBe(hiddenBaseline.batches);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => control.counts().snapshots).toBeGreaterThan(hiddenBaseline.snapshots);
  await expect(node(page, "asset").locator("[data-canvas-preview-state]"))
    .toHaveAttribute("data-canvas-preview-state", "processing");
  await expect(editor).toHaveValue("SSE keeps the in-progress draft");
  await expect(editor).toHaveAttribute("data-node-identity", "kept");
  expect(control.counts().submits).toBe(0); expect(control.counts().binary).toBe(0);
});

test("Canvas increments cross tabs through the existing single global stream", async ({ page }) => {
  const first = await fixture(page);
  Object.assign(first.asset, { preparation_state: "ready" });
  await page.goto("/projects/canvas/canvas-b");
  await expect(page.getByRole("textbox", { name: "编辑提示词内容" })).toBeEditable();
  const peer = await page.context().newPage();
  const second = await fixture(peer);
  Object.assign(second.asset, { preparation_state: "ready" });
  await peer.goto("/projects/canvas/canvas-b");
  await expect(peer.getByRole("textbox", { name: "编辑提示词内容" })).toBeEditable();
  const openCount = (target: Page) => target.evaluate(() =>
    (window as unknown as FixtureWindow).__canvasSources.filter((source) => source.readyState === 1).length);
  await expect.poll(async () => (await openCount(page)) + (await openCount(peer))).toBe(1);
  first.setSeq(1); second.setSeq(1);
  const leader = await openCount(page) ? page : peer;
  await emit(leader, 1);
  await expect.poll(() => first.counts().details).toBe(1);
  await expect.poll(() => second.counts().details).toBe(1);
  expect(first.counts().submits + second.counts().submits).toBe(0);
  await peer.close();
});
