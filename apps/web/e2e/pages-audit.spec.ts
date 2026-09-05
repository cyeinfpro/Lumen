import { expect, test, type Page, type Route } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

const NOW = "2026-09-05T04:00:00Z";
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function installProjectPages(page: Page) {
  await installAgentFixture(page);
  await page.route("**/api/workflows?**", (route) => json(route, { items: [{
    id: "pages-project", title: "秋季商品项目", type: "apparel_model_showcase", status: "running",
    user_prompt: "秋季商品展示", product_image_ids: [], current_step: "showcase_generation",
    metadata_jsonb: {}, created_at: NOW, updated_at: NOW, output_count: 2,
    completion_percent: 50, next_action: "查看生成任务",
  }], next_cursor: null }));
  await page.route("**/api/workflows/apparel-model-library?**", (route) => json(route, { items: [] }));
}

test("pages: project actions are separate from the row link and failed deletion stays local", async ({ page }, testInfo) => {
  await installProjectPages(page);
  let deletions = 0;
  let release: (() => void) | undefined;
  await page.route("**/api/workflows/pages-project", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deletions += 1;
    await new Promise<void>((resolve) => { release = resolve; });
    return json(route, { detail: { error: { code: "temporary_failure", message: "删除失败" } } }, 503);
  });
  await page.goto("/projects/apparel-model-showcase");
  const list = page.getByRole("list", { name: "项目列表" });
  await expect(list.getByRole("link", { name: "秋季商品项目", exact: true })).toBeVisible();
  const actions = list.getByRole("button", { name: "秋季商品项目的项目操作" });
  expect(await actions.evaluate((node) => node.closest("a") === null)).toBe(true);
  await actions.click();
  await page.getByRole("button", { name: "删除", exact: true }).or(page.getByRole("menuitem", { name: "删除", exact: true })).click();
  const dialog = page.getByRole("dialog", { name: "删除“秋季商品项目”？" });
  await expect(dialog).toContainText("关联对话和生成图片将被移除");
  await dialog.getByRole("button", { name: "删除", exact: true }).click();
  await expect.poll(() => deletions).toBe(1);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  release?.();
  await expect(dialog.getByRole("alert")).toContainText("删除结果未确认");
  await testInfo.attach("project-delete-error", { body: await page.screenshot(), contentType: "image/png" });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(list.getByRole("link", { name: "秋季商品项目", exact: true })).toBeVisible();
  expect(deletions).toBe(1);
  await testInfo.attach("project-list", { body: await page.screenshot(), contentType: "image/png" });
});

test("pages: API-key save errors retain dirty input and cancelled navigation keeps the draft", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/me", (route) => json(route, {
    id: "user-1", email: "user-1@example.com", role: "member", account_mode: "byok",
    runtime_defaults: { agent_enabled: true, canvas_enabled: false, nav_visibility: { studio: true, agent: true, video: true, projects: true, assets: true } },
  }));
  await page.route("**/api/me/api-credentials", (route) => json(route, { items: [] }));
  await page.route("**/api/me/api-credentials/suppliers", (route) => json(route, { items: [{ id: "pages-supplier", name: "Pages Supplier", validation_model: "test-model" }] }));
  await page.route("**/api/me/api-credentials/pages-supplier", (route) => json(route, { detail: { error: { code: "invalid_api_key", message: "Key 被拒绝" } } }, 400));
  await page.goto("/settings/api-key");
  const input = page.getByLabel("API 密钥", { exact: true });
  await expect(input).toHaveValue("");
  await input.fill("pages-test-not-a-real-secret");
  await page.getByRole("button", { name: "验证并保存" }).click();
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(input).toHaveValue("pages-test-not-a-real-secret");
  await expect(page.locator("#api-key-value-err")).toContainText("Key 被拒绝");
  await testInfo.attach("settings-field-error", { body: await page.screenshot(), contentType: "image/png" });
  await page.getByRole("link", { name: "用量", exact: true }).filter({ visible: true }).click();
  const dialog = page.getByRole("dialog", { name: "放弃未保存的设置？" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑" }).click();
  await expect(page).toHaveURL(/\/settings\/api-key$/u);
  await expect(input).toHaveValue("pages-test-not-a-real-secret");
});

test("pages: unavailable billing data is never announced as an empty ledger", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/me/usage?**", (route) => json(route, {
    range_start: NOW, range_end: NOW, messages_count: 0, generations_count: 0,
    generations_succeeded: 0, completions_count: 0, completions_succeeded: 0,
    total_pixels_generated: 0, total_tokens_in: 0, total_tokens_out: 0, storage_bytes: 0,
  }));
  await page.route("**/api/me/billing/snapshot", (route) => json(route, { detail: "unavailable" }, 503));
  await page.route("**/api/me/wallet/transactions?**", (route) => json(route, { detail: "unavailable" }, 503));
  await page.goto("/settings/usage");
  await expect(page.getByText("结算流水加载失败", { exact: true })).toBeVisible();
  await expect(page.getByText("费用构成加载失败", { exact: true })).toBeVisible();
  await expect(page.getByText("暂无近期扣费流水。", { exact: true })).toHaveCount(0);
  await expect(page.getByText("预留金额", { exact: true })).toBeVisible();
  await testInfo.attach("billing-unavailable", { body: await page.screenshot(), contentType: "image/png" });
});
