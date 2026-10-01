import { expect, test } from "@playwright/test";
import { installAgentFixture, openAgent } from "./agent-fixture";

test("Astra settings preserve drafts, migrate off, and submit the selected reasoning preset", async ({ page }, testInfo) => {
  // This test exercises model settings and submission, not transport recovery.
  // A fulfilled SSE response closes immediately; keep the fixture connection
  // open so synthetic reconnects do not repeatedly revalidate identity.
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
  const fixture = await installAgentFixture(page, { mode: "text" });
  await page.route("**/api/agent/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      enabled: true, tool_gateway_configured: true, default_model: "fixture-model",
      models: [
        { model: "fixture-model", vision_supported: true, reasoning_supported: true },
        { model: "gpt-6-astra", vision_supported: true, reasoning_supported: true },
        { model: "gpt-6-sol", vision_supported: true, reasoning_supported: true },
      ],
    }),
  }));
  await openAgent(page);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  await input.fill("请分析这份需求，保留原始草稿");
  const summary = page.getByTestId("agent-execution-summary");
  const compactLandscape = await page.evaluate(() =>
    window.matchMedia("(orientation: landscape) and (max-height: 480px)").matches,
  );
  // Compact landscape hides the summary, not the input-row settings control.
  // Assert the intended layout explicitly instead of falling back on failure.
  if (compactLandscape) await expect(summary).toBeHidden();
  else await expect(summary).toBeVisible();
  const settingsTrigger = compactLandscape
    ? page.getByTestId("agent-composer").getByRole("button", { name: "Agent 设置", exact: true })
    : summary.getByRole("button", { name: /调整执行参数/u });
  await expect(settingsTrigger).toBeVisible();
  await settingsTrigger.click();
  const reasoning = page.getByRole("combobox", { name: "Agent 推理强度" });
  await reasoning.selectOption("none");
  await page.getByRole("combobox", { name: "Agent 模型" }).selectOption("gpt-6-astra");
  await expect(reasoning).toHaveValue("low");
  await expect(reasoning.locator("option")).toHaveText(["自动", "低", "中", "高", "超高", "最大"]);
  await page.getByRole("button", { name: "深入", exact: true }).click();
  await expect(reasoning).toHaveValue("high");
  await expect(page.getByRole("button", { name: "深入", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: testInfo.outputPath("astra-settings.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Agent 设置", exact: true })).toHaveCount(0);
  await expect(settingsTrigger).toBeFocused();
  await expect(input).toHaveValue("请分析这份需求，保留原始草稿");
  await expect(summary).toContainText("GPT-6 Astra");
  await expect(summary).toContainText("推理高");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => fixture.lastMessageBody?.model).toBe("gpt-6-astra");
  expect(fixture.lastMessageBody?.reasoning_effort).toBe("high");
});
