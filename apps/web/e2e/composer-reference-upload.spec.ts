import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Route } from "@playwright/test";

import { installAgentFixture } from "./agent-fixture";

const REFERENCE_IMAGE_PATH = resolve(
  process.cwd(),
  "../../assets/apparel-model-presets/01_toddler/male/toddler-male-mixed-001-069fa8f8-e104.thumb.webp",
);
const NOW = "2026-08-22T16:30:00Z";

function json(route: Route, payload: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

test("reference upload expands a collapsed composer and shows the attachment", async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => {
    class QuietEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly readyState = QuietEventSource.OPEN;
      readonly url = "";
      readonly withCredentials = false;
      onerror = null;
      onmessage = null;
      onopen = null;
      close() {}
    }
    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: QuietEventSource,
    });
  });
  await installAgentFixture(page);
  const conversation = {
    id: "conversation-1",
    title: "参考图回归",
    pinned: false,
    archived: false,
    memory_disabled: false,
    active_scope_id: null,
    last_activity_at: NOW,
    default_params: {},
    default_system: null,
    default_system_prompt_id: null,
    created_at: NOW,
  };
  let historyCalls = 0;
  let uploadCalls = 0;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/auth/me") {
      await new Promise((resolve) => setTimeout(resolve, 500));
      return json(route, {
        id: "user-a",
        email: "user-a@example.com",
        role: "member",
        account_mode: "wallet",
        runtime_defaults: {
          fast: true,
          canvas_enabled: false,
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
    if (path === "/api/conversations" && request.method() === "GET") {
      return json(route, { items: [conversation], next_cursor: null });
    }
    if (path === "/api/conversations/conversation-1") {
      return json(route, conversation);
    }
    if (path === "/api/conversations/conversation-1/messages") {
      historyCalls += 1;
      return json(route, {
        items: [],
        next_cursor: null,
        generations: [],
        completions: [],
        images: [],
      });
    }
    if (path === "/api/conversations/conversation-1/context") {
      return json(route, {
        input_budget_tokens: 128000,
        total_target_tokens: 128000,
        response_reserve_tokens: 4096,
        estimated_input_tokens: 0,
        estimated_history_tokens: 0,
        estimated_system_tokens: 0,
        included_messages_count: 0,
        truncated: false,
        percent: 0,
      });
    }
    if (path === "/api/images/upload" && request.method() === "POST") {
      uploadCalls += 1;
      return json(route, {
        id: "uploaded-reference-1",
        width: 4,
        height: 4,
        mime: "image/png",
        url: "/api/images/uploaded-reference-1/binary",
        display_url: "/api/images/uploaded-reference-1/binary",
        preview_url: "/api/images/uploaded-reference-1/binary",
        thumb_url: "/api/images/uploaded-reference-1/binary",
        metadata_jsonb: {},
      });
    }
    return route.fallback();
  });

  const authenticated = page.waitForResponse((response) => {
    const request = response.request();
    return (
      new URL(response.url()).pathname === "/api/auth/me" &&
      request.method() === "GET" &&
      response.ok()
    );
  });
  await page.goto("/?conversationId=conversation-1");
  await authenticated;
  const viewport = page.viewportSize();
  if ((viewport?.width ?? 0) < 768) {
    await expect(page.locator('[data-app-viewport="true"]')).toBeVisible();
  } else {
    await expect(page.getByTestId("desktop-primary-nav")).toBeVisible();
  }
  await expect.poll(() => historyCalls).toBeGreaterThan(0);
  await expect(
    page.getByText("登录状态确认中，写操作已暂时切换为只读", {
      exact: true,
    }),
  ).toHaveCount(0);
  const uploadButton = page.getByRole("button", { name: "添加参考图" }).first();
  await expect(uploadButton).toBeVisible();
  await expect(uploadButton).toBeEnabled();
  await expect(page.getByRole("button", { name: "移除参考图" })).toHaveCount(0);

  await page
    .locator('input[type="file"][accept="image/*"]')
    .last()
    .setInputFiles({
      name: "reference.webp",
      mimeType: "image/webp",
      buffer: readFileSync(REFERENCE_IMAGE_PATH),
    });

  const attachmentAction =
    (viewport?.width ?? 0) < 768
      ? page.getByRole("button", { name: "打开图 1 操作" })
      : page.getByRole("button", { name: "移除参考图" });
  await expect(attachmentAction).toBeVisible();
  expect(uploadCalls).toBe(1);
  await testInfo.attach("reference-upload-expanded", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
});
