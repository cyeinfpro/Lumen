import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64");
const gif = Buffer.from("R0lGODlhAQABAIAAAAAAAP///yH5BAAAAAAALAAAAAABAAEAAAIBRAA7", "base64");
async function setup(page: Page) {
  await installAgentFixture(page);
  const calls: { path: string; key: string; body: string }[] = [];
  let fail = false;
  await page.route("**/api/images/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST" && path.includes("/gif")) {
      calls.push({ path, key: route.request().headers()["idempotency-key"] ?? "", body: route.request().postData() ?? "" });
      await new Promise((resolve) => setTimeout(resolve, 180));
      if (fail && path.endsWith("/retime")) {
        fail = false;
        return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "此速度无法精确保留时序" }) });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        id: path.endsWith("/upload") ? "imported-gif" : "edited-gif-" + calls.length, width: 4, height: 4, mime: "image/gif",
      }) });
    }
    if (path === "/api/images/gif/history") return route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify([{ id: "imported-gif", width: 4, height: 4, mime: "image/gif" }]) });
    if (path.endsWith("/gif")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      width: 4, height: 4, frame_count: 3, durations_ms: [100, 200, 300], total_duration_ms: 600, loop: 0,
    }) });
    if (/\/gif\/frames\/\d+$/.test(path)) return route.fulfill({ status: 200, contentType: "image/png", body: png });
    if (path.endsWith("/binary")) return route.fulfill({ status: 200, contentType: "image/gif", body: gif });
    return route.fallback();
  });
  const historyReady = page.waitForResponse((response) => response.request().method() === "GET" &&
    new URL(response.url()).pathname === "/api/conversations/studio-conversation-1/messages" && response.ok(), { timeout: 220_000 });
  await page.goto("/?conversationId=studio-conversation-1", { waitUntil: "domcontentloaded" });
  await historyReady;
  if ((page.viewportSize()?.width ?? 0) >= 768) await page.getByRole("button", { name: "展开输入框" }).click();
  await page.getByRole("textbox", { name: "输入提示词" }).fill("GIF 本地编辑验收");
  await page.getByRole("tablist", { name: "模式", exact: true }).getByRole("tab", { name: "生图", exact: true }).click();
  await page.getByRole("button", { name: "执行设置", exact: true }).click();
  await page.getByLabel("导入 GIF 编辑", { exact: true }).setInputFiles({ name: "source.gif", mimeType: "image/gif", buffer: gif });
  const dialog = page.getByRole("dialog", { name: "GIF 本地编辑", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "选择帧" })).toBeVisible();
  return { calls, dialog, failNext: () => { fail = true; } };
}
test("GIF import, speed, selected-frame replacement and duplicate-click protection", async ({ page }, testInfo) => {
  const { calls, dialog } = await setup(page);
  await dialog.getByRole("combobox", { name: "播放倍速" }).selectOption("2");
  await dialog.getByRole("button", { name: "保存调速", exact: true }).dblclick();
  await expect(dialog.getByText("已保存新版本，原图未修改。")).toBeVisible();
  expect(calls.filter((call) => call.path.endsWith("/retime"))).toHaveLength(1);
  expect(calls[1].body).toBe('{"speed":2}');
  expect(calls.every((call) => call.key.length >= 8)).toBe(true);
  await dialog.getByRole("button", { name: "继续编辑", exact: true }).click();
  await dialog.getByRole("combobox", { name: "选择帧" }).selectOption("1");
  await expect(dialog.getByRole("link", { name: "导出 PNG" })).toBeVisible();
  const invalid = Buffer.from(png); invalid.writeUInt32BE(5, 16);
  await dialog.getByLabel("导入替换 PNG", { exact: true }).setInputFiles({ name: "wrong-size.png", mimeType: "image/png", buffer: invalid });
  await expect(dialog.getByRole("alert")).toContainText("PNG 尺寸必须为 4 × 4");
  await expect(dialog.getByRole("button", { name: "保存替换", exact: true })).toBeDisabled();
  await dialog.getByLabel("导入替换 PNG", { exact: true }).setInputFiles({ name: "replacement.png", mimeType: "image/png", buffer: png });
  await expect(dialog.getByAltText("替换预览")).toBeVisible();
  await dialog.getByRole("combobox", { name: "选择帧" }).selectOption("2");
  await expect(dialog.getByRole("button", { name: "保存替换", exact: true })).toBeDisabled();
  await dialog.getByRole("combobox", { name: "选择帧" }).selectOption("1");
  await dialog.getByLabel("导入替换 PNG", { exact: true }).setInputFiles({ name: "replacement.png", mimeType: "image/png", buffer: png });
  await testInfo.attach("gif-frame-editor", { body: await page.screenshot({ path: testInfo.outputPath("gif-frame-editor.png") }), contentType: "image/png" });
  await dialog.getByRole("button", { name: "保存替换", exact: true }).dblclick();
  await expect(dialog.getByRole("img", { name: "新 GIF 版本预览" })).toBeVisible();
  expect(calls.filter((call) => call.path.endsWith("/frames/1"))).toHaveLength(1);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(dialog).toHaveCount(0);
});
test("GIF errors, close/reopen and navigation do not submit unintended work", async ({ page }) => {
  const { calls, dialog, failNext } = await setup(page);
  failNext();
  await dialog.getByRole("combobox", { name: "播放倍速" }).selectOption("4");
  await dialog.getByRole("button", { name: "保存调速", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("此速度无法精确保留时序");
  await expect(dialog.getByRole("button", { name: "保存调速", exact: true })).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(calls).toHaveLength(2);
  const expand = page.getByRole("button", { name: "展开输入框", exact: true });
  if (await expand.isVisible()) await expand.click();
  const recent = page.getByRole("button", { name: "最近 GIF", exact: true });
  if (!(await recent.isVisible())) await page.getByRole("button", { name: "执行设置", exact: true }).click();
  await recent.click();
  await page.getByRole("button", { name: "GIF 1 · 4 × 4", exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "播放倍速" })).toHaveValue("1");
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await expect(dialog).toHaveCount(0);
  expect(calls.filter((call) => call.path.endsWith("/retime"))).toHaveLength(1);
});

async function verifyGifLightbox(page: Page, testInfo: TestInfo, landscape = false) {
  const { dialog, calls } = await setup(page);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.keyboard.press("Escape");
  if (landscape) await page.setViewportSize({ width: 700, height: 320 });
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("lumen:open-lightbox", { detail: {
    items: [{ id: "existing-gif", mime: "image/gif", url: "/api/images/existing-gif/binary", prompt: "GIF fixture" }],
    initialId: "existing-gif",
  } })));
  await page.getByRole("button", { name: "更多操作", exact: true }).last().click();
  const menu = page.getByRole("menu", { name: "更多操作", exact: true });
  await expect(menu).toBeVisible();
  const bounds = await menu.boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  if (landscape) {
    expect(await menu.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await menu.hover();
    await page.mouse.wheel(0, 500);
    await expect(menu.getByRole("menuitem").last()).toBeInViewport();
    await menu.getByRole("menuitem", { name: "编辑 GIF", exact: true }).scrollIntoViewIfNeeded();
  }
  await testInfo.attach("gif-lightbox-menu", { body: await page.screenshot({ path: testInfo.outputPath("gif-lightbox-menu.png") }), contentType: "image/png" });
  await page.getByRole("menuitem", { name: "编辑 GIF", exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox", { name: "选择帧" })).toHaveCount(1);
  expect(calls).toHaveLength(1);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

test("Existing GIF lightbox exposes local editing without calling a model", async ({ page }, testInfo) => {
  await verifyGifLightbox(page, testInfo);
});

test("Landscape GIF lightbox menu fits 700 by 320 viewport", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-system-chrome", "Mobile landscape coverage");
  await verifyGifLightbox(page, testInfo, true);
});
