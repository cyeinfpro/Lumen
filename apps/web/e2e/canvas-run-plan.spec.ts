import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

const NOW = "2026-10-10T13:00:00Z";
async function fixture(page: Page) {
  await installAgentFixture(page, { canvasEnabled: true });
  const graph = { schema_version: 1, nodes: [
    { id: "p", type: "prompt", title: "提示", position: { x: 0, y: 0 }, config: { text: "local stub" }, ui: {} },
    { id: "a", type: "image_generate", title: "生成图片", position: { x: 320, y: 0 }, config: { count: 2 }, ui: {} },
    { id: "b", type: "image_edit", title: "编辑图片", position: { x: 640, y: 0 }, config: {}, ui: {} },
  ], edges: [
    { id: "pa", source_node_id: "p", source_handle: "text", target_node_id: "a", target_handle: "prompt", data_type: "text", binding_mode: "follow_active" },
    { id: "pb", source_node_id: "p", source_handle: "text", target_node_id: "b", target_handle: "prompt", data_type: "text", binding_mode: "follow_active" },
    { id: "ab", source_node_id: "a", source_handle: "image", target_node_id: "b", target_handle: "source", data_type: "image", binding_mode: "follow_active" },
  ], frames: [], settings: { snap_to_grid: false, grid_size: 16 } };
  type Body = Record<string, unknown>;
  const posts: Body[] = [], receipts = new Map<string, Body>(), previews: Body[] = [], repairs: Body[] = [];
  const state = { hash: "a".repeat(64), lost: false, offline: false, incomplete: false, run: null as Body | null };
  await page.route("**/api/canvases/canvas-plan**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path.endsWith("/plans/preview")) {
      const body = request.postDataJSON() as Body; previews.push(body);
      if ((body.output_indices as Record<string, number>).a === undefined) return route.fulfill({ status: 422, json: { detail: { error: { code: "canvas_plan_output_required", message: "请选择生成候选" } } } });
      return route.fulfill({ json: { plan: { schema_version: 1, canvas_id: "canvas-plan", document_revision: body.document_revision, kind: body.kind,
        target_node_ids: ["a", "b"], graph_hash: "b".repeat(64), plan_hash: state.hash, budget_micro: body.budget_micro, failure_policy: body.failure_policy,
        bindings: [], steps: ["a", "b"].map(node_id => ({ node_id, dependencies: node_id === "b" ? ["a"] : [], reuse: null, estimated_cost_micro: 100,
          effective_model: "local-stub", capability_version: state.hash, output_index: node_id === "a" ? 1 : 0 })) }, estimated_cost_micro: 200, budget_semantics: "admission_estimate_not_settlement_cap" } });
    }
    if (path.endsWith("/plans/run")) {
      const body = request.postDataJSON() as Body; posts.push(body);
      expect(request.headers()["idempotency-key"]).toBe(body.idempotency_key);
      if (state.offline) return route.abort("failed");
      state.run = { id: "run-plan", canvas_id: "canvas-plan", kind: "all", status: "queued", target_node_ids: ["a", "b"], executions: [], summary: {}, created_at: NOW };
      receipts.set(String(body.idempotency_key), state.run);
      if (state.lost) return route.abort("failed");
      return route.fulfill({ json: state.run });
    }
    if (path.includes("/plans/intents/")) {
      const key = decodeURIComponent(path.split("/").at(-1)!);
      return route.fulfill({ json: { admitted: receipts.has(key), run: receipts.get(key) ?? null } });
    }
    if (path.endsWith("/retry-failed")) {
      const body = request.postDataJSON() as Body; repairs.push(body);
      if (state.incomplete) return route.fulfill({ status: 409, json: { detail: { error: { code: "canvas_plan_repair_incomplete", message: "fail-fast 修复不完整", details: { missing_node_ids: ["failed-other"] } } } } });
      receipts.set(String(body.idempotency_key), state.run!);
      return route.fulfill({ json: state.run });
    }
    if (path.endsWith("/runs")) return route.fulfill({ json: { items: state.run ? [state.run] : [] } });
    if (path.includes("/runs/")) return route.fulfill({ json: state.run });
    if (path.endsWith("/history")) return route.fulfill({ json: { items: [], next_cursor: null } });
    if (request.method() !== "GET") return route.fulfill({ status: 400, json: { detail: "Unexpected write" } });
    return route.fulfill({ json: { id: "canvas-plan", title: "计划验收", revision: 1, graph, recent_executions: [], active_runs: [], selections: [], created_at: NOW, updated_at: NOW } });
  });
  return { state, posts, previews, repairs, receipts };
}
async function open(page: Page) {
  await page.goto("/projects/canvas/canvas-plan");
  await page.getByRole("button", { name: "打开运行计划", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "画布运行计划", exact: true });
  await expect(dialog.getByRole("button", { name: "保存并预览计划" })).toBeEnabled();
  return dialog;
}
async function configure(page: Page) {
  const dialog = await open(page);
  await dialog.getByLabel("计划范围", { exact: true }).selectOption("all");
  await dialog.getByLabel("计划准入预算", { exact: true }).fill("500");
  await dialog.locator("summary").filter({ hasText: "输出选择与精确复用" }).click();
  await dialog.getByLabel("节点 a 输出候选", { exact: true }).selectOption("1");
  await dialog.getByRole("button", { name: "保存并预览计划" }).click();
  await expect(dialog.getByRole("button", { name: "确认运行此计划" })).toBeEnabled();
  return dialog;
}
test("explicit plan preview and double click create one immutable task intent", async ({ page }, info) => {
  const f = await fixture(page), dialog = await configure(page);
  await expect(dialog).toContainText("最终结算上限");
  await expect(dialog).toContainText("目标：a、b");
  await dialog.getByRole("button", { name: "确认运行此计划" }).evaluate(element => { (element as HTMLButtonElement).click(); (element as HTMLButtonElement).click(); });
  await expect(dialog.getByRole("region", { name: "计划运行与修复" })).toContainText("run-plan");
  expect(f.receipts.size).toBe(1); expect(new Set(f.posts.map(body => body.idempotency_key)).size).toBe(1);
  expect(f.posts[0]).toMatchObject({ budget_micro: 500, document_revision: 1, output_indices: { a: 1 }, reuse_outputs: {}, plan_hash: "a".repeat(64) });
  await page.screenshot({ path: info.outputPath("run-plan-admitted.png") });
});
test("budget edits invalidate preview and capability repricing blocks stale admission", async ({ page }) => {
  const f = await fixture(page), dialog = await configure(page);
  await dialog.getByLabel("计划准入预算", { exact: true }).fill("600");
  await expect(dialog.getByRole("button", { name: "确认运行此计划" })).toBeDisabled();
  await dialog.getByRole("button", { name: "保存并预览计划" }).click();
  await expect(dialog.getByRole("button", { name: "确认运行此计划" })).toBeEnabled();
  f.state.hash = "c".repeat(64);
  await dialog.getByRole("button", { name: "确认运行此计划" }).click();
  await expect(dialog.getByRole("alert")).toContainText("能力或价格已变化");
  expect(f.posts).toHaveLength(0);
});
test("lost acknowledgement queries original receipt without another paid intent", async ({ page }) => {
  const f = await fixture(page); f.state.lost = true;
  const dialog = await configure(page);
  await dialog.getByRole("button", { name: "确认运行此计划" }).click();
  await expect(dialog.getByRole("region", { name: "计划运行与修复" })).toContainText("run-plan");
  expect(f.receipts.size).toBe(1); expect(new Set(f.posts.map(body => body.idempotency_key)).size).toBe(1);
});
test("offline unknown survives reload, queries read only and explicitly replays exact key", async ({ page }) => {
  const f = await fixture(page); f.state.offline = true;
  let dialog = await configure(page);
  await dialog.getByRole("button", { name: "确认运行此计划" }).click();
  await expect(dialog.getByRole("button", { name: "查询原提交", exact: true })).toBeEnabled();
  const original = f.posts[0], baseline = f.posts.length;
  await page.reload();
  await page.getByRole("button", { name: "打开运行计划", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "画布运行计划", exact: true });
  await expect(dialog.getByRole("button", { name: "查询原提交", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "查询原提交", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "确认运行此计划" })).toBeDisabled();
  expect(f.posts.length).toBe(baseline);
  f.state.offline = false;
  await dialog.locator("summary").filter({ hasText: "原请求仍未被确认" }).click();
  await dialog.getByRole("button", { name: "重发原幂等请求", exact: true }).click();
  await expect(dialog.getByRole("region", { name: "计划运行与修复" })).toContainText("run-plan");
  expect(f.posts.at(-1)).toEqual(original); expect(f.receipts.size).toBe(1);
});
test("repair only latest confirmed failures and surface exact fail-fast missing nodes", async ({ page }) => {
  const f = await fixture(page);
  const task = { id: "task", kind: "generation", status: "failed", progress_stage: "failed",
    recovery: { state: "failed", can_query: true, can_generate_new: true, can_cancel: false, automatic_resubmit: false } };
  const failed = { id: "failed", node_id: "a", node_type: "image_generate", status: "failed", attempt: 0, outputs: [], tasks: [task] };
  f.state.run = { id: "repair-run", canvas_id: "canvas-plan", kind: "all", status: "failed", summary: {}, executions: [
    failed, { ...failed, id: "unknown", node_id: "b", tasks: [{ ...task, error_code: "submit_unknown" }] },
    { ...failed, id: "partial", node_id: "c", status: "partial_failed", outputs: [{ type: "image", image_id: "saved" }] },
  ] }; f.state.incomplete = true;
  const dialog = await open(page);
  await dialog.getByLabel("查看批量运行", { exact: true }).selectOption("repair-run");
  const repair = dialog.getByRole("region", { name: "计划运行与修复" });
  await expect(repair.getByRole("checkbox")).toHaveCount(1);
  await repair.getByRole("checkbox").check();
  await repair.getByLabel("修复额外预算", { exact: true }).fill("500");
  await repair.getByRole("button", { name: "修复所选失败步骤" }).click();
  await expect(dialog.getByRole("alert")).toContainText("failed-other");
  expect(f.repairs[0]).toMatchObject({ execution_ids: ["failed"], additional_budget_micro: 500 });
});
