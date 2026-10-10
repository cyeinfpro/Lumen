import { expect, test, type Page } from "@playwright/test";
import { canvasNode, installScaleFixture } from "./canvas-scale-fixture";

async function visibleNodes(page: Page) {
  return page.locator(".react-flow").evaluate((flow) => {
    const viewport = flow.getBoundingClientRect();
    return [...flow.querySelectorAll(".react-flow__node")].filter((node) => {
      const box = node.getBoundingClientRect();
      return box.right > viewport.left && box.left < viewport.right
        && box.bottom > viewport.top && box.top < viewport.bottom;
    }).length;
  });
}
async function toolbarHistory(page: Page, name: "撤销" | "重做") {
  const desktop = page.getByRole("button", { name, exact: true });
  if (await desktop.isVisible()) { await desktop.click(); return; }
  await page.getByRole("button", { name: "更多画布操作", exact: true }).tap();
  await page.getByRole("menuitem", { name, exact: true }).tap();
}
const positions = (nodes: Array<{ id: string; position: { x: number; y: number } }>) =>
  nodes.map((node) => ({ id: node.id, ...node.position }));

test("layout undo and redo retain saved geometry without stranding the camera", async ({ page }, info) => {
  const fixture = await installScaleFixture(page, 100);
  await page.goto("/projects/canvas/canvas-scale");
  await expect(canvasNode(page, "n-0")).toBeAttached();
  const initial = positions(fixture.graph.nodes);
  await page.keyboard.press("ControlOrMeta+0");
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Shift+A");
  await expect.poll(() => positions(fixture.graph.nodes)).not.toEqual(initial);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  const laidOut = positions(fixture.graph.nodes);
  await expect.poll(() => visibleNodes(page)).toBeGreaterThan(0);
  await page.keyboard.press("ControlOrMeta+z");
  await expect.poll(() => positions(fixture.graph.nodes)).toEqual(initial);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  await expect.poll(() => visibleNodes(page)).toBeGreaterThan(0);
  await page.keyboard.press("ControlOrMeta+y");
  await expect.poll(() => positions(fixture.graph.nodes)).toEqual(laidOut);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  await expect.poll(() => visibleNodes(page)).toBeGreaterThan(0);
  await toolbarHistory(page, "撤销");
  await expect.poll(() => positions(fixture.graph.nodes)).toEqual(initial);
  await expect.poll(() => visibleNodes(page)).toBeGreaterThan(0);
  await toolbarHistory(page, "重做");
  await expect.poll(() => positions(fixture.graph.nodes)).toEqual(laidOut);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  await expect.poll(() => visibleNodes(page)).toBeGreaterThan(0);
  expect(fixture.counts.prohibited).toBe(0);
  await page.screenshot({ path: info.outputPath("history-geometry-visible.png") });
});

test("configuration undo preserves the existing camera and current graph inputs", async ({ page }) => {
  const fixture = await installScaleFixture(page, 100);
  await page.goto("/projects/canvas/canvas-scale");
  await expect(canvasNode(page, "n-0")).toBeAttached();
  // Use the full-size initial node, not a sub-pixel touch target at overview.
  const node = canvasNode(page, "n-0");
  const editor = node.getByRole("textbox", { name: "编辑提示词内容" });
  const original = await editor.inputValue();
  await editor.fill("A reversible draft only");
  await editor.blur();
  await expect.poll(() => fixture.graph.nodes[0]!.config.text).toBe("A reversible draft only");
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  const camera = await page.locator(".react-flow__viewport").getAttribute("style");
  await page.keyboard.press("ControlOrMeta+z");
  await expect.poll(() => fixture.graph.nodes[0]!.config.text).toBe(original);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存");
  await page.evaluate(() => new Promise<void>((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  expect(await page.locator(".react-flow__viewport").getAttribute("style")).toBe(camera);
  expect(fixture.counts.prohibited).toBe(0);
});
