import { expect, test, type Page, type Route, type TestInfo } from "@playwright/test";
import { installAgentFixture, openAgent } from "./agent-fixture";

const NOW = "2026-09-05T08:00:00Z";
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, json: body });
const prompt = (id: string, name: string) => ({ id, name, content: `${name}内容`, is_default: false, created_at: NOW, updated_at: NOW });

async function capture(page: Page, testInfo: TestInfo, name: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator("main").first()).toBeVisible();
  await testInfo.attach(name, { body: await page.screenshot(), contentType: "image/png" });
}

async function installPrompts(page: Page) {
  await installAgentFixture(page);
  const items = [prompt("prompt-a", "商品导演"), prompt("prompt-b", "文案编辑")];
  await page.route("**/api/system-prompts", (route) => json(route, { items, default_id: null }));
  await page.goto("/settings/prompts");
  return items;
}

async function cancelDeparture(page: Page) {
  const dialog = page.getByRole("dialog", { name: "放弃未保存的设置？" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

async function confirmDeparture(page: Page) {
  const dialog = page.getByRole("dialog", { name: "放弃未保存的设置？" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "放弃并离开", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

test("review: prompt labels and invalid fields have connected accessible errors", async ({ page }, testInfo) => {
  await installPrompts(page);
  const name = page.getByLabel("名称", { exact: true });
  const content = page.getByLabel("内容", { exact: true });
  await name.fill("");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  for (const [input, error] of [[name, "名称必填"], [content, "内容必填"]] as const) {
    await expect(input).toHaveAttribute("aria-invalid", "true");
    const errorId = await input.getAttribute("aria-describedby");
    expect(errorId).toBeTruthy();
    await expect(page.locator(`[id="${errorId}"]`)).toHaveText(error);
  }
  await capture(page, testInfo, "prompt-field-errors");
});

test("review: dirty prompt selection, creation and route departure preserve drafts until confirmed", async ({ page }, testInfo) => {
  await installPrompts(page);
  const content = page.getByLabel("内容", { exact: true });
  const first = page.getByRole("button", { name: /商品导演.*商品导演内容/u });
  const second = page.getByRole("button", { name: /文案编辑.*文案编辑内容/u });
  await first.click();
  await content.fill("保留修改");
  await second.click();
  await cancelDeparture(page);
  await expect(content).toHaveValue("保留修改");
  await second.click();
  await confirmDeparture(page);
  await expect(content).toHaveValue("文案编辑内容");
  await content.fill("新建前修改");
  await page.getByRole("button", { name: "新建提示词", exact: true }).click();
  await cancelDeparture(page);
  await expect(content).toHaveValue("新建前修改");
  await page.getByRole("button", { name: "新建提示词", exact: true }).click();
  await confirmDeparture(page);
  await expect(content).toHaveValue("");
  await content.fill("离开前修改");
  const usage = page.getByRole("link", { name: "用量", exact: true }).filter({ visible: true });
  await usage.click();
  await cancelDeparture(page);
  await expect(content).toHaveValue("离开前修改");
  const save = page.getByRole("button", { name: "保存", exact: true });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport({ ratio: 1 });
  const bounds = await save.boundingBox();
  expect(bounds?.height).toBeGreaterThanOrEqual(44);
  expect(await save.evaluate((button) => {
    const rect = button.getBoundingClientRect();
    return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await capture(page, testInfo, "prompt-retained-draft");
  await usage.click();
  await confirmDeparture(page);
  await expect(page).toHaveURL(/\/settings\/usage$/u);
});

test("review: prompt deletion names the target and retains a failed actionable confirmation", async ({ page }, testInfo) => {
  await installPrompts(page);
  let deletes = 0;
  await page.route("**/api/system-prompts/prompt-a", (route) => {
    deletes += 1;
    return json(route, { error: { code: "conflict", message: "删除暂不可用" } }, 409);
  });
  await page.getByRole("button", { name: /商品导演.*商品导演内容/u }).click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "删除“商品导演”？" });
  await expect(dialog).toContainText("解除账号及关联会话");
  expect(deletes).toBe(0);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(deletes).toBe(0);
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await dialog.getByRole("button", { name: "删除", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  expect(deletes).toBe(1);
  await expect(dialog.getByRole("button", { name: "删除", exact: true })).toBeEnabled();
  await capture(page, testInfo, "prompt-delete-error");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByLabel("名称", { exact: true })).toHaveValue("商品导演");
});

async function installAdmin(page: Page) {
  await installAgentFixture(page);
  await page.route("**/api/auth/me", (route) => json(route, { id: "user-a", email: "admin@example.com", role: "admin", account_mode: "wallet", runtime_defaults: { agent_enabled: true } }));
  await page.route("**/api/admin/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/admin/settings") return json(route, { items: [
      { key: "upstream.default_model", value: "initial-model", has_value: true, is_sensitive: false, description: "Default model" },
      { key: "telegram.bot_username", value: "initial_bot", has_value: true, is_sensitive: false, description: "Bot username" },
    ] });
    return json(route, { items: [], models: [], proxies: [] });
  });
  await page.goto("/admin");
}

async function adminTab(page: Page, key: string, label: string) {
  if ((page.viewportSize()?.width ?? 0) < 768) await page.getByLabel("管理后台页面").selectOption(key);
  else await page.getByRole("button", { name: label, exact: true }).click();
}

for (const kind of ["settings", "telegram"] as const) {
  test(`review: dirty admin ${kind} guards mobile select and desktop navigation`, async ({ page }, testInfo) => {
    await installAdmin(page);
    await adminTab(page, kind, kind === "settings" ? "系统设置" : "Telegram");
    const input = kind === "settings" ? page.getByLabel("默认对话模型", { exact: true }) : page.getByLabel(/机器人用户名/u);
    await input.fill("edited_value");
    const leave = async () => {
      if (kind === "settings" && (page.viewportSize()?.width ?? 0) < 768) {
        const select = page.getByLabel("管理后台页面");
        await select.focus();
        // Desktop Chromium's native popup does not commit arrow keys in headless mode.
        // Type-ahead still emits a real keyboard-driven change to the Telegram option.
        await select.press(testInfo.project.use.isMobile ? "ArrowUp" : "t");
      } else await adminTab(page, "health", "健康");
    };
    await leave();
    await cancelDeparture(page);
    await expect(input).toHaveValue("edited_value");
    await capture(page, testInfo, `admin-${kind}-dirty`);
    await leave();
    await confirmDeparture(page);
    await expect(input).toHaveCount(0);
  });
}

test("review: optional image preview render failure is recoverable without losing the composer draft", async ({ page }, testInfo) => {
  await installAgentFixture(page, { mode: "partial-image" });
  let intercepted = 0;
  await page.route("**/_next/static/chunks/*.js", async (route) => {
    const response = await route.fetch();
    const source = await response.text();
    const declaration = /function (?:DesktopLightbox|MobileLightbox)\(\) \{/gu;
    const modified = source.replace(declaration, (match) => {
      intercepted += 1;
      return `${match}\nif (globalThis.__lumenTestPreviewFailure) throw new Error('Fixture optional preview render failure');`;
    });
    await route.fulfill({ response, body: modified });
  });
  await openAgent(page);
  const draft = page.getByRole("textbox", { name: "发送给 Agent" });
  await draft.fill("预览失败前的草稿");
  await page.evaluate(() => { Object.assign(globalThis, { __lumenTestPreviewFailure: true }); });
  await page.getByRole("button", { name: "查看大图", exact: true }).click();
  await expect(page.getByRole("heading", { name: "图片预览不可用", exact: true })).toBeVisible();
  expect(intercepted).toBeGreaterThan(0);
  await expect(draft).toHaveValue("预览失败前的草稿");
  await expect(page.getByRole("button", { name: "刷新页面", exact: true })).toBeVisible();
  await capture(page, testInfo, "optional-preview-render-failure");
  await page.evaluate(() => { Object.assign(globalThis, { __lumenTestPreviewFailure: false }); });
  await page.getByRole("button", { name: "重试图片预览", exact: true }).click();
  await expect(page.getByRole("heading", { name: "图片预览不可用", exact: true })).toHaveCount(0);
  // Mobile preview owns its open event locally and must be reopened after remount.
  if ((page.viewportSize()?.width ?? 0) < 768) await page.getByRole("button", { name: "查看大图", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(draft).toHaveValue("预览失败前的草稿");
  await capture(page, testInfo, "optional-preview-recovered");
});

test("review: probing the saved credential never invalidates or clears the replacement field", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/me", (route) => json(route, { id: "user-a", email: "byok@example.com", role: "member", account_mode: "byok", runtime_defaults: {} }));
  let credential = { id: "credential-a", supplier_id: "supplier-a", supplier_name: "Saved supplier", status: "active", key_hint: "sk-...test", last_error_code: null, last_verified_at: null as string | null, last_failed_at: null, rate_limited_until: null };
  let replacements = 0;
  await page.route("**/api/me/api-credentials", (route) => json(route, { items: [credential] }));
  await page.route("**/api/me/api-credentials/supplier-a", (route) => {
    expect(route.request().method()).toBe("PUT");
    expect(route.request().postDataJSON()).toEqual({ api_key: "replacement-draft" });
    replacements += 1;
    credential = { ...credential, id: "credential-b", key_hint: "sk-...new", last_verified_at: NOW };
    return json(route, credential);
  });
  await page.route("**/api/me/api-credentials/suppliers", (route) => json(route, { items: [{ id: "supplier-a", name: "Saved supplier", validation_model: "test-model" }] }));
  await page.route("**/api/me/api-credentials/credential-a/probe", (route) => json(route, { error: { code: "invalid_api_key", message: "Key 被拒绝" } }, 400));
  await page.goto("/settings/api-key");
  const replacement = page.getByLabel("API 密钥", { exact: true });
  await page.getByRole("button", { name: "重新检测", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Key 被拒绝" })).toBeVisible();
  await expect(replacement).not.toHaveAttribute("aria-invalid", "true");
  await expect(replacement).toHaveValue("");
  await replacement.fill("replacement-draft");
  await expect(page.getByRole("alert").filter({ hasText: "Key 被拒绝" })).toBeVisible();
  await expect(replacement).not.toHaveAttribute("aria-invalid", "true");
  await capture(page, testInfo, "credential-probe-error");
  expect(replacements).toBe(0);
  await page.getByRole("button", { name: "验证并保存", exact: true }).click();
  await expect(replacement).toHaveValue("");
  await expect(page.getByText("Saved supplier · sk-...new", { exact: true })).toBeVisible();
  expect(replacements).toBe(1);
  await expect(page.getByRole("alert").filter({ hasText: "Key 被拒绝" })).toHaveCount(0);
  await expect(replacement).not.toHaveAttribute("aria-invalid", "true");
  await capture(page, testInfo, "credential-replacement-healthy");
});
