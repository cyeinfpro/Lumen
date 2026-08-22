import {
  expect,
  test,
  type Page,
  type Response as PlaywrightResponse,
  type TestInfo,
} from "@playwright/test";

const enabled = process.env.AGENT_FULL_STACK_E2E === "1";
test.skip(!enabled, "requires the opt-in live Agent full-stack harness");

const controlUrl = process.env.AGENT_E2E_CONTROL_URL ?? "";
const controlToken = process.env.AGENT_E2E_CONTROL_TOKEN ?? "";
const referenceImage = process.env.AGENT_E2E_REFERENCE_IMAGE ?? "";
const userA = {
  email: process.env.AGENT_E2E_USER_A_EMAIL ?? "",
  password: process.env.AGENT_E2E_USER_A_PASSWORD ?? "",
};
const userB = {
  email: process.env.AGENT_E2E_USER_B_EMAIL ?? "",
  password: process.env.AGENT_E2E_USER_B_PASSWORD ?? "",
};

async function scenario(name: string, testInfo: TestInfo): Promise<void> {
  const response = await fetch(`${controlUrl.replace(/\/$/u, "")}/scenario`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${controlToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ name, test_id: testInfo.testId }),
  });
  if (!response.ok) {
    throw new Error(`Agent E2E scenario controller rejected ${name}: HTTP ${String(response.status)}`);
  }
}

async function login(page: Page, account = userA): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill(account.email);
  await page.getByLabel("密码").fill(account.password);
  await page.getByRole("button", { name: /^登录/u }).click();
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/u);
}

async function openAgent(page: Page): Promise<void> {
  await page.goto("/agent");
  await expect(page.locator("[data-agent-workspace]")).toBeVisible();
}

async function send(page: Page, prompt: string): Promise<PlaywrightResponse> {
  const response = page.waitForResponse(
    (candidate) =>
      candidate.request().method() === "POST" &&
      /\/api\/agent\/sessions\/[^/]+\/messages$/u.test(new URL(candidate.url()).pathname),
  );
  await page.getByRole("textbox", { name: "发送给 Agent" }).fill(prompt);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  return response;
}

async function uploadReference(page: Page, count = 1): Promise<void> {
  const input = page.locator('input[type="file"][accept*="image/png"]');
  await input.setInputFiles(Array.from({ length: count }, () => referenceImage));
  await expect(page.getByRole("combobox", { name: /参考图 1 角色/u })).toBeVisible();
}

async function runSnapshot(page: Page, runId: string): Promise<Record<string, unknown>> {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/agent/runs/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error(`run snapshot failed: ${String(response.status)}`);
    return response.json() as Promise<Record<string, unknown>>;
  }, runId);
}

test("live 01: text reaches Web/API/Worker/Runtime and persists", async ({ page }, info) => {
  await scenario("text", info);
  await login(page);
  await openAgent(page);
  const response = await send(page, "[E2E:text] Reply with LUMEN_AGENT_TEXT_OK");
  expect(response.ok()).toBe(true);
  await expect(page.getByText("LUMEN_AGENT_TEXT_OK")).toBeVisible();
  await page.reload();
  await expect(page.getByText("LUMEN_AGENT_TEXT_OK")).toBeVisible();
});

test("live 02: natural language creates one text-to-image task", async ({ page }, info) => {
  await scenario("text_to_image", info);
  await login(page);
  await openAgent(page);
  await send(page, "[E2E:t2i] Create one square red test image");
  await expect(page.locator('[data-agent-message-id] img').last()).toBeVisible();
  await expect(page.getByText(/文生图 · 已提交/u)).toBeVisible();
});

test("live 03: one owned reference routes image-to-image", async ({ page }, info) => {
  await scenario("image_to_image", info);
  await login(page);
  await openAgent(page);
  await uploadReference(page);
  await page.getByRole("combobox", { name: "参考图 1 角色" }).selectOption("product");
  await send(page, "[E2E:i2i] Preserve the product and change the background");
  await expect(page.getByText(/图生图 · 已提交/u)).toBeVisible();
});

test("live 04: multiple reference order and roles reach the run snapshot", async ({ page }, info) => {
  await scenario("multi_reference", info);
  await login(page);
  await openAgent(page);
  await uploadReference(page, 2);
  await page.getByRole("combobox", { name: "参考图 1 角色" }).selectOption("product");
  await page.getByRole("combobox", { name: "参考图 2 角色" }).selectOption("style");
  const response = await send(page, "[E2E:multi-ref] Use both references in order");
  const payload = await response.json() as { agent_run: { references: Array<{ ordinal: number; role: string }> } };
  expect(payload.agent_run.references.map((item) => [item.ordinal, item.role])).toEqual([
    [0, "product"],
    [1, "style"],
  ]);
});

test("live 05: refresh during generation does not duplicate the task", async ({ page }, info) => {
  await scenario("slow_generation", info);
  await login(page);
  await openAgent(page);
  await send(page, "[E2E:slow-image] Create one delayed image");
  await expect(page.getByText(/图片生成中|图片排队中/u)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/图片生成中|图片排队中|已提交/u)).toBeVisible();
  await expect(page.getByText(/文生图/u)).toHaveCount(1);
});

test("live 06: pre-tool Runtime disconnect fails without a Generation", async ({ page }, info) => {
  await scenario("runtime_disconnect_text", info);
  await login(page);
  await openAgent(page);
  const response = await send(page, "[E2E:disconnect-text] stream then disconnect");
  const payload = await response.json() as { agent_run: { id: string } };
  await expect(page.getByText(/运行失败/u)).toBeVisible();
  const run = await runSnapshot(page, payload.agent_run.id);
  expect(run.tool_calls).toEqual([]);
});

test("live 07: post-tool disconnect is partial and creates one batch", async ({ page }, info) => {
  await scenario("runtime_disconnect_after_tool", info);
  await login(page);
  await openAgent(page);
  const response = await send(page, "[E2E:disconnect-tool] submit once then disconnect");
  const payload = await response.json() as { agent_run: { id: string } };
  await expect(page.getByText("部分完成")).toBeVisible();
  const run = await runSnapshot(page, payload.agent_run.id) as {
    status: string;
    tool_calls: Array<{ generation_ids: string[] }>;
  };
  expect(run.status).toBe("partial");
  expect(run.tool_calls).toHaveLength(1);
  expect(run.tool_calls[0]?.generation_ids).toHaveLength(1);
});

test("live 08: cancellation fences later tool callbacks", async ({ page }, info) => {
  await scenario("cancel_before_tool", info);
  await login(page);
  await openAgent(page);
  const response = await send(page, "[E2E:cancel] wait before attempting a tool");
  const payload = await response.json() as { agent_run: { id: string } };
  await page.getByRole("button", { name: "停止 Agent 运行" }).click();
  await expect(page.getByText("已取消")).toBeVisible();
  const run = await runSnapshot(page, payload.agent_run.id);
  expect(run.status).toBe("cancelled");
  expect(run.tool_calls).toEqual([]);
});

test("live 09a: insufficient balance preflight creates no image", async ({ page }, info) => {
  await scenario("insufficient_balance", info);
  await login(page);
  await openAgent(page);
  await send(page, "[E2E:insufficient-balance] must be rejected before dispatch");
  await expect(page.getByText("充值后可继续运行 Agent。")).toBeVisible();
  await expect(page.getByText(/文生图|图生图/u)).toHaveCount(0);
});

test("live 09b: unavailable BYOK preflight creates no image", async ({ page }, info) => {
  await scenario("byok_unavailable", info);
  await login(page, userB);
  await openAgent(page);
  await send(page, "[E2E:byok-unavailable] must be rejected before dispatch");
  await expect(page.getByText("添加有效密钥后可继续运行 Agent。")).toBeVisible();
  await expect(page.getByText(/文生图|图生图/u)).toHaveCount(0);
});

test("live 09c: unavailable vision preflight creates no image", async ({ page }, info) => {
  await scenario("vision_unavailable", info);
  await login(page);
  await openAgent(page);
  await uploadReference(page);
  await send(page, "[E2E:vision-unavailable] must be rejected before dispatch");
  await expect(page.getByText("当前对话模型不支持参考图。")).toBeVisible();
  await expect(page.getByText(/文生图|图生图/u)).toHaveCount(0);
});

test("live 10: two users isolate messages, events, references, and tokens", async ({ browser }, info) => {
  await scenario("two_user_isolation", info);
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  try {
    await Promise.all([login(pageA, userA), login(pageB, userB)]);
    await Promise.all([openAgent(pageA), openAgent(pageB)]);
    await uploadReference(pageA);
    const [responseA] = await Promise.all([
      send(pageA, "[E2E:isolation-a] use my reference and reply USER_A_ONLY"),
      send(pageB, "[E2E:isolation-b] reply USER_B_ONLY"),
    ]);
    const payloadA = await responseA.json() as {
      agent_run: {
        id: string;
        references: Array<{ image_id: string }>;
      };
    };
    expect(JSON.stringify(payloadA)).not.toMatch(
      /api[_-]?key|authorization|capability|tool[_-]?token/iu,
    );
    const crossUserStatuses = await pageB.evaluate(
      async ({ runId, imageId }) => {
        const [run, image] = await Promise.all([
          fetch(`/api/agent/runs/${encodeURIComponent(runId)}`),
          fetch(`/api/images/${encodeURIComponent(imageId)}/binary`),
        ]);
        return { run: run.status, image: image.status };
      },
      {
        runId: payloadA.agent_run.id,
        imageId: payloadA.agent_run.references[0]?.image_id ?? "missing",
      },
    );
    expect(crossUserStatuses).toEqual({ run: 404, image: 404 });
    await expect(pageA.getByText("USER_A_ONLY")).toBeVisible();
    await expect(pageA.getByText("USER_B_ONLY")).toHaveCount(0);
    await expect(pageB.getByText("USER_B_ONLY")).toBeVisible();
    await expect(pageB.getByText("USER_A_ONLY")).toHaveCount(0);
  } finally {
    await contextA.close();
    await contextB.close();
  }
});
