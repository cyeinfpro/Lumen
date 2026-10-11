import { expect, test } from "@playwright/test";
import { canvasNode, installScaleFixture } from "./canvas-scale-fixture";

for (const width of [320, 375]) test("candidate selection and download remain separate targets at " + width + "px", async ({ page }, info) => {
  await page.setViewportSize({ width, height: 700 });
  const errors: string[] = [], downloads: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("download", (download) => downloads.push(download.suggestedFilename()));
  const fixture = await installScaleFixture(page, 100);
  await page.goto("/projects/canvas/canvas-scale");
  const target = canvasNode(page, "n-" + (fixture.anchor + 1));
  await expect(target).toBeAttached();
  await expect.poll(() => page.locator(".react-flow__viewport").getAttribute("style")).toMatch(/scale\(0\./);
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await page.keyboard.press("0");
  const grip = target.locator("header > svg").first();
  await expect.poll(async () => (await grip.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(15);
  await grip.click();
  await expect(target).toHaveClass(/selected/);
  await page.getByRole("button", { name: "打开检查器", exact: true }).filter({ visible: true }).click();
  const choice = page.getByRole("button", { name: "选择第 2 个图片输出", exact: true }).filter({ visible: true });
  await expect(choice).toBeVisible();
  await choice.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("candidate-targets-" + width + ".png") });
  // Assert actual hit testing before the unforced user click, not CSS visibility alone.
  await expect.poll(() => choice.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return hit === element || (hit !== null && element.contains(hit));
  })).toBe(true);
  const download = page.locator("[data-canvas-output-card]").filter({ has: page.getByRole("button", { name: /第 2 个图片输出$/ }) }).getByRole("button", { name: "下载图片成品", exact: true });
  const choiceBox = await choice.boundingBox(), downloadBox = await download.boundingBox();
  expect(choiceBox).not.toBeNull(); expect(downloadBox).not.toBeNull();
  expect(downloadBox!.y).toBeGreaterThanOrEqual(choiceBox!.y + choiceBox!.height);
  if (info.project.use.hasTouch) await choice.tap();
  else await choice.click();
  await expect(page.getByRole("button", { name: "当前第 2 个图片输出", exact: true }).filter({ visible: true })).toHaveAttribute("aria-pressed", "true");
  expect(downloads).toEqual([]);
  const downloaded = page.waitForEvent("download");
  if (info.project.use.hasTouch) await download.tap();
  else await download.click();
  expect(await (await downloaded).failure()).toBeNull();
  expect(downloads).toHaveLength(1);
  await expect(page.getByRole("button", { name: "当前第 2 个图片输出", exact: true }).filter({ visible: true })).toHaveAttribute("aria-pressed", "true");
  expect(fixture.counts.prohibited).toBe(0);
  expect(errors).toEqual([]);
});
