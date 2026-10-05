import { expect, test, type Page } from "@playwright/test";

async function openInvites(page: Page) {
  if ((page.viewportSize()?.width ?? 0) < 768) {
    await page.getByRole("combobox", { name: "管理后台页面" }).selectOption("invites");
  } else {
    await page.getByRole("button", { name: "邀请链接", exact: true }).click();
  }
  await expect(page.getByRole("heading", { name: "生成邀请链接" })).toBeVisible();
}

test("redacted history, one-time copy, reload and revoke remain usable", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (/getSnapshot.*cached|Maximum update depth/.test(message.text())) errors.push(message.text());
  });
  await page.addInitScript(() => {
    // This CRUD fixture has no live task stream. Keep reloads independent of
    // WebKit EventSource teardown errors from a finite mocked SSE response.
    class FixtureEventSource extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      readyState = 1;
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, "EventSource", { value: FixtureEventSource });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value: string) => { sessionStorage.setItem("copied-invite", value); } },
    });
  });
  const baseRow = {
    id: "invite-1", token: "redacted", url: "https://lumen.example/invite/redacted",
    email: null, role: "member", used_at: null, used_by_email: null, revoked_at: null,
    expires_at: "2099-01-01T00:00:00Z", created_at: "2026-10-05T00:00:00Z",
  };
  const rows = [baseRow];
  let revoked = 0;
  await page.context().addCookies([
    { name: "csrf", value: "fixture-csrf", domain: "127.0.0.1", path: "/" },
  ]);
  await page.route("**/events?**", (route) => route.fulfill({
    status: 200, contentType: "text/event-stream", body: 'event: heartbeat\ndata: {"schema_version":1}\n\n',
  }));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const json = (body: unknown, status = 200) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(body),
    });
    if (path === "/api/events") return route.fulfill({
      status: 200, contentType: "text/event-stream", body: 'event: heartbeat\ndata: {"schema_version":1}\n\n',
    });
    if (path === "/api/auth/me") return json({
      id: "admin-1", email: "admin@example.test", role: "admin", account_mode: "wallet",
      runtime_defaults: { agent_enabled: false, nav_visibility: {} },
    });
    if (path === "/api/auth/csrf") return json({ csrf_token: "fixture-csrf" });
    if (path === "/api/tasks/mine/active") return json({ generations: [], completions: [] });
    if (path === "/api/tasks") return json({ items: [], next_cursor: null });
    if (path === "/api/admin/invite_links" && method === "GET") return json({ items: rows });
    if (path === "/api/admin/invite_links" && method === "POST") {
      const row = { ...baseRow, id: "invite-new" };
      rows.push(row);
      return json({ ...row, token: "real-new-token", url: "https://lumen.example/invite/real-new-token" }, 201);
    }
    if (path.startsWith("/api/admin/invite_links/") && method === "DELETE") {
      revoked += 1;
      const row = rows.find((item) => path.endsWith(item.id));
      if (row) Object.assign(row, { revoked_at: "2026-10-05T01:00:00Z" });
      return route.fulfill({ status: 204 });
    }
    return json({});
  });
  await page.goto("/admin");
  await openInvites(page);
  await expect(page.getByText("链接已隐藏，仅生成时可复制").filter({ visible: true })).toHaveCount(1);
  await expect(page.getByText(/\/invite\/redacted/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /复制/ })).toHaveCount(0);
  await page.getByRole("button", { name: "生成链接", exact: true }).click();
  await expect(page.getByText("新邀请已生成")).toBeVisible();
  await page.getByRole("button", { name: "复制", exact: true }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("copied-invite"))).toBe(
    "https://lumen.example/invite/real-new-token",
  );
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(page.getByText("新邀请已生成")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /复制/ })).toHaveCount(0);
  await page.reload();
  await openInvites(page);
  await expect(page.getByText(/real-new-token|\/invite\/redacted/)).toHaveCount(0);
  const revoke = page.getByRole("button", { name: "撤销", exact: true });
  await revoke.first().click();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  expect(revoked).toBe(0);
  await revoke.first().click();
  const confirm = page.getByRole("button", { name: /^(确认撤销|撤销)$/ }).filter({ visible: true });
  // The confirming row is first in both responsive layouts.
  await confirm.first().click();
  await expect.poll(() => revoked).toBe(1);
  await expect(page.getByText("已撤销").filter({ visible: true })).toHaveCount(1);
  expect(errors).toEqual([]);
});
