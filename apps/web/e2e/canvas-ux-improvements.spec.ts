import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64");
const NOW = "2026-10-10T08:00:00Z";
function node(id: string, type: string, x: number, y: number, config = {}) {
  return { id, type, title: id, schema_version: 1, position: { x, y }, size: { width: 260, height: 220 }, config, ui: {} };
}
async function fixture(page: Page, media = false) {
  await installAgentFixture(page, { canvasEnabled: true });
  await page.route("**/api/videos/options", (route) => route.fulfill({ json: {
    enabled: true, models: [{ model: "fixture-video", actions: ["t2v"], resolutions: ["720p"], durations_s: [5] }],
    durations_s: [5], resolutions: ["720p"], aspect_ratios: ["16:9"], generate_audio: true, pricing: [], hold_estimates: {},
  } }));
  const graph = {
    schema_version: 1, nodes: media ? [
      node("poster-video", "video_text_generate", 0, 0),
      node("no-poster-video", "video_text_generate", 290, 0),
      node("failed-image", "image_generate", 580, 0),
      node("processing-image", "image_generate", 0, 280),
    ] : [node("prompt-1", "prompt", 0, 0, { text: "Original draft", locked: false }), node("empty-image", "image_generate", 300, 0)],
    edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 },
  };
  const outputs = [
    { type: "video", url: "/api/videos/poster-video/binary", poster_url: "/api/ux-media/poster.png", width: 640, height: 360 },
    { type: "video", url: "/api/videos/no-poster-video/binary", width: 640, height: 360 },
    { type: "image", url: "/api/ux-media/retry.png", preview_url: "/api/ux-media/retry.png", width: 320, height: 180 },
  ];
  const executions = media ? graph.nodes.map((n, index) => ({
    id: "execution-" + index, node_id: n.id, node_type: n.type, status: index === 3 ? "running" : "succeeded",
    outputs: outputs[index] ? [outputs[index]] : [], created_at: NOW, tasks: [],
  })) : [];
  let revision = 4, previewsFail = true, mediaRequests = 0, runRequests = 0;
  await page.route("**/api/canvases/canvas-ux**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/runs")) {
      runRequests += 1;
      return route.fulfill({ status: 400, json: { detail: "Fixture prohibits task execution" } });
    }
    if (path.endsWith("/mutations")) {
      for (const operation of route.request().postDataJSON().operations ?? []) {
        const current = graph.nodes.find((n) => n.id === operation.node_id);
        if (current && operation.op === "update_node_config") current.config = operation.config;
      }
      revision += 1;
      return route.fulfill({ json: { revision } });
    }
    return route.fulfill({ json: {
      id: "canvas-ux", title: "画布体验测试", description: "", revision, graph, created_at: NOW, updated_at: NOW,
      selections: executions.slice(0, 3).map((execution) => ({ node_id: execution.node_id, execution_id: execution.id, output_index: 0 })),
      recent_executions: executions, active_runs: [],
    } });
  });
  await page.route("**/api/ux-media/**", (route) => previewsFail && route.request().url().includes("retry.png")
    ? route.abort("connectionfailed") : route.fulfill({ contentType: "image/png", body: PNG }));
  await page.route("**/api/videos/*/binary", (route) => { mediaRequests += 1; return route.abort("connectionfailed"); });
  return { recover: () => { previewsFail = false; }, mediaRequests: () => mediaRequests, runRequests: () => runRequests };
}
const canvasNode = (page: Page, id: string) => page.locator('.react-flow__node[data-id="' + id + '"]');

test("real Canvas pairs both themes without losing draft, selection or viewport", async ({ page }, testInfo) => {
  await fixture(page);
  await page.goto("/projects/canvas/canvas-ux");
  const editor = page.getByRole("textbox", { name: "编辑提示词内容" });
  await expect(editor).toBeEditable();
  await editor.fill("Theme-switch draft stays intact");
  await editor.blur();
  await canvasNode(page, "prompt-1").locator("header").click({ position: { x: 20, y: 20 } });
  await expect(canvasNode(page, "prompt-1")).toHaveClass(/selected/);
  const viewport = page.locator(".react-flow__viewport");
  const zoomOut = page.getByRole("button", { name: "缩小画布", exact: true }).filter({ visible: true }).first();
  if (await zoomOut.count()) {
    const initial = await viewport.getAttribute("style");
    await zoomOut.click();
    await expect.poll(() => viewport.getAttribute("style")).not.toBe(initial);
  }
  await page.waitForTimeout(400);
  const transform = await viewport.getAttribute("style");
  await editor.evaluate((element) => element.setAttribute("data-theme-identity", "retained"));
  for (const theme of ["light", "dark", "light"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await page.locator("html").evaluate((html) => html.classList.remove("theme-light", "theme-dark", "dark"));
    const article = canvasNode(page, "empty-image").locator("article");
    await expect(article).toHaveCSS("background-color", theme === "light" ? "rgb(255, 255, 255)" : "rgb(13, 14, 17)");
    await expect(article).toHaveCSS("backdrop-filter", "none");
    await expect(editor).toHaveValue("Theme-switch draft stays intact");
    await expect(editor).toHaveAttribute("data-theme-identity", "retained");
    await expect(canvasNode(page, "prompt-1")).toHaveClass(/selected/);
    expect(await viewport.getAttribute("style")).toBe(transform);
    const contrast = await article.evaluate((element) => {
      const label = Array.from(element.querySelectorAll("span")).find((span) => span.textContent === "输入状态")!;
      function luminance(rgb: string) {
        const v = rgb.match(/[\d.]+/g)!.slice(0, 3).map(Number).map((n) => { const s = n / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
        return v[0]! * 0.2126 + v[1]! * 0.7152 + v[2]! * 0.0722;
      }
      const fg = luminance(getComputedStyle(label).color);
      const bg = luminance(getComputedStyle(label.parentElement!.parentElement!).backgroundColor);
      return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
    const screenshotPath = testInfo.outputPath("canvas-real-" + theme + ".png");
    await page.screenshot({ path: screenshotPath });
    await testInfo.attach("canvas-real-" + theme, { path: screenshotPath, contentType: "image/png" });
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("real Canvas only loads video after click and unmounts playback on close", async ({ page }, testInfo) => {
  const control = await fixture(page, true);
  await page.goto("/projects/canvas/canvas-ux");
  const poster = canvasNode(page, "poster-video"), absent = canvasNode(page, "no-poster-video");
  await expect(poster.locator("img")).toHaveAttribute("src", "/api/ux-media/poster.png");
  await expect(absent).toContainText("暂无海报，点击播放视频");
  await expect(page.locator("video")).toHaveCount(0);
  expect(control.mediaRequests()).toBe(0);
  await poster.getByRole("button", { name: "播放poster-video视频预览" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.locator("video")).toHaveCount(1);
  await expect(page.getByText("视频载入失败", { exact: true })).toBeVisible();
  expect(control.mediaRequests()).toBeGreaterThan(0);
  await page.getByRole("dialog").getByRole("button", { name: "关闭视频预览", exact: true }).click();
  await expect(page.locator("video")).toHaveCount(0);
  await absent.getByRole("button", { name: "播放no-poster-video视频预览" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.locator("video")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(control.runRequests()).toBe(0);
  await testInfo.attach("canvas-poster-only", { body: await page.screenshot(), contentType: "image/png" });
});

test("real Canvas distinguishes processing, absent poster and retryable failure", async ({ page }, testInfo) => {
  const control = await fixture(page, true);
  await page.goto("/projects/canvas/canvas-ux");
  const failed = canvasNode(page, "failed-image");
  await expect(failed).toContainText("预览暂时载入失败");
  await expect(failed.locator("[data-canvas-preview-state]")).toHaveAttribute("data-canvas-preview-state", "failed");
  await expect(canvasNode(page, "processing-image")).toContainText("处理中，结果就绪后显示");
  await expect(canvasNode(page, "no-poster-video")).toContainText("暂无海报，点击播放视频");
  await expect(failed).not.toContainText("已删除");
  await expect(failed).not.toContainText("无权限");
  control.recover();
  await failed.getByRole("button", { name: "重试预览", exact: true }).click();
  await expect(failed.locator("[data-canvas-preview-state]")).toHaveAttribute("data-canvas-preview-state", "ready");
  await expect(failed.getByRole("button", { name: "重试预览", exact: true })).toHaveCount(0);
  expect(control.runRequests()).toBe(0);
  expect(control.mediaRequests()).toBe(0);
  await testInfo.attach("canvas-preview-recovered", { body: await page.screenshot(), contentType: "image/png" });
});
