import { mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { canvasNode, installScaleFixture } from "./canvas-scale-fixture";

interface Sample { name: string; elapsedMs: number; framesMs: number[]; longTasksMs: number[]; domNodes: number; domEdges: number; images: number; }
type Meter = { start: number; last: number; frames: number[]; longTasks: number[]; active: boolean };
type TouchTrace = { type: string; time: number; target: string; path: string[] };
type MeterWindow = Window & { __scaleMeter: Meter; __scaleTouchTrace: TouchTrace[] };
const activePhases = new WeakMap<Page, string>();
const rounds = Number(process.env.LUMEN_SCALE_ROUNDS || 10);
const loads = Number(process.env.LUMEN_SCALE_LOADS || 5);
const settle = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
const nodeGrip = (page: Page, id: string) => canvasNode(page, id).locator("header > svg").first();
async function emptyPanePoint(page: Page) {
  return page.locator(".react-flow__pane").evaluate((pane) => {
    const r = pane.getBoundingClientRect();
    for (let y = r.top + 12; y < r.bottom - 60; y += 12) for (let x = r.left + 12; x < r.right - 60; x += 12) {
      if (document.elementFromPoint(x, y) === pane) return { x, y };
    }
    throw new Error("No genuine empty pane point found");
  });
}
const viewportStyle = (page: Page) => page.locator(".react-flow__viewport").getAttribute("style");
async function visibleNodeIds(page: Page) {
  return page.locator(".react-flow__pane").evaluate((pane) => {
    const view = pane.getBoundingClientRect();
    const left = Math.max(0, view.left), top = Math.max(0, view.top);
    const right = Math.min(innerWidth, view.right), bottom = Math.min(innerHeight, view.bottom);
    return Array.from(pane.querySelectorAll<HTMLElement>(".react-flow__node")).filter((node) => {
      const r = node.getBoundingClientRect(), style = getComputedStyle(node);
      return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none"
        && r.right > left && r.left < right && r.bottom > top && r.top < bottom;
    }).map((node) => node.dataset.id);
  });
}
async function readView(page: Page, id: string) {
  return page.evaluate((id) => {
    const node = document.querySelector('.react-flow__node[data-id="' + id + '"]');
    const grip = node?.querySelector("header > svg");
    const box = grip?.getBoundingClientRect();
    const describe = (element: Element | null) => element ? { tag: element.tagName, dataId: element.getAttribute("data-id"), label: element.getAttribute("aria-label"), class: element.getAttribute("class") } : null;
    const centre = box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null;
    return {
      viewport: document.querySelector(".react-flow__viewport")?.getAttribute("style"),
      canvasBounds: document.querySelector(".react-flow__pane")?.getBoundingClientRect().toJSON(),
      domNodes: document.querySelectorAll(".react-flow__node").length,
      domEdges: document.querySelectorAll(".react-flow__edge").length,
      gripBounds: box?.toJSON(), centre,
      centreTarget: centre ? describe(document.elementFromPoint(centre.x, centre.y)) : null,
      centreStack: centre ? document.elementsFromPoint(centre.x, centre.y).slice(0, 8).map(describe) : [],
      nodeStyle: node?.getAttribute("style"), nodeClass: node?.getAttribute("class"),
      touchTrace: (window as unknown as MeterWindow).__scaleTouchTrace ?? [],
    };
  }, id);
}
async function visibleButton(page: Page, name: string) { return page.getByRole("button", { name, exact: true }).filter({ visible: true }).first(); }
async function closeInspector(page: Page) {
  const close = await visibleButton(page, "关闭检查器");
  if (await close.count()) await close.click();
}
async function focusNode(page: Page, id: string) {
  await closeInspector(page);
  await page.keyboard.press("Escape");
  await page.keyboard.press("ControlOrMeta+0");
  await settle(page);
  await nodeGrip(page, id).click();
  await expect(canvasNode(page, id)).toHaveClass(/selected/);
  const fit = await visibleButton(page, "适应选区");
  if (await fit.count()) await fit.click();
  else await page.keyboard.press("Shift+2");
  // Use Playwright actionability/stability checks rather than a guessed animation delay.
  await nodeGrip(page, id).hover();
  await expect(canvasNode(page, id)).toHaveClass(/selected/);
}
function distribution(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b);
  const q = (p: number) => sorted[Math.max(0, Math.ceil(sorted.length * p) - 1)] ?? null;
  return { count: values.length, min: sorted[0] ?? null, median: q(0.5), p95: q(0.95), max: sorted.at(-1) ?? null };
}
async function installMeter(page: Page) {
  await page.addInitScript((traceEnabled: boolean) => {
    const meter: Meter = { start: 0, last: 0, frames: [], longTasks: [], active: false };
    (window as unknown as MeterWindow).__scaleMeter = meter;
    const trace: TouchTrace[] = [];
    (window as unknown as MeterWindow).__scaleTouchTrace = trace;
    const describe = (target: EventTarget | null) => target instanceof Element
      ? target.tagName + (target.getAttribute("data-id") ? "[data-id=" + target.getAttribute("data-id") + "]" : "") + (target.getAttribute("aria-label") ? "[aria-label=" + target.getAttribute("aria-label") + "]" : "") + "." + target.className.toString().slice(0, 120)
      : String(target);
    if (traceEnabled) for (const type of ["touchstart", "touchend", "pointerdown", "click"]) document.addEventListener(type, (event) => {
      trace.push({ type, time: performance.now(), target: describe(event.target), path: event.composedPath().slice(0, 7).map(describe) });
      if (trace.length > 30) trace.shift();
    }, { capture: true, passive: true });
    const frame = (now: number) => {
      if (meter.active && meter.last) meter.frames.push(now - meter.last);
      meter.last = now; requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    if (PerformanceObserver.supportedEntryTypes.includes("longtask")) new PerformanceObserver((list) => {
      if (meter.active) meter.longTasks.push(...list.getEntries().map((e) => e.duration));
    }).observe({ entryTypes: ["longtask"] });
  }, process.env.LUMEN_SCALE_TRACE_TOUCH === "1");
}
async function measure(page: Page, samples: Sample[], name: string, action: () => Promise<unknown>) {
  await page.evaluate(() => {
    const m = (window as unknown as MeterWindow).__scaleMeter;
    Object.assign(m, { start: performance.now(), last: 0, frames: [], longTasks: [], active: true });
  });
  activePhases.set(page, name);
  console.log("SCALE_START", name);
  await action(); await settle(page);
  console.log("SCALE_END", name);
  const sample = await page.evaluate((name) => {
    const m = (window as unknown as MeterWindow).__scaleMeter; m.active = false;
    return { name, elapsedMs: performance.now() - m.start, framesMs: m.frames, longTasksMs: m.longTasks,
      domNodes: document.querySelectorAll(".react-flow__node").length, domEdges: document.querySelectorAll(".react-flow__edge").length,
      images: document.querySelectorAll(".react-flow__node img").length };
  }, name);
  samples.push(sample);
  activePhases.set(page, "between-measurements");
}

for (const count of [100, 500, 1000]) test("real scale " + count + " nodes operation and media acceptance", async ({ page, browser }, info) => {
  // Observation budget, not a responsiveness pass/fail threshold.
  test.setTimeout(360_000);
  const fixture = await installScaleFixture(page, count);
  const promptId = "n-" + fixture.anchor, imageId = "n-" + (fixture.anchor + 1), videoId = "n-" + (fixture.anchor + 2);
  await installMeter(page);
  const samples: Sample[] = [], navigation: Array<{ state: string; elapsedMs: number; browserReadyMs: number; domNodes: number; domEdges: number }> = [];
  const errors: string[] = [];
  let completed = false;
  let appTelemetry: unknown = null;
  let beforeTouch: unknown = null;
  let overviewBeforeTouch: unknown = null;
  const touchZoomSetups: unknown[] = [];
  const undoVisibility: unknown[] = [];
  const dragOrigin = { ...fixture.graph.nodes[fixture.anchor]!.position };
  const dragResets: unknown[] = [];
  let finalView: unknown = null;
  let screenshotError: string | null = null;
  const pageErrorDetails: Array<{ message: string; stack?: string; phase: string; timestamp: string }> = [];
  page.on("pageerror", (error) => {
    errors.push(error.message);
    pageErrorDetails.push({ message: error.message, stack: error.stack, phase: activePhases.get(page) ?? "navigation-or-setup", timestamp: new Date().toISOString() });
  });
  const environment = { timestamp: new Date().toISOString(), platform: os.platform(), release: os.release(), arch: os.arch(),
    cpu: os.cpus()[0]?.model, logicalCpus: os.cpus().length, totalMemory: os.totalmem(), freeMemoryBefore: os.freemem(), loadBefore: os.loadavg(),
    node: process.version, browser: browser.version(), project: info.project.name, viewport: info.project.use.viewport,
    browserTelemetryContext: "about:blank before app navigation",
    browserTelemetry: await page.evaluate(() => ({ longTaskSupported: PerformanceObserver.supportedEntryTypes.includes("longtask"), devicePixelRatio, hardwareConcurrency: navigator.hardwareConcurrency, userAgent: navigator.userAgent })),
    reducedMotion: info.project.use.contextOptions?.reducedMotion ?? "no-preference", appMode: "Next dev webpack; existing build cache; no CPU throttle",
    cache: "Fresh Playwright context for each scale; then same-context warm reloads. Route interception disables HTTP cache." };
  const reportPath = path.resolve(process.env.LUMEN_SCALE_RESULTS || "../../tmp/lumen-core-ui-joint/scale-results", info.project.name + "-" + count + ".json");
  try {
    for (let i = 0; i < loads; i++) {
      const started = Date.now();
      if (i === 0) await page.goto("/projects/canvas/canvas-scale"); else await page.reload();
      await expect(canvasNode(page, "n-0")).toBeAttached();
      await expect(page.locator(".react-flow__edge").first()).toBeAttached();
      await settle(page);
      navigation.push({ state: i === 0 ? "fresh-context" : "warm-reload-" + i, elapsedMs: Date.now() - started,
        browserReadyMs: await page.evaluate(() => performance.now()),
        domNodes: await page.locator(".react-flow__node").count(), domEdges: await page.locator(".react-flow__edge").count() });
    }
    appTelemetry = await page.evaluate(() => ({ devicePixelRatio, innerWidth, innerHeight,
      visualViewport: window.visualViewport ? { width: window.visualViewport.width, height: window.visualViewport.height, scale: window.visualViewport.scale } : null,
      viewportMeta: document.querySelector('meta[name="viewport"]')?.getAttribute("content") }));
    expect(fixture.counts.video).toBe(0); await expect(page.locator("video")).toHaveCount(0);
    for (let i = 0; i < rounds; i++) {
      await page.keyboard.press("Escape");
      await measure(page, samples, "fit-all", async () => { await page.keyboard.press("ControlOrMeta+0"); await expect(canvasNode(page, promptId)).toBeVisible(); });
      if (info.project.use.hasTouch) {
        if (i === 0) {
          overviewBeforeTouch = await readView(page, imageId);
          await mkdir(path.dirname(reportPath), { recursive: true });
          await page.screenshot({ path: reportPath.replace(/\.json$/, "-overview.png") });
        }
        if (process.env.LUMEN_SCALE_TOUCH_OVERVIEW !== "1") {
          // Real app shortcut. Setup is outside select timing; no store/API mutation,
          // forced tap, mouse selection or synthetic pinch is used here.
          await page.keyboard.press("0");
          await expect.poll(async () => (await nodeGrip(page, imageId).boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(15);
          await settle(page);
        }
        const view = await readView(page, imageId);
        touchZoomSetups.push({ round: i, viewport: view.viewport, gripBounds: view.gripBounds, centreTarget: view.centreTarget });
      }
      if (i === 0 && info.project.use.hasTouch) {
        beforeTouch = await readView(page, imageId);
        await mkdir(path.dirname(reportPath), { recursive: true });
        await page.screenshot({ path: reportPath.replace(/\.json$/, "-before-touch.png") });
      }
      await measure(page, samples, "select", async () => {
        const header = nodeGrip(page, imageId);
        if (info.project.use.hasTouch) await header.tap();
        else await header.click();
        await expect(canvasNode(page, imageId)).toHaveClass(/selected/);
      });
      const desktopInspector = (info.project.use.viewport?.width ?? 0) >= 1200;
      if (desktopInspector) { const p = await emptyPanePoint(page); await page.mouse.click(p.x, p.y); }
      await measure(page, samples, "inspector", async () => {
        if (desktopInspector) await nodeGrip(page, imageId).click();
        else await (await visibleButton(page, "打开检查器")).click();
        await expect(page.getByRole("button", { name: /个图片输出/ }).filter({ visible: true }).first()).toBeVisible();
      });
      const output = i % 2 ? 0 : 1;
      await measure(page, samples, "output-selection", async () => {
        await page.getByRole("button", { name: "选择第 " + (output + 1) + " 个图片输出", exact: true }).filter({ visible: true }).click();
        await expect(page.getByRole("button", { name: "当前第 " + (output + 1) + " 个图片输出", exact: true }).filter({ visible: true })).toHaveAttribute("aria-pressed", "true");
        await expect.poll(() => page.locator('img[src*="/api/scale-media/"]').filter({ visible: true }).first().evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(640);
      });
      await closeInspector(page);
      await focusNode(page, promptId);
      const header = nodeGrip(page, promptId);
      await header.hover();
      const beforeDrag = await canvasNode(page, promptId).evaluate((node) => (node as HTMLElement).style.transform);
      const beforeDragWorld = { ...fixture.graph.nodes[fixture.anchor]!.position };
      const beforeDragNodes = structuredClone(fixture.graph.nodes);
      const beforeDragSelections = structuredClone(fixture.selections);
      const beforeDragMutation = fixture.counts.mutations;
      await measure(page, samples, "drag", async () => {
        const box = (await header.boundingBox())!;
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.mouse.down(); await page.mouse.move(box.x + box.width / 2 + 50, box.y + box.height / 2 + 35, { steps: 8 }); await page.mouse.up();
        await expect.poll(() => canvasNode(page, promptId).evaluate((node) => (node as HTMLElement).style.transform)).not.toBe(beforeDrag);
      });
      // The last graph edit is this measured drag. Real undo restores that
      // transaction outside timing, without accumulating overlap or pixel error.
      activePhases.set(page, "drag-reset-" + i);
      console.log("SCALE_RESET_START", i);
      const resetStarted = Date.now();
      const reset = { round: i, method: "real keyboard undo", beforeDragWorld,
        afterDragWorld: { ...fixture.graph.nodes[fixture.anchor]!.position },
        beforeDragMutation, beforeResetMutation: fixture.counts.mutations,
        afterResetMutation: fixture.counts.mutations, viewportBefore: await viewportStyle(page),
        viewportAfter: null as string | null, target: dragOrigin,
        actual: { ...fixture.graph.nodes[fixture.anchor]!.position }, elapsedMs: 0, completed: false };
      dragResets.push(reset);
      try {
        await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(beforeDragMutation);
        await expect.poll(() => Math.hypot(fixture.graph.nodes[fixture.anchor]!.position.x - beforeDragWorld.x, fixture.graph.nodes[fixture.anchor]!.position.y - beforeDragWorld.y)).toBeGreaterThan(0);
        await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
        reset.afterDragWorld = { ...fixture.graph.nodes[fixture.anchor]!.position };
        reset.beforeResetMutation = fixture.counts.mutations;
        reset.viewportBefore = await viewportStyle(page);
        await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
        await page.keyboard.press("ControlOrMeta+z");
        await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(reset.beforeResetMutation);
        await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
        await expect.poll(() => fixture.graph.nodes[fixture.anchor]!.position).toEqual(dragOrigin);
        expect(fixture.graph.nodes).toEqual(beforeDragNodes);
        expect(fixture.selections).toEqual(beforeDragSelections);
        await settle(page);
        expect(await viewportStyle(page)).toBe(reset.viewportBefore);
        reset.completed = true;
      } finally {
        reset.actual = { ...fixture.graph.nodes[fixture.anchor]!.position };
        reset.afterResetMutation = fixture.counts.mutations;
        reset.viewportAfter = await viewportStyle(page).catch(() => null);
        reset.elapsedMs = Date.now() - resetStarted;
      }
      console.log("SCALE_RESET_END", i);
      activePhases.set(page, "between-measurements");
      await measure(page, samples, "pan", async () => {
        const before = await viewportStyle(page), point = await emptyPanePoint(page);
        await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
        await page.keyboard.down("Space"); await page.mouse.move(point.x, point.y);
        await page.mouse.down(); await page.mouse.move(point.x + 50, point.y + 30, { steps: 8 });
        await page.mouse.up(); await page.keyboard.up("Space");
        await expect.poll(() => viewportStyle(page)).not.toBe(before);
      });
      await measure(page, samples, "zoom", async () => {
        const before = await viewportStyle(page);
        await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
        const zoom = await visibleButton(page, i % 2 ? "放大画布" : "缩小画布");
        if (await zoom.count()) await zoom.click(); else await page.keyboard.press(i % 2 ? "+" : "-");
        await expect.poll(() => viewportStyle(page)).not.toBe(before);
      });
    }
    expect(dragResets).toHaveLength(rounds);
    await focusNode(page, promptId);
    const editor = canvasNode(page, promptId).getByRole("textbox", { name: "编辑提示词内容" });
    await editor.fill("Scale draft " + count); await editor.blur();
    await expect.poll(() => fixture.graph.nodes[fixture.anchor]!.config.text).toBe("Scale draft " + count);
    await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
    await page.waitForTimeout(300);
    await nodeGrip(page, promptId).click();
    await settle(page);
    const transform = await viewportStyle(page);
    await editor.evaluate((element) => element.setAttribute("data-scale-identity", "retained"));
    for (let i = 0; i < rounds; i++) {
      for (const gap of [false, true]) {
        const next = fixture.next(gap), before = { ...fixture.counts };
        await measure(page, samples, gap ? "snapshot-gap-burst" : "sse-burst-32", async () => {
          await fixture.emit(next.seq);
          await expect.poll(() => gap ? fixture.counts.snapshots : fixture.counts.details).toBeGreaterThan(gap ? before.snapshots : before.details);
          await expect(editor).toHaveValue("Scale draft " + count);
          await expect(editor).toHaveAttribute("data-scale-identity", "retained");
          await expect(canvasNode(page, promptId)).toHaveClass(/selected/);
          expect(await viewportStyle(page)).toBe(transform);
          expect(fixture.counts.batches - before.batches).toBeLessThanOrEqual(1);
        });
      }
    }
    const hiddenBaseline = { ...fixture.counts };
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await fixture.emit(fixture.next().seq, 100);
    await page.waitForTimeout(5500);
    expect(fixture.counts.snapshots).toBe(hiddenBaseline.snapshots);
    expect(fixture.counts.batches).toBe(hiddenBaseline.batches);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => fixture.counts.snapshots).toBeGreaterThan(hiddenBaseline.snapshots);
    await focusNode(page, imageId);
    await expect(canvasNode(page, imageId).getByRole("button", { name: "节点运行中", exact: true })).toBeVisible();
    await focusNode(page, videoId);
    expect(fixture.counts.video).toBe(0);
    for (let i = 0; i < rounds; i++) {
    await measure(page, samples, i === 0 ? "video-first-decode" : "video-repeat-decode", async () => {
      await canvasNode(page, videoId).getByRole("button", { name: "播放Scale " + (fixture.anchor + 2) + "视频预览" }).click();
      await expect(page.locator("video")).toHaveCount(1);
      if (fixture.dimensions.mp4Bytes) await expect.poll(() => page.locator("video").evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
      else await expect(page.getByText("视频载入失败", { exact: true })).toBeVisible();
    });
    const video = page.locator("video");
    const decoded = await video.evaluate((v: HTMLVideoElement) => ({ readyState: v.readyState, width: v.videoWidth, height: v.videoHeight, currentTime: v.currentTime }));
    expect(decoded.width).toBeGreaterThan(0);
    await page.getByRole("dialog").getByRole("button", { name: "关闭视频预览", exact: true }).click();
    await expect(page.locator("video")).toHaveCount(0);
    const afterClose = fixture.counts.video; await page.waitForTimeout(200);
    expect(fixture.counts.video).toBe(afterClose);
    }
    for (let i = 0; i < rounds; i++) {
      await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
      await page.keyboard.press("ControlOrMeta+a");
      await measure(page, samples, "layout-all", async () => {
        const before = fixture.counts.mutations;
        await page.keyboard.press("Shift+A");
        await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(before);
        await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
      });
      const beforeUndo = fixture.counts.mutations;
      await page.keyboard.press("ControlOrMeta+z");
      await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(beforeUndo);
      await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
      // Correctness check outside the layout timing: restoring graph coordinates
      // must not leave all rendered nodes outside the actual pane intersection.
      await expect.poll(async () => (await visibleNodeIds(page)).length).toBeGreaterThan(0);
      undoVisibility.push({ round: i, visibleNodeIds: await visibleNodeIds(page), viewport: await viewportStyle(page) });
    }
    expect(fixture.counts.prohibited).toBe(0);
    expect(errors).toEqual([]);
    completed = true;
  } finally {
    await mkdir(path.dirname(reportPath), { recursive: true });
    finalView = await readView(page, imageId).catch((error: unknown) => ({ unavailable: String(error) }));
    if (!page.isClosed()) {
      const screenshotPath = reportPath.replace(/\.json$/, "-final.png");
      await page.screenshot({ path: screenshotPath }).then(() => info.attach("scale-canvas-" + count, { path: screenshotPath, contentType: "image/png" })).catch((error: unknown) => { screenshotError = String(error); });
    }
    const xs = fixture.graph.nodes.map((node) => node.position.x), ys = fixture.graph.nodes.map((node) => node.position.y);
    const graphBounds = { left: Math.min(...xs), top: Math.min(...ys),
      right: Math.max(...fixture.graph.nodes.map((node) => node.position.x + (node.size?.width ?? 260))),
      bottom: Math.max(...fixture.graph.nodes.map((node) => node.position.y + (node.size?.height ?? 220))) };
    const groups = Object.fromEntries([...new Set(samples.map((s) => s.name))].map((name) => [name, {
      elapsed: distribution(samples.filter((s) => s.name === name).map((s) => s.elapsedMs)),
      frames: distribution(samples.filter((s) => s.name === name).flatMap((s) => s.framesMs)),
      longTasks: distribution(samples.filter((s) => s.name === name).flatMap((s) => s.longTasksMs)),
    }]));
    const report = { environment: { ...environment, appTelemetry, loadAfter: os.loadavg(), freeMemoryAfter: os.freemem() },
      dimensions: { ...fixture.dimensions, anchorNodeIds: [promptId, imageId, videoId] }, navigation, samples, distributions: groups, counts: fixture.counts, errors, pageErrorDetails,
      viewDiagnostics: { overviewBeforeTouch, beforeTouch, touchZoomSetups, dragOrigin, dragResets, dragTrajectory: "Measured +50/+35 CSS px mouse drag; outside timing real keyboard undo restores its sole position transaction every round. Forward and undo acknowledgements, exact original world position, all other nodes/configs/output selections and unchanged camera are asserted. Failed reset diagnostics are retained.", undoVisibility, finalView, graphBounds, screenshotError, touchSelectionSetup: process.env.LUMEN_SCALE_TOUCH_OVERVIEW === "1" ? "overview unchanged" : "real keyboard 0 resets zoom to 100% outside selection timing; actual selection uses touchscreen", touchTraceEnabled: process.env.LUMEN_SCALE_TRACE_TOUCH === "1", finalPhase: activePhases.get(page) },
      outcome: completed ? "passed" : "incomplete-or-failed",
      outcomeScope: "Assertion completion only. Playwright final runner status is authoritative, including timeout during artifact capture.",
      playwrightStatusAtFinally: info.status, expectedStatus: info.expectedStatus,
      methodology: "Browser performance.now from before Playwright action through assertion and two requestAnimationFrames. Includes driver/actionability/polling, not event-only CPU latency. Frame deltas and Chromium LongTask durations are raw observations. Layout includes autosave acknowledgement. No pass/fail speed threshold.",
      limits: "Deterministic intercepted API; mocked EventSource transport exercises real app merge subscribers. Hidden visibility property is simulated, not OS background throttling. WebKit is desktop-hosted mobile emulation. Repeated PNG bytes and local 1s MP4 do not model production bandwidth or media diversity." };
    await mkdir(path.dirname(reportPath), { recursive: true }); await writeFile(reportPath, JSON.stringify(report, null, 2));
    await info.attach("scale-raw-" + count, { path: reportPath, contentType: "application/json" });
  }
});
