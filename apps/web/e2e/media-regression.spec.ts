import { expect, test, type Page } from "@playwright/test";
import { resolve } from "node:path";
import { installAgentFixture } from "./agent-fixture";

const NOW = "2026-09-05T12:00:00Z";
const portrait = resolve("public/inspiration/editorial-fashion-portrait.webp");
const asset = {
  id: "media-generation", prompt: "Media portrait", created_at: NOW, aspect_ratio: "4:5", has_ref: false,
  size_actual: "512x640", message_id: "media-message", conversation_id: "media-conversation",
  image: {
    id: "media-image", url: "/api/images/media-image/binary", mime: "image/webp", width: 512, height: 640,
    thumb_url: "/api/images/media-image/variants/thumb256", preview_url: "/api/images/media-image/variants/preview1024",
    display_url: "/api/images/media-image/variants/display2048", variants: { thumb256: "ready", preview1024: "ready", display2048: "ready" },
  },
};

async function assetsFixture(page: Page) {
  await installAgentFixture(page);
  let failImages = false;
  let feedStatus = 200;
  await page.route("**/api/generations/feed?**", (route) => route.fulfill({
    status: feedStatus, contentType: "application/json",
    body: JSON.stringify(feedStatus === 200 ? { items: [asset], total: 1, next_cursor: null } : { detail: "media failure" }),
  }));
  await page.route("**/api/images/media-image/**", (route) => failImages
    ? route.fulfill({ status: 503, body: "Unavailable" })
    : route.fulfill({ status: 200, contentType: "image/webp", path: portrait }));
  return { failImages: (value: boolean) => { failImages = value; }, feedStatus: (value: number) => { feedStatus = value; } };
}

test("media assets expose independent preview, selection and touch menu surfaces", async ({ page }, testInfo) => {
  await assetsFixture(page);
  await page.goto("/stream");
  const tile = page.locator("article").filter({ has: page.getByRole("button", { name: "Media portrait", exact: true }) });
  await expect(tile.locator("img")).toBeVisible();
  expect(await tile.locator('button button, button a, [role="button"] button, [role="button"] input').count()).toBe(0);
  await page.getByRole("button", { name: "多选", exact: true }).click();
  const selection = tile.getByRole("checkbox", { name: "选择 Media portrait" });
  await selection.check();
  await expect(selection).toBeChecked();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await tile.getByRole("button", { name: "Media portrait", exact: true }).press("Enter");
  await expect(page.getByRole("dialog").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(selection).toBeChecked();
  const menu = tile.getByRole("button", { name: "作品菜单", exact: true });
  await menu.click();
  await expect(page.getByRole("button", { name: "做参考图", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "删除图片", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toBeFocused();
  await expect(tile.locator("img")).toHaveCSS("object-fit", "cover");
  await page.screenshot({ path: testInfo.outputPath("media-assets.png") });
});

test("media thumbnail failures are recoverable without changing tile geometry or opening preview", async ({ page }) => {
  const fixture = await assetsFixture(page);
  fixture.failImages(true);
  await page.goto("/stream");
  const tile = page.locator("article").filter({ has: page.getByRole("button", { name: "Media portrait", exact: true }) });
  const retry = tile.getByRole("button", { name: "重试", exact: true });
  await expect(retry).toBeVisible();
  const before = await tile.boundingBox();
  fixture.failImages(false);
  await retry.click();
  await expect(tile.locator("img")).toBeVisible();
  await expect.poll(() => tile.locator("img").evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  const after = await tile.boundingBox();
  expect(Math.abs((before?.height ?? 0) - (after?.height ?? 0))).toBeLessThan(2);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("media feed refresh failure retains loaded assets and never claims an empty library", async ({ page }) => {
  const fixture = await assetsFixture(page);
  await page.goto("/stream");
  await expect(page.getByRole("button", { name: "Media portrait", exact: true })).toBeVisible();
  fixture.feedStatus(503);
  await page.getByRole("button", { name: "刷新素材", exact: true }).click();
  await expect(page.getByRole("heading", { name: "记录加载失败" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Media portrait", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "暂无作品" })).toHaveCount(0);
  fixture.feedStatus(200);
  await page.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(page.getByRole("heading", { name: "记录加载失败" })).toHaveCount(0);
});

test("media forbidden feed is not a successful empty result", async ({ page }) => {
  const fixture = await assetsFixture(page);
  fixture.feedStatus(403);
  await page.goto("/stream");
  await expect(page.getByRole("heading", { name: "访问受限" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("heading", { name: "暂无作品" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重新加载" })).toHaveCount(0);
});

async function videoFixture(page: Page) {
  await installAgentFixture(page);
  let cancelRequestedAt: string | null = null;
  let cancelCalls = 0;
  const generation = () => ({
    id: "media-video", action: "t2v", model: "media-model", prompt: "Media video task", reference_media: [],
    duration_s: 5, resolution: "720p", aspect_ratio: "16:9", generate_audio: true,
    status: "running", progress_stage: "rendering", progress_pct: 0, submission_epoch: 1,
    cancel_requested_at: cancelRequestedAt, est_token_upper: 1, est_cost: { micro: 100, rmb: "0.0001" },
    video: null, created_at: NOW, updated_at: NOW,
  });
  await page.route("**/api/videos/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path.endsWith("/options")) body = {
      enabled: true, default_action: "t2v", default_model: "media-model", actions: ["t2v"],
      models: ["media-model", "media-other"].map((model) => ({ model, actions: ["t2v"], resolutions: ["720p"], aspect_ratios: ["16:9"], durations_s: [5], generate_audio: true })),
      resolutions: ["720p"], aspect_ratios: ["16:9"], durations_s: [5], generate_audio: true, pricing: [], hold_estimates: {},
    };
    else if (path.endsWith("/cancel")) {
      cancelCalls += 1;
      cancelRequestedAt = NOW;
      body = generation();
    } else if (path === "/api/videos/generations") body = { items: [generation()], next_cursor: null };
    else body = generation();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  return { cancelCalls: () => cancelCalls };
}

test("media video cancellation survives drawer remount and reload without invented percentages", async ({ page }, testInfo) => {
  const fixture = await videoFixture(page);
  await page.goto("/video");
  await page.getByRole("button", { name: "1 进行中", exact: true }).click();
  let drawer = page.getByRole("dialog", { name: "视频任务", exact: true });
  await expect(drawer.getByRole("status")).toHaveText("生成中");
  await expect(drawer.getByText(/\d+%/)).toHaveCount(0);
  await drawer.getByRole("button", { name: "取消", exact: true }).click();
  await expect(drawer.getByRole("status")).toHaveText("已请求取消");
  await expect(drawer.getByRole("button", { name: "已请求取消", exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath("media-video-cancel.png") });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "1 进行中", exact: true }).click();
  await expect(drawer.getByRole("status")).toHaveText("已请求取消");
  await page.reload();
  await page.getByRole("button", { name: "1 进行中", exact: true }).click();
  drawer = page.getByRole("dialog", { name: "视频任务", exact: true });
  await expect(drawer.getByRole("status")).toHaveText("已请求取消");
  expect(fixture.cancelCalls()).toBe(1);
});

test("media video uses one controlled responsive parameter panel and confirms model changes", async ({ page }, testInfo) => {
  await videoFixture(page);
  await page.goto("/video");
  const narrow = (page.viewportSize()?.width ?? 1440) < 1120;
  if (narrow) {
    await expect(page.getByRole("combobox", { name: "模型", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: /^参数/ }).first().click();
    await expect(page.getByRole("dialog", { name: "视频生成参数", exact: true })).toBeVisible();
  }
  const model = page.getByRole("combobox", { name: "模型", exact: true });
  await expect(model).toHaveCount(1);
  await expect(model).toHaveValue("media-model");
  await model.selectOption("media-other");
  const confirmation = page.getByRole("dialog", { name: "切换视频模型？", exact: true });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  await expect(model).toHaveValue("media-model");
  await model.selectOption("media-other");
  await confirmation.getByRole("button", { name: "确认切换", exact: true }).click();
  await expect(model).toHaveValue("media-other");
  await expect(confirmation).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("media-video-parameters.png") });
  if (narrow) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "视频生成参数", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: /^参数/ }).first().click();
    await expect(model).toHaveValue("media-other");
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
