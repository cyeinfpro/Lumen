import { expect, test } from "@playwright/test";
import { installAgentFixture, openAgent } from "./agent-fixture";

const NOW = "2026-09-05T08:00:00Z";

function auditSnapshot(errorCode: string | null = null, cancelRequested = false) {
  const status = errorCode ? "failed" : cancelRequested ? "running" : "succeeded";
  return {
    items: [
      { id: "user-1", conversation_id: "conversation-1", role: "user", content: { source: "agent", text: "审计任务" }, intent: "agent", status: null, parent_message_id: null, created_at: "2026-09-05T07:59:59Z" },
      { id: "assistant-1", conversation_id: "conversation-1", role: "assistant", content: { source: "agent", agent_run_id: "run-1", text: Array.from({ length: 60 }, (_, i) => `历史段落 ${i + 1}：已确认的任务记录。`).join("\n\n") }, intent: "agent", status, parent_message_id: "user-1", created_at: NOW },
    ],
    runs: [{
      id: "run-1", agent_session_id: "session-1", user_message_id: "user-1", assistant_message_id: "assistant-1",
      status, execution_epoch: 1, last_event_seq: 8, idempotency_key: "audit-operation", model: "fixture-model", reasoning_effort: null,
      turn_count: 1, tool_call_count: 2, usage: {}, error_code: errorCode, error_message: null, continuable: false,
      started_at: NOW, finished_at: cancelRequested ? null : NOW, cancel_requested_at: cancelRequested ? NOW : null,
      created_at: NOW, updated_at: NOW, references: [],
      tool_calls: [
        { id: "audit-tool", agent_run_id: "run-1", ordinal: 0, name: "lumen_read_file", mode: "file_read", status: "succeeded", generation_ids: [], generation_count: 0, error_code: null, started_at: NOW, finished_at: NOW, created_at: NOW, updated_at: NOW, duration_ms: 200,
          details: { kind: "file_read", file_names: ["brief.md"], query: null, line_start: 1, line_end: 40, result_snippets: Array.from({ length: 6 }, (_, i) => `源文件记录 ${i + 1}：${"保持可核对的执行结果。".repeat(20)}`) } },
        { id: "audit-failed-tool", agent_run_id: "run-1", ordinal: 1, name: "lumen_web_search", mode: "web_search", status: "failed", generation_ids: [], generation_count: 0, error_code: "agent_web_search_unavailable", started_at: NOW, finished_at: NOW, created_at: NOW, updated_at: NOW, duration_ms: 100, details: null },
      ],
    }],
    next_cursor: null, generations: [], completions: [], images: [],
  };
}

for (const width of [360, 390, 768, 1024, 1440]) {
  test(`Agent audit inspector preserves one controlled draft at ${width}px`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-light", "explicit audit width matrix");
    await page.setViewportSize({ width, height: 900 });
    const fixture = await installAgentFixture(page, { mode: "text" });
    await openAgent(page);
    const input = page.getByRole("textbox", { name: "发送给 Agent" });
    await input.fill("保留这份草稿");
    await expect(page.locator("[data-agent-workspace]")).toHaveCount(1);
    await expect(page.getByTestId("agent-composer")).toHaveCount(1);
    const summary = page.getByTestId("agent-execution-summary");
    await expect(summary.getByRole("combobox")).toHaveCount(0);
    const composerBefore = await page.getByTestId("agent-composer").boundingBox();
    await summary.getByRole("button", { name: /调整执行参数/u }).click();
    await expect(page.getByRole("dialog", { name: "Agent 设置", exact: true })).toHaveCount(1);
    await page.getByRole("combobox", { name: "默认图片数量" }).selectOption("3");
    await page.getByRole("combobox", { name: "Agent 推理强度" }).selectOption("high");
    const model = page.getByRole("combobox", { name: "Agent 模型" });
    await model.selectOption("fixture-fast-model");
    await expect(model).toHaveValue("");
    await page.getByRole("group", { name: "确认模型变更" }).getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Agent 推理强度" })).toHaveValue("high");
    await model.selectOption("fixture-fast-model");
    await page.getByRole("button", { name: "确认切换", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Agent 推理强度" })).toHaveValue("none");
    await page.keyboard.press("Escape");
    await expect(input).toHaveValue("保留这份草稿");
    await expect(summary).toContainText("3 张");
    const composerAfter = await page.getByTestId("agent-composer").boundingBox();
    expect(Math.abs((composerAfter?.height ?? 0) - (composerBefore?.height ?? 0))).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    const summaryGeometry = await summary.evaluate((node) => {
      const trigger = node.querySelector("button")!.getBoundingClientRect();
      const cost = node.querySelector("[data-agent-cost-estimate]")?.getBoundingClientRect();
      return !cost || trigger.right <= cost.left + 1 || trigger.bottom <= cost.top + 1;
    });
    expect(summaryGeometry).toBe(true);
    for (const theme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: theme });
      const material = await page.getByTestId("agent-composer").evaluate((node) => {
        const style = getComputedStyle(node);
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d")!;
        context.fillStyle = style.backgroundColor;
        context.fillRect(0, 0, 1, 1);
        return { alpha: context.getImageData(0, 0, 1, 1).data[3], backdrop: style.backdropFilter };
      });
      expect(material).toEqual({ alpha: 255, backdrop: "none" });
      await testInfo.attach(`agent-ui-${width}-${theme}`, { body: await page.screenshot(), contentType: "image/png" });
    }
    expect(fixture.lastMessageBody).toBeNull();
  });
}

test("Agent audit tool resizing follows the bottom but preserves reading history with reduced motion", async ({ page }) => {
  await installAgentFixture(page);
  await page.route("**/api/agent/sessions/session-1/messages?**", (route) => route.fulfill({ json: auditSnapshot() }));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openAgent(page);
  const scroll = page.getByTestId("agent-conversation-scroll");
  const tool = page.locator('[data-agent-tool-call="audit-tool"] > button');
  const failed = page.locator('[data-agent-tool-call="audit-failed-tool"]');
  await expect(failed.getByRole("alert")).toContainText("联网搜索暂不可用");
  await expect(failed.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  await scroll.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
    node.dispatchEvent(new Event("scroll"));
  });
  await tool.evaluate((node) => (node as HTMLButtonElement).click());
  await expect.poll(() => scroll.evaluate((node) => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop))).toBeLessThanOrEqual(2);
  await tool.evaluate((node) => (node as HTMLButtonElement).click());
  await scroll.evaluate((node) => {
    node.scrollTop = 200;
    node.dispatchEvent(new Event("scroll"));
  });
  const before = await scroll.evaluate((node) => node.scrollTop);
  await tool.evaluate((node) => (node as HTMLButtonElement).click());
  await expect(tool).toHaveAttribute("aria-expanded", "true");
  await expect.poll(() => scroll.evaluate((node) => node.scrollTop)).toBe(before);
  await expect(page.locator('[data-agent-tool-call="audit-tool"] [role="region"]')).toHaveAttribute("aria-hidden", "false");
});

test("Agent audit explicit history target survives content resizing", async ({ page }) => {
  await installAgentFixture(page);
  await page.route("**/api/agent/sessions/session-1/messages?**", (route) => route.fulfill({ json: auditSnapshot() }));
  await page.goto("/agent?session=session-1&scrollTo=user-1");
  const target = page.locator("#agent-message-user-1");
  await expect(target).toBeInViewport();
  const scroll = page.getByTestId("agent-conversation-scroll");
  const before = await scroll.evaluate((node) => node.scrollTop);
  await page.locator('[data-agent-tool-call="audit-tool"] > button').evaluate((node) => (node as HTMLButtonElement).click());
  await expect(target).toBeInViewport();
  await expect.poll(() => scroll.evaluate((node) => node.scrollTop)).toBe(before);
});

test("Agent audit uncertain status refreshes snapshots without posting a changed draft", async ({ page }) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  let snapshots = 0;
  await page.route("**/api/agent/sessions/session-1/messages?**", (route) => {
    snapshots += 1;
    return route.fulfill({ json: auditSnapshot("agent_submission_uncertain") });
  });
  await openAgent(page);
  await expect(page.locator('[data-agent-run-state="uncertain"]')).toContainText("提交待确认");
  await expect(page.getByText("运行失败", { exact: true })).toHaveCount(0);
  await page.getByRole("textbox", { name: "发送给 Agent" }).fill("已编辑的下一轮草稿");
  const before = snapshots;
  await page.getByRole("button", { name: "核对任务", exact: true }).click();
  await expect.poll(() => snapshots).toBeGreaterThan(before);
  expect(fixture.lastMessageBody).toBeNull();
  await expect(page.getByRole("textbox", { name: "发送给 Agent" })).toHaveValue("已编辑的下一轮草稿");
  await page.route("**/api/images/upload", (route) => route.fulfill({ status: 400, json: { detail: { code: "invalid_image", message: "图片格式不正确" } } }));
  await page.locator('input[type="file"][accept^="image/png"]').setInputFiles({
    name: "reference.png", mimeType: "image/png",
    buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64"),
  });
  await expect(page.getByTestId("agent-composer").getByRole("alert")).toBeVisible();
  await expect(page.getByTestId("agent-composer").getByRole("status")).toContainText("提交待确认");
});

test("Agent audit lost acknowledgements retain one logical key across manual retry and reload", async ({ page }) => {
  await installAgentFixture(page, { mode: "text" });
  const keys: string[] = [];
  await page.route("**/api/agent/sessions/session-1/messages", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON();
    expect(route.request().headers()["idempotency-key"]).toBe(body.idempotency_key);
    keys.push(body.idempotency_key);
    return route.fulfill({ status: 504, json: { error: { code: "gateway_timeout", message: "Acknowledgement unavailable" } } });
  });
  await openAgent(page);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  await input.fill("保留待确认请求");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator('[data-agent-run-state="uncertain"]')).toHaveCount(1);
  await expect(page.getByTestId("agent-composer").getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "核对任务", exact: true }).click();
  expect(keys).toHaveLength(3); // Existing transport allows two bounded 504 retries.
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => keys.length).toBe(6);
  await expect(page.locator('[data-agent-run-state="uncertain"]')).toHaveCount(1);
  expect(new Set(keys).size).toBe(1);
  await page.reload();
  await expect(input).toHaveValue("保留待确认请求");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => keys.length).toBe(9);
  expect(new Set(keys).size).toBe(1);
  await expect(page.locator('[data-agent-run-state="uncertain"]')).toHaveCount(1);
});

test("Agent retry keeps its local key after another tab confirms and retires the shared pending lease", async ({ page, context }) => {
  const other = await context.newPage();
  await installAgentFixture(page);
  await installAgentFixture(other);
  const keys: string[] = [];
  let confirmed = false;
  const receipt = (key: string) => {
    const snapshot = auditSnapshot();
    snapshot.runs[0]!.idempotency_key = key;
    return { user_message: snapshot.items[0], assistant_message: snapshot.items[1], agent_run: snapshot.runs[0] };
  };
  await page.route("**/api/agent/sessions/session-1/messages", (route) => {
    const body = route.request().postDataJSON();
    keys.push(body.idempotency_key);
    return confirmed ? route.fulfill({ json: receipt(body.idempotency_key) })
      : route.fulfill({ status: 504, json: { error: { code: "gateway_timeout", message: "Lost acknowledgement" } } });
  });
  await other.route("**/api/agent/sessions/session-1/messages", (route) => {
    const key = route.request().postDataJSON().idempotency_key;
    keys.push(key);
    confirmed = true;
    return route.fulfill({ json: receipt(key) });
  });
  await openAgent(page);
  await openAgent(other);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  await input.fill("跨标签页待确认草稿");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator('[data-agent-run-state="uncertain"]')).toHaveCount(1);
  await other.getByRole("textbox", { name: "发送给 Agent" }).fill("跨标签页待确认草稿");
  await other.getByRole("button", { name: "发送", exact: true }).click();
  await expect(other.getByRole("textbox", { name: "发送给 Agent" })).toHaveValue("");
  await expect(input).toHaveValue("跨标签页待确认草稿");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(input).toHaveValue("");
  expect(keys).toHaveLength(5);
  expect(new Set(keys).size).toBe(1);
  await expect(page.locator('[data-agent-run-state]')).toHaveCount(1);
  await other.close();
});

for (const edited of [false, true]) {
  test(`Agent accepted snapshot after reload acknowledges the bound draft, edited=${edited}`, async ({ page }) => {
    await installAgentFixture(page);
    let key = "";
    let showReceipt = false;
    await page.route("**/api/agent/sessions/session-1/messages", (route) => {
      key = route.request().postDataJSON().idempotency_key;
      return route.fulfill({ status: 504, json: { error: { code: "gateway_timeout", message: "Lost acknowledgement" } } });
    });
    await page.route("**/api/agent/sessions/session-1/messages?**", (route) => {
      if (!showReceipt) return route.fallback();
      const snapshot = auditSnapshot();
      snapshot.runs[0]!.idempotency_key = key;
      return route.fulfill({ json: snapshot });
    });
    await openAgent(page);
    const input = page.getByRole("textbox", { name: "发送给 Agent" });
    await input.fill("已提交草稿");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.locator('[data-agent-run-state="uncertain"]')).toHaveCount(1);
    if (edited) await input.fill("后续编辑保留");
    showReceipt = true;
    await page.reload();
    await expect(page.locator('[data-agent-run-state="succeeded"]')).toHaveCount(1);
    await expect(input).toHaveValue(edited ? "后续编辑保留" : "");
  });
}

test("Agent reconciliation disables duplicate checks while the loaded messages are refetching", async ({ page }) => {
  await installAgentFixture(page);
  let hold = false;
  let requests = 0;
  let release = () => {};
  await page.route("**/api/agent/sessions/session-1/messages?**", async (route) => {
    if (hold) {
      requests += 1;
      await new Promise<void>((resolve) => { release = resolve; });
    }
    return route.fulfill({ json: auditSnapshot("agent_submission_uncertain") });
  });
  await openAgent(page);
  await page.getByRole("textbox", { name: "发送给 Agent" }).fill("核对时保留");
  const check = page.getByRole("button", { name: "核对任务", exact: true });
  await expect(check).toBeEnabled();
  hold = true;
  await check.click();
  const pending = check;
  await expect(pending).toBeDisabled();
  await expect(pending).toHaveAttribute("aria-busy", "true");
  await pending.evaluate((node) => { (node as HTMLButtonElement).click(); (node as HTMLButtonElement).click(); });
  await expect.poll(() => requests).toBe(1);
  hold = false;
  release();
  await expect(check).toBeEnabled();
  expect(requests).toBe(1);
  await expect(page.getByRole("textbox", { name: "发送给 Agent" })).toHaveValue("核对时保留");
});

test("Agent audit server stop request remains pending instead of implying cancellation", async ({ page }) => {
  await installAgentFixture(page);
  await page.route("**/api/agent/sessions/session-1/messages?**", (route) => route.fulfill({ json: auditSnapshot(null, true) }));
  await openAgent(page);
  await expect(page.locator('[data-agent-run-state="stopping"]')).toContainText("等待确认");
  await expect(page.getByRole("button", { name: "停止请求中", exact: true })).toBeDisabled();
  await expect(page.getByText("已取消", { exact: true })).toHaveCount(0);
});
