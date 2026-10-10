import { expect, test, type Page } from "@playwright/test";
import { canvasNode, installScaleFixture } from "./canvas-scale-fixture";

const camera = (page: Page) => page.locator(".react-flow__viewport").getAttribute("style");
async function focusNode(page: Page, id: string) {
  const close = page.getByRole("button", { name: "关闭检查器", exact: true });
  if (await close.isVisible()) await close.click();
  await page.keyboard.press("Escape");
  await page.keyboard.press("ControlOrMeta+0");
  await page.evaluate(() => new Promise<void>((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const grip = canvasNode(page, id).locator("header > svg").first();
  await grip.click();
  await expect(canvasNode(page, id)).toHaveClass(/selected/);
  const fit = page.getByRole("button", { name: "适应选区", exact: true });
  if (await fit.isVisible()) await fit.click();
  else await page.keyboard.press("Shift+2");
  await grip.hover();
  return grip;
}

test("initial fit works and repeated real node drags keep the chosen camera", async ({ page }, info) => {
  const fixture = await installScaleFixture(page, 100);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/projects/canvas/canvas-scale");
  await expect(canvasNode(page, "n-0")).toBeAttached();
  await expect.poll(() => page.locator(".react-flow__viewport").evaluate((element) =>
    new DOMMatrixReadOnly(getComputedStyle(element).transform).a)).toBeLessThan(1);
  const id = "n-" + fixture.anchor;
  for (let round = 0; round < 2; round++) {
    const grip = await focusNode(page, id);
    const before = structuredClone(fixture.graph.nodes);
    const original = { ...fixture.graph.nodes[fixture.anchor]!.position };
    const transform = await camera(page);
    const mutation = fixture.counts.mutations;
    const box = (await grip.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 50, box.y + box.height / 2 + 35, { steps: 8 });
    await page.mouse.up();
    await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(mutation);
    await expect.poll(() => fixture.graph.nodes[fixture.anchor]!.position).not.toEqual(original);
    await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
    await grip.hover();
    expect(await camera(page)).toBe(transform);
    const undoMutation = fixture.counts.mutations;
    await page.keyboard.press("ControlOrMeta+z");
    await expect.poll(() => fixture.counts.mutations).toBeGreaterThan(undoMutation);
    await expect.poll(() => fixture.graph.nodes).toEqual(before);
    await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
    expect(await camera(page)).toBe(transform);
  }
  expect(errors).toEqual([]);
  expect(fixture.counts.prohibited).toBe(0);
  await page.screenshot({ path: info.outputPath("drag-camera-preserved.png") });
});
