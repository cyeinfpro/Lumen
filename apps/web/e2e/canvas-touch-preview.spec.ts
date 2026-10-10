import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { canvasNode, installScaleFixture } from "./canvas-scale-fixture";

const settle = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
const lightbox = (page: Page) => page.getByRole("dialog", { name: /^图片预览：|^图片查看器$/ });
async function openOverview(page: Page, count: number) {
  const fixture = await installScaleFixture(page, count);
  await page.goto("/projects/canvas/canvas-scale");
  await expect(canvasNode(page, "n-0")).toBeAttached();
  await expect(page.locator(".react-flow__edge").first()).toBeAttached();
  await settle(page);
  await page.getByRole("button", { name: "适应视图", exact: true }).filter({ visible: true }).tap();
  const node = canvasNode(page, "n-" + (fixture.anchor + 1));
  const grip = node.locator("header > svg").first();
  await expect(grip).toBeVisible();
  await expect.poll(async () => (await grip.boundingBox())?.width ?? Infinity).toBeLessThan(3);
  await settle(page);
  return { fixture, node, grip };
}

for (const count of [100, 500, 1000]) test("overview touch selects actual header without retargeted preview at " + count, async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { fixture, node, grip } = await openOverview(page, count);
  const bounds = await grip.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.width).toBeLessThan(3);
  await grip.tap();
  await expect(node).toHaveClass(/selected/);
  await expect(lightbox(page)).toHaveCount(0);
  expect(fixture.counts.prohibited).toBe(0);
  expect(errors).toEqual([]);
  const root = path.resolve(process.env.LUMEN_TOUCH_RESULTS || "../../tmp/lumen-core-ui-joint/scale-touch-regression");
  await mkdir(root, { recursive: true });
  await page.screenshot({ path: path.join(root, info.project.name + "-" + count + ".png") });
  await writeFile(path.join(root, info.project.name + "-" + count + ".json"), JSON.stringify({
    browser: page.context().browser()?.version(), viewport: page.viewportSize(), bounds,
    viewportTransform: await page.locator(".react-flow__viewport").getAttribute("style"),
    selectedId: "n-" + (fixture.anchor + 1), errors, counts: fixture.counts,
  }, null, 2));
});

test("direct picture touch, mouse and keyboard activation remain available", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { fixture, node, grip } = await openOverview(page, 100);
  // Real product zoom shortcut only prepares a separately tested direct-media interaction.
  await page.keyboard.press("0");
  await expect.poll(async () => (await grip.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(15);
  const preview = node.locator("[data-canvas-output-preview]");
  for (const kind of ["touch", "mouse", "Enter", "Space"] as const) {
    if (kind === "touch") await preview.tap();
    else if (kind === "mouse") await preview.click();
    else { await preview.focus(); await page.keyboard.press(kind); }
    await expect(lightbox(page)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(lightbox(page)).toHaveCount(0);
  }
  expect(errors).toEqual([]);
  expect(fixture.counts.prohibited).toBe(0);
});
