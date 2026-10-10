import { expect, test, type Locator, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { installAgentFixture, openAgent } from "./agent-fixture";

const TITLE = "通知几何测试风格";
const ITEM = {
  id: "user:notification-fixture", source: "user_upload", visibility_scope: "user_private",
  title: TITLE, category: "minimal", mood: null, prompt_template: "Fixture prompt",
  palette: [], recommended_aspects: ["1:1"], style_tags: [],
  cover_image_url: "/api/images/notification-fixture/binary",
  display_url: null, thumb_url: null, cover_image_id: "notification-fixture",
  sample_image_ids: [], samples: [], auto_tagged_at: null, created_at: "2026-08-20T08:00:00Z",
};
const ERROR = "临时服务错误，草稿已保留，请稍后再试。".repeat(16);

async function fixture(page: Page, deferSync = false) {
  await installAgentFixture(page);
  let release: (() => void) | undefined;
  await page.route("**/api/poster-styles**", async (route) => {
    const path = decodeURIComponent(new URL(route.request().url()).pathname);
    let body: unknown;
    let status = 200;
    if (path.endsWith("/sync-presets")) {
      if (deferSync) await new Promise<void>((resolve) => { release = resolve; });
      body = { detail: { error: { code: "fixture_error", message: ERROR } } };
      status = 503;
    } else if (route.request().method() === "PATCH") {
      body = { detail: { error: { code: "fixture_error", message: ERROR } } };
      status = 503;
    } else if (path.endsWith("/jobs")) {
      body = { items: [], total: 0, has_more: false };
    } else if (path.endsWith("/poster-styles")) {
      body = { items: [ITEM], total: 1, limit: 200, offset: 0, has_more: false,
        sync: { can_sync: true, last_success_at: null, last_error: null } };
    } else {
      body = ITEM;
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
  return { ready: () => Boolean(release), release: () => release?.() };
}

async function expectHitTarget(button: Locator) {
  await expect(button).toBeVisible();
  await expect.poll(() => button.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return rect.width >= 43.5 && rect.height >= 43.5 && Boolean(hit && element.contains(hit)) &&
      !element.closest("[inert], [aria-hidden='true']");
  })).toBe(true);
}

async function expectAbove(notice: Locator, obstacle: Locator) {
  await expect.poll(async () => {
    const [toast, control] = await Promise.all([notice.boundingBox(), obstacle.boundingBox()]);
    if (!toast || !control) return false;
    const horizontalOverlap = toast.x < control.x + control.width && toast.x + toast.width > control.x;
    return !horizontalOverlap || toast.y + toast.height <= control.y - 8;
  }).toBe(true);
}

test("notifications: nested real dialogs own the same clickable toast and restore focus", async ({ page }, testInfo) => {
  await fixture(page);
  await page.goto("/poster-styles");
  const card = page.getByRole("button", { name: `查看 ${TITLE} 详情` });
  // Establish a real keyboard trigger; Safari intentionally does not focus buttons on tap.
  await card.focus();
  await card.press("Enter");
  const drawer = page.getByRole("dialog", { name: TITLE, exact: true });
  const edit = drawer.getByRole("button", { name: "编辑", exact: true });
  await edit.focus();
  await edit.press("Enter");
  const dialog = page.getByRole("dialog", { name: "编辑风格", exact: true });
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  const notice = page.locator("[data-lumen-toast-viewport]").getByRole("alert");
  await expect(notice).toContainText("更新失败");
  await expect(dialog.locator("[data-lumen-toast-host]")).toHaveCount(1);
  await page.locator("[data-lumen-toast-host]").evaluate((host) => { host.setAttribute("data-test-stable-host", "same"); });
  const close = notice.getByRole("button", { name: "关闭通知" });
  await expectHitTarget(close);
  await expectAbove(notice, dialog.locator("footer"));
  await dialog.locator("footer").evaluate((footer) => { footer.style.paddingTop = "64px"; });
  try {
    await expectAbove(notice, dialog.locator("footer"));
  } catch (error) {
    console.log("NOTIFICATION_GEOMETRY", await dialog.evaluate((root) =>
      Array.from(root.querySelectorAll("footer, [data-lumen-toast-host], [data-lumen-toast-viewport], [role=alert]")).map((el) => ({
        tag: el.tagName, className: el.className, style: el.getAttribute("style"),
        rect: el.getBoundingClientRect().toJSON(), inert: Boolean(el.closest("[inert], [aria-hidden=true]")),
      })),
    ));
    throw error;
  }
  const frames = await dialog.evaluate(async (root) => {
    const samples = [];
    for (let frame = 0; frame < 45; frame += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const footer = root.querySelector("footer")!;
      const toast = root.querySelector('[role="alert"]')!;
      const host = root.querySelector("[data-lumen-toast-host]")!;
      const viewport = root.querySelector("[data-lumen-toast-viewport]")!;
      const close = toast.querySelector('button[aria-label="关闭通知"]')!;
      const control = close.getBoundingClientRect();
      const hit = document.elementFromPoint(control.left + control.width / 2, control.top + control.height / 2);
      const describe = (element: Element) => ({
        rect: element.getBoundingClientRect().toJSON(),
        style: element.getAttribute("style"), transform: getComputedStyle(element).transform,
      });
      samples.push({
        frame, at: performance.now(), fonts: document.fonts.status,
        root: describe(root), footer: describe(footer), toast: describe(toast),
        host: describe(host), viewport: describe(viewport),
        gap: footer.getBoundingClientRect().top - toast.getBoundingClientRect().bottom,
        closeHit: Boolean(hit && close.contains(hit)),
      });
    }
    return samples;
  });
  await writeFile(testInfo.outputPath("notification-frame-geometry.json"), JSON.stringify(frames, null, 2));
  await page.screenshot({ path: testInfo.outputPath("notification-nested-footer.png") });
  expect(frames.filter((frame) => frame.gap < 8 || !frame.closeHit)).toEqual([]);
  const capturedGap = await dialog.evaluate((root) =>
    root.querySelector("footer")!.getBoundingClientRect().top -
      root.querySelector('[role="alert"]')!.getBoundingClientRect().bottom,
  );
  await writeFile(testInfo.outputPath("notification-captured-gap.json"), JSON.stringify({ capturedGap }));
  expect(capturedGap).toBeGreaterThanOrEqual(8);
  await close.focus();
  await page.keyboard.press("Tab");
  await expect.poll(() => dialog.evaluate((root) =>
    root.contains(document.activeElement) && !document.activeElement?.closest("[data-lumen-toast-host]"),
  )).toBe(true);

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(edit).toBeFocused();
  await expect(drawer.locator('[data-test-stable-host="same"]')).toHaveCount(1);
  await expectAbove(notice, drawer.locator("footer"));
  await expectHitTarget(close);
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(card).toBeFocused();
  await expect(page.locator('body > [data-test-stable-host="same"]')).toHaveCount(1);
  await expectHitTarget(close);
  await close.click();
  await expect(notice).toHaveCount(0);
});

test("notifications: real Agent composer growth and a reduced viewport remain unobstructed", async ({ page }, testInfo) => {
  await fixture(page);
  // Warm the real destination before creating the timed notification.
  await openAgent(page);
  await page.goto("/poster-styles");
  await page.getByRole("button", { name: "同步", exact: true }).filter({ visible: true }).click();
  // Finish the real request before navigating: WebKit may abort a pending fetch
  // when its initiating page is left, which is outside placement behavior.
  const notice = page.locator("[data-lumen-toast-viewport]").getByRole("alert");
  await expect(notice).toContainText("同步预设失败");
  await notice.getByRole("button", { name: "关闭通知" }).focus();
  await expect.poll(async () => {
    await page.evaluate(() => window.dispatchEvent(new CustomEvent("lumen:command-palette-open")));
    return page.getByRole("dialog", { name: "命令面板" }).count();
  }).toBe(1);
  const command = page.getByRole("combobox", { name: "搜索命令或页面" });
  await command.fill("Agent");
  await command.press("Enter");
  await expect(page).toHaveURL(/\/agent(?:\?|$)/u);
  const composer = page.getByTestId("agent-composer");
  await expect(composer).toBeVisible();
  await expect(notice).toContainText("同步预设失败");
  const close = notice.getByRole("button", { name: "关闭通知" });
  await expectAbove(notice, composer);
  const input = composer.locator("textarea").first();
  const initialHeight = (await composer.boundingBox())?.height ?? 0;
  await input.fill(Array.from({ length: 7 }, (_, index) => `第 ${index + 1} 行保留的草稿内容`).join("\n"));
  await expect.poll(async () => (await composer.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(initialHeight);
  await expectAbove(notice, composer);
  await page.setViewportSize({ width: 375, height: 480 });
  await expectAbove(notice, composer);
  await expectHitTarget(close);
  await expect(input).toHaveValue(/第 7 行保留的草稿内容/u);
  await page.screenshot({ path: testInfo.outputPath("notification-composer-375x480.png") });
  await close.click();
  await expect(notice).toHaveCount(0);
});
