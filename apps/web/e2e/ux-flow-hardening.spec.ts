import { expect, test, type Page, type Route, type TestInfo } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function recordControls(page: Page, testInfo: TestInfo) {
  const buttons = page.getByRole("button");
  for (const button of await buttons.all()) {
    if (await button.isVisible()) await expect(button).toHaveAccessibleName(/\S/u);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await testInfo.attach("current-visible-state", { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
  await testInfo.attach("rendered-control-inventory", {
    body: JSON.stringify(await page.locator("button, a, input, textarea, select").evaluateAll((nodes) => nodes.map((node) => ({
      tag: node.tagName, id: node.id, text: node.textContent?.trim().slice(0, 120), label: node.getAttribute("aria-label"),
      disabled: node.hasAttribute("disabled"), busy: node.getAttribute("aria-busy"),
    }))), null, 2), contentType: "application/json",
  });
}

test("ux: login validation focuses the field and password visibility preserves input", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.goto("/login");
  const email = page.locator("#login-email");
  const password = page.locator("#login-password");
  await expect(email).toBeVisible();
  await page.locator('form button[type="submit"]').click();
  await expect(email).toBeFocused();
  await expect(email).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#login-form-error")).toBeVisible();
  await email.fill("ux-fixture@example.com");
  await email.press("Enter");
  await expect(password).toBeFocused();
  await password.fill("fixture-password-123");
  const toggle = page.getByRole("button", { name: /显示密码/u });
  await toggle.click();
  await expect(password).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: /隐藏密码/u }).click();
  await expect(password).toHaveAttribute("type", "password");
  await expect(password).toHaveValue("fixture-password-123");
  await expect(page.locator("#login-form-error")).toHaveCount(0);
  await recordControls(page, testInfo);
});

test("ux: signup verification errors stay in the correct step and changing keys keeps account input", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/api-suppliers", (route) => json(route, { items: [{ id: "ux-supplier", name: "UX Fixture Supplier", validation_model: "fixture-model" }] }));
  let verified = false;
  let verificationCalls = 0;
  await page.route("**/api/auth/api-key/verify", (route) => {
    verificationCalls += 1;
    return verified
      ? json(route, { verification_token: "fixture-verification-token", key_hint: "fixture-key", expires_at: "2099-01-01T00:00:00Z" })
      : json(route, { detail: { error: { code: "invalid_api_key", message: "测试密钥被拒绝" } } }, 400);
  });
  await page.goto("/signup");
  const key = page.locator("#signup-api-key");
  await expect(page.locator("#signup-supplier")).toHaveValue("ux-supplier");
  await key.fill("not-a-real-key-fixture");
  await key.press("Enter");
  await expect(page.locator("#signup-verification-error")).toBeVisible();
  await expect(page.locator("#signup-account-error")).toHaveCount(0);
  await expect(key).toBeFocused();
  await expect(key).toHaveValue("not-a-real-key-fixture");
  verified = true;
  await key.press("Enter");
  await expect(page.locator("#signup-email")).toBeFocused();
  await expect(key).toHaveValue("");
  await page.locator("#signup-email").fill("ux-fixture@example.com");
  await page.locator("#signup-password").fill("fixture-password-123");
  await page.getByRole("button", { name: "更换 API 密钥" }).click();
  await expect(key).toBeEnabled();
  await expect(key).toBeFocused();
  await expect(page.locator("#signup-email")).toHaveValue("ux-fixture@example.com");
  await expect(page.locator("#signup-password")).toHaveValue("fixture-password-123");
  await expect(page.getByRole("form", { name: "创建账号" }).getByRole("button", { name: /创建账号/u })).toBeDisabled();
  expect(verificationCalls).toBe(2);
  await recordControls(page, testInfo);
});

test("ux: signup submit retains a readable busy label, prevents duplicates and preserves rejected input", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/api-suppliers", (route) => json(route, { items: [{ id: "ux-supplier", name: "UX Fixture Supplier", validation_model: "fixture-model" }] }));
  await page.route("**/api/auth/api-key/verify", (route) => json(route, { verification_token: "fixture-verification-token", key_hint: "fixture-key" }));
  let submissions = 0;
  let release: (() => void) | undefined;
  await page.route("**/api/auth/signup/byok", async (route) => {
    submissions += 1;
    await new Promise<void>((resolve) => { release = resolve; });
    return json(route, { detail: { error: { code: "email_already_exists", message: "测试账号已存在" } } }, 409);
  });
  await page.goto("/signup");
  await expect(page.locator("#signup-supplier")).toHaveValue("ux-supplier");
  await page.locator("#signup-api-key").fill("not-a-real-key-fixture");
  await page.locator("#signup-api-key").press("Enter");
  await expect(page.locator("#signup-email")).toBeFocused();
  await page.locator("#signup-email").fill("ux-fixture@example.com");
  await page.locator("#signup-password").fill("fixture-password-123");
  await page.locator("#signup-confirm-password").fill("fixture-password-123");
  const form = page.getByRole("form", { name: "创建账号" });
  await form.locator('button[type="submit"]').click();
  await expect(form.getByRole("button", { name: "创建中…" })).toBeDisabled();
  await expect.poll(() => submissions).toBe(1);
  await form.evaluate((node) => (node as HTMLFormElement).requestSubmit());
  expect(submissions).toBe(1);
  release?.();
  await expect(page.locator("#signup-account-error")).toBeVisible();
  await expect(page.locator("#signup-account-error")).toBeFocused();
  await expect(page.locator("#signup-email")).toHaveValue("ux-fixture@example.com");
  await expect(page.locator("#signup-password")).toHaveValue("fixture-password-123");
  await expect(form.locator('button[type="submit"]')).toBeEnabled();
  await recordControls(page, testInfo);
});

test("ux: reset request validates in place without losing the route or field context", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.goto("/reset-password");
  const input = page.locator("#reset-email");
  await expect(input).toBeVisible();
  await page.locator('form button[type="submit"]').click();
  await expect(input).toBeFocused();
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(input).toHaveAttribute("aria-describedby", "reset-form-error");
  await input.fill("ux-fixture@example.com");
  await expect(page.locator("#reset-form-error")).toHaveCount(0);
  await expect(page).toHaveURL(/\/reset-password$/u);
  await recordControls(page, testInfo);
});

test("ux: expired password reset keeps input, focuses the error and offers a fresh link", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/password/reset-confirm", (route) => json(route, { detail: { error: { code: "invalid_token", message: "重置链接已失效" } } }, 410));
  await page.goto("/reset-password/ux-fixture-token");
  const password = page.locator("#reset-password");
  const confirm = page.locator("#reset-confirm");
  await password.fill("fixture-password-123");
  await confirm.fill("different-password");
  await expect(confirm).toHaveAttribute("aria-invalid", "true");
  await expect(confirm).toHaveAttribute("aria-describedby", "reset-confirm-mismatch");
  await confirm.fill("fixture-password-123");
  await page.getByRole("button", { name: "更新密码" }).click();
  await expect(page.locator("#reset-confirm-error")).toBeFocused();
  await expect(password).toHaveValue("fixture-password-123");
  await expect(confirm).toHaveValue("fixture-password-123");
  await recordControls(page, testInfo);
  await page.getByRole("link", { name: "重新获取重置链接" }).click();
  await expect(page).toHaveURL(/\/reset-password$/u);
});

test("ux: successful reset moves focus to confirmation and keeps login reachable", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/password/reset-confirm", (route) => json(route, { ok: true }));
  await page.goto("/reset-password/ux-fixture-token");
  await page.locator("#reset-password").fill("fixture-password-123");
  await page.locator("#reset-confirm").fill("fixture-password-123");
  await page.getByRole("button", { name: "更新密码" }).click();
  await expect(page.locator("#reset-confirm-success")).toBeFocused();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.getByRole("link", { name: "去登录" })).toHaveAttribute("href", "/login");
  await recordControls(page, testInfo);
});
