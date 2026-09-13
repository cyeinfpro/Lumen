import { expect, test } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

test("idle Agent session accepts null and recovers messages after session switch and focus", async ({ page }) => {
  // A fresh Next dev server compiles the Agent route on its first visit.
  // Keep all recovery assertions at the normal 8-second expect deadline.
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  // Keep SSE genuinely open rather than letting a finite mocked HTTP body
  // introduce unrelated reconnect errors into this nullable-response test.
  await page.addInitScript(() => {
    class OpenEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = true;
      readyState = OpenEventSource.OPEN;
      onerror: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onopen: ((event: Event) => void) | null = null;
      constructor(url: string | URL) {
        super();
        this.url = String(url);
        queueMicrotask(() => {
          if (this.readyState !== OpenEventSource.OPEN) return;
          const event = new Event("open");
          this.onopen?.(event);
          this.dispatchEvent(event);
        });
      }
      close() { this.readyState = OpenEventSource.CLOSED; }
    }
    Object.defineProperty(window, "EventSource", { configurable: true, value: OpenEventSource });
  });
  await installAgentFixture(page, { sessionCount: 2 });
  let published = false;
  let messageCalls = 0;
  let nullResponses = 0;
  page.on("response", (response) => {
    if (response.url().includes("/session-2/active-run") && response.status() === 200) nullResponses++;
  });
  await page.route("**/api/agent/sessions/session-2/messages?**", (route) => {
    messageCalls++;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      items: published ? [{
        id: "idle-recovered-message", conversation_id: "conversation-2", role: "user",
        content: { source: "agent", text: "空闲会话恢复后的新消息" }, intent: "agent",
        status: null, parent_message_id: null, created_at: "2026-09-13T00:00:00Z",
      }] : [],
      runs: [], next_cursor: null, generations: [], completions: [], images: [],
    }) });
  });
  await page.goto("/agent?session=session-1");
  // Dev-only query inspector artwork can cover tablet navigation while its
  // styles load. Exclude the inspector, not application controls or overlays.
  await page.addStyleTag({ content: ".tsqd-parent-container { display: none !important; }" });
  await expect(page.locator("[data-agent-workspace]")).toBeVisible({ timeout: 30_000 });
  const ready = page.locator('[data-agent-workspace] [role="status"]').filter({ hasText: "已就绪" });
  await expect(ready.first()).toBeVisible();
  // Phones and tablet-width desktop layouts have different drawer triggers.
  const openSessions = page.getByRole("button", {
    name: /^(打开 Agent 会话列表|打开会话侧栏)$/u,
  }).first();
  if (await openSessions.isVisible()) await openSessions.click();
  await page.locator('[data-agent-session-id="session-2"] > div > button:visible').first().click();
  await expect(page).toHaveURL(/session=session-2/u);
  await page.keyboard.press("Escape");
  await expect.poll(() => nullResponses).toBeGreaterThan(0);
  await expect(ready.first()).toBeVisible();
  await expect.poll(() => messageCalls).toBeGreaterThan(0);
  const beforeFocus = messageCalls;
  published = true;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => messageCalls).toBeGreaterThan(beforeFocus);
  await expect(page.getByText("空闲会话恢复后的新消息", { exact: true })).toBeVisible();
  await expect(ready.first()).toBeVisible();
  expect(errors.filter((text) => /response_schema_error|Successful typed API response contains JSON null/u.test(text))).toEqual([]);
});
