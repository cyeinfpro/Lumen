import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

const NOW = "2026-10-10T11:00:00Z";
const reason = "上次提交状态待确认，请先查询原任务";
async function fixture(page: Page) {
  await installAgentFixture(page, { canvasEnabled: true });
  let reads = 0, writes = 0;
  const task = { id: "task-readonly", kind: "generation", generation_id: "generation-owner",
    status: "expired", progress_stage: "finished", finished_at: NOW, error_code: "submit_unknown",
    recovery: { state: "expired", can_query: true, can_cancel: true, can_generate_new: true, automatic_resubmit: false } };
  const billing = { currency: "CNY", source: "task_ledger_aggregate", task_count: 2,
    estimated_cost_micro: null as number | null, reserved_micro: 0 as number | null,
    actual_cost_micro: null as number | null, known_estimated_cost_micro: 123456,
    known_actual_cost_micro: 10001 };
  const execution = { id: "execution-facts", node_id: "image", node_type: "image_generate",
    status: "failed", tasks: [task], outputs: [], billing, created_at: NOW, updated_at: NOW };
  const graph = { schema_version: 1, nodes: [
    { id: "prompt", type: "prompt", schema_version: 1, title: "提示词", position: { x: 0, y: 0 },
      config: { text: "A safe readonly preview" }, ui: {} },
    { id: "image", type: "image_generate", schema_version: 1, title: "生图", position: { x: 340, y: 0 },
      config: {}, ui: {} },
  ], edges: [{ id: "prompt-image", source_node_id: "prompt", source_handle: "text",
    target_node_id: "image", target_handle: "prompt", data_type: "text", binding_mode: "follow_active" }],
  frames: [], settings: { snap_to_grid: false, grid_size: 16 } };
  await page.route("**/api/canvases/canvas-facts**", async (route) => {
    if (route.request().method() !== "GET") {
      writes += 1; return route.fulfill({ status: 400, json: { detail: "Only reads are permitted in this fixture" } });
    }
    reads += 1;
    return route.fulfill({ json: { id: "canvas-facts", title: "恢复与账本", revision: 1, graph,
      recent_executions: [execution], active_runs: [], selections: [], created_at: NOW, updated_at: NOW } });
  });
  return { task, billing, execution, counts: () => ({ reads, writes }) };
}
const node = (page: Page) => page.locator('.react-flow__node[data-id="image"]');
const mobile = (page: Page) => (page.viewportSize()?.width ?? 1440) < 768;
async function select(page: Page) {
  // This suite tests recovery facts after initial layout, not clicks racing the
  // initial two-frame fit. Wait for the real viewport to leave its identity
  // transform, then let Playwright wait for the actual grip to settle.
  await expect(node(page)).toBeVisible();
  await expect.poll(() => page.locator(".react-flow__viewport").evaluate((element) => {
    const matrix = new DOMMatrixReadOnly(getComputedStyle(element).transform);
    return matrix.a !== 1 || matrix.d !== 1 || matrix.e !== 0 || matrix.f !== 0;
  })).toBe(true);
  await node(page).locator("header .lucide-grip-vertical").click();
  await expect(node(page)).toHaveClass(/selected/);
}
async function openInspector(page: Page) {
  await select(page);
  if (mobile(page)) await page.getByRole("button", { name: "打开检查器", exact: true }).click();
  const facts = page.getByRole("region", { name: "任务恢复与费用" });
  await expect(facts).toHaveCount(1);
  await facts.scrollIntoViewIfNeeded();
  return facts;
}
async function assertCommandDisabled(page: Page) {
  if (mobile(page)) {
    await page.getByRole("button", { name: "更多画布操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "命令菜单", exact: true }).click();
  } else await page.getByRole("button", { name: "打开命令菜单", exact: true }).click();
  const command = page.getByRole("option", { name: /运行当前节点/ });
  await expect(command).toBeDisabled(); await expect(command).toContainText(reason);
  await page.keyboard.press("Escape");
}

test("expired unknown submission blocks all run entries and queries the original without writes", async ({ page }, info) => {
  const control = await fixture(page);
  await page.goto("/projects/canvas/canvas-facts");
  await expect(node(page).getByRole("button", { name: "节点不可运行：" + reason })).toBeDisabled();
  await expect(node(page)).toContainText("提交状态待确认，请先查询原任务");
  await expect(node(page)).not.toContainText("节点运行失败");
  await select(page); await assertCommandDisabled(page);
  const facts = await openInspector(page);
  await expect(facts.locator('[data-canvas-recovery-state="submission_unknown"]')).toContainText("避免重复提交和扣费");
  const inspector = mobile(page) ? page.getByRole("dialog", { name: "节点检查器" }) : page.locator("body");
  const run = inspector.getByRole("button", { name: "运行节点", exact: true }).last();
  await expect(run).toBeDisabled();
  await expect(run.locator(".animate-spin")).toHaveCount(0);
  await expect(inspector.getByRole("progressbar")).toHaveCount(0);
  const baseline = control.counts().reads;
  await facts.getByRole("button", { name: "查询原任务状态", exact: true }).click();
  await expect.poll(() => control.counts().reads).toBeGreaterThan(baseline);
  await expect(facts.getByRole("button", { name: "查询原任务状态", exact: true })).toBeEnabled();
  expect(control.counts().writes).toBe(0);
  await page.screenshot({ path: info.outputPath("unknown-query-only.png") });
});

test("billing preserves missing values, zero and explicitly partial subtotals on the real inspector", async ({ page }, info) => {
  const control = await fixture(page);
  await page.goto("/projects/canvas/canvas-facts");
  const facts = await openInspector(page);
  await facts.locator("summary").click();
  await expect(facts).toContainText("费用记录 · 2 个任务");
  await expect(facts).toContainText("已知小计 ¥0.123456（尚未完整）");
  await expect(facts).toContainText("已知小计 ¥0.010001（尚未完整）");
  const reserve = facts.locator("dl > div").filter({ hasText: "当前预留" });
  await expect(reserve).toContainText("¥0");
  control.billing.known_actual_cost_micro = 0;
  await facts.getByRole("button", { name: "查询原任务状态", exact: true }).click();
  await expect(facts.locator("dl > div").filter({ hasText: "实际结算" })).toContainText("待确认");
  await expect(reserve).toContainText("¥0");
  expect(control.counts().writes).toBe(0);
  await facts.locator("dl").scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("billing-unknown-zero.png") });
});

test("query changes saving and cancel states without presenting a retry or new-generation action", async ({ page }) => {
  const control = await fixture(page);
  control.task.status = "running"; control.task.progress_stage = "saving_artifact"; control.task.error_code = "";
  control.task.recovery = { state: "saving_artifact", can_query: true, can_cancel: false, can_generate_new: false, automatic_resubmit: false };
  control.execution.status = "running";
  await page.goto("/projects/canvas/canvas-facts");
  const facts = await openInspector(page);
  await expect(facts.locator('[data-canvas-recovery-state="saving_artifact"]')).toContainText("查询状态不会重新生成");
  control.task.recovery.state = "cancel_requested";
  await facts.getByRole("button", { name: "查询原任务状态", exact: true }).click();
  await expect(facts.locator('[data-canvas-recovery-state="cancel_requested"]')).toContainText("最终状态和费用以任务记录为准");
  await expect(facts.getByRole("button")).toHaveCount(1);
  expect(control.counts().writes).toBe(0);
});
