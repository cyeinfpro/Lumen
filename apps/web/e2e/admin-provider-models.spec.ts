import { expect, test, type Page, type Route } from "@playwright/test";

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installAdminProviderFixture(page: Page) {
  let savedBody: Record<string, unknown> | null = null;
  await page
    .context()
    .addCookies([
      { name: "csrf", value: "fixture-csrf", domain: "127.0.0.1", path: "/" },
    ]);
  await page.route("**/events?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: 'event: heartbeat\ndata: {"schema_version":1}\n\n',
    }),
  );
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/auth/me") {
      await new Promise((resolve) => setTimeout(resolve, 200));
      return json(route, {
        id: "admin-1",
        email: "admin@example.com",
        role: "admin",
        account_mode: "wallet",
        runtime_defaults: {
          agent_enabled: true,
          nav_visibility: {
            studio: true,
            agent: true,
            video: true,
            projects: true,
            assets: true,
          },
        },
      });
    }
    if (path === "/api/auth/csrf") {
      return json(route, { csrf_token: "fixture-csrf" });
    }
    if (path === "/api/events") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: 'event: heartbeat\ndata: {"schema_version":1}\n\n',
      });
    }
    if (path === "/api/tasks/mine/active") {
      return json(route, { generations: [], completions: [] });
    }
    if (path === "/api/tasks") {
      return json(route, { items: [], next_cursor: null });
    }
    if (path === "/api/admin/models/discover" && method === "POST") {
      return json(route, {
        models: [
          {
            id: "gpt-4.1",
            profile: {
              agent_api: "openai-responses",
              responses_supported: true,
              vision_supported: true,
              context_window: 128000,
              max_output_tokens: 16384,
              reasoning_supported: false,
              source: "known_family",
            },
          },
          {
            id: "gpt-5.6-sol",
            profile: {
              agent_api: "openai-responses",
              responses_supported: true,
              vision_supported: true,
              context_window: 256000,
              max_output_tokens: 32000,
              reasoning_supported: true,
              source: "provider",
            },
          },
        ],
        fetched_at: "2026-08-22T00:00:00Z",
        error: null,
      });
    }
    if (path === "/api/admin/providers" && method === "GET") {
      return json(route, { items: [], proxies: [], source: "db" });
    }
    if (path === "/api/admin/providers" && method === "PUT") {
      savedBody = request.postDataJSON() as Record<string, unknown>;
      return json(route, { items: [], proxies: [], source: "db" });
    }
    if (path === "/api/admin/providers/stats") {
      return json(route, { items: [], auto_probe_interval: 120 });
    }
    if (path === "/api/admin/settings") {
      return json(route, {
        items: [
          {
            key: "upstream.default_model",
            value: "legacy-model",
            has_value: true,
            is_sensitive: false,
            description: "Default model",
          },
        ],
      });
    }
    if (path === "/api/admin/models") {
      return json(route, {
        models: [],
        fetched_at: "2026-08-22T00:00:00Z",
        errors: [],
      });
    }
    return json(route, {});
  });
  return { savedBody: () => savedBody };
}

test("provider URL and key discover models and fill Agent parameters", async ({
  page,
}) => {
  const fixture = await installAdminProviderFixture(page);
  await page.goto("/admin");
  const mobileAdminNav = page.getByRole("combobox", { name: "管理后台页面" });
  if ((page.viewportSize()?.width ?? 0) < 768) {
    await expect(mobileAdminNav).toBeVisible();
    await mobileAdminNav.selectOption({ label: "供应商" });
  } else {
    const providerButton = page.getByRole("button", {
      name: "供应商",
      exact: true,
    });
    await expect(providerButton).toBeVisible();
    await providerButton.click();
  }
  await page.getByRole("button", { name: "添加供应商" }).click();
  await page.getByPlaceholder("例如：主供应商").fill("主供应商");
  await page
    .getByPlaceholder("http://10.0.0.8:8000/v1")
    .fill("https://provider.example/v1");
  await page.getByPlaceholder("sk-...").fill("provider-secret");
  await page.getByPlaceholder("sk-...").press("Tab");

  const models = page.getByLabel("主供应商 Agent 模型");
  await expect(models).toBeVisible();
  await expect(models).toHaveValue("gpt-5.6-sol");
  await expect(
    page.getByRole("spinbutton", { name: /上下文窗口/ }),
  ).toHaveValue("256000");
  await expect(
    page.getByRole("spinbutton", { name: /单轮输出上限/ }),
  ).toHaveValue("32000");
  await expect(page.getByLabel("切换 Reasoning 能力")).toBeChecked();

  await page.keyboard.press("Escape");
  const saveButton = page.getByRole("button").filter({ hasText: "保存" });
  await expect(saveButton).toBeVisible();
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  await expect.poll(() => fixture.savedBody()).not.toBeNull();
  const saved = fixture.savedBody() as {
    default_model?: string;
    items?: Array<{ agent_models?: string[] }>;
  };
  expect(saved.default_model).toBe("gpt-5.6-sol");
  expect(saved.items?.[0]?.agent_models).toEqual(["gpt-4.1", "gpt-5.6-sol"]);
});
