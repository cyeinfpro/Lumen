import { expect, test } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

for (const scenario of ["svg", "svg-default", "svg-disabled", "png", "gif", "gif-auto"]) {
  const format = scenario.startsWith("svg") ? "svg" : scenario.startsWith("gif") ? "gif" : scenario;
  test(`SVG author UI sends independent config only for ${scenario}`, async ({ page }) => {
    await installAgentFixture(page);
    let submitted: Record<string, unknown> | null = null;
    await page.route("**/api/conversations/*/messages", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      submitted = route.request().postDataJSON();
      await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "fixture captured request" }) });
    });
    const historyReady = page.waitForResponse((response) => response.request().method() === "GET" && new URL(response.url()).pathname === "/api/conversations/studio-conversation-1/messages" && response.ok());
    await page.goto("/?conversationId=studio-conversation-1");
    await historyReady;
    if ((page.viewportSize()?.width ?? 0) >= 768) await page.getByRole("button", { name: "展开输入框" }).click();
    await page.getByRole("textbox", { name: "输入提示词" }).fill("A vector lantern");
    await page.getByRole("tablist", { name: "模式", exact: true }).getByRole("tab", { name: "生图", exact: true }).click();
    const settings = page.getByRole("button", { name: "执行设置", exact: true });
    await settings.click();
    const dialog = page.getByRole("dialog", { name: /^(高级)?执行设置$/ });
    await dialog.getByRole("combobox", { name: "输出格式", exact: true }).selectOption("svg");
    const author = dialog.getByRole("textbox", { name: "SVG 作者/绘制模型 ID", exact: true });
    const effort = dialog.getByRole("combobox", { name: "SVG 作者推理强度", exact: true });
    await expect(author).toHaveValue("");
    await expect(dialog).toContainText("不代表当前路由");
    await expect(dialog).toContainText("gpt-6.1-sol");
    await expect(effort).toBeDisabled();
    await author.fill("fixture-author-id");
    await effort.selectOption("max");
    await page.keyboard.press("Escape");
    await settings.click();
    await expect(author).toHaveValue("fixture-author-id");
    await expect(effort).toHaveValue("max");
    if (scenario === "svg-default") await effort.selectOption("");
    if (scenario === "svg-disabled") await author.fill("   ");
    await dialog.getByRole("combobox", { name: "输出格式", exact: true }).selectOption(format);
    if (format === "gif") {
      if (scenario === "gif-auto") await dialog.getByRole("combobox", { name: "GIF 每帧尺寸", exact: true }).selectOption("auto");
      else {
        await dialog.getByRole("spinbutton", { name: "GIF 宽", exact: true }).fill("767");
        await expect(dialog.getByRole("alert")).toContainText("16 的倍数");
        await dialog.getByRole("spinbutton", { name: "GIF 宽", exact: true }).fill("768");
        await dialog.getByRole("spinbutton", { name: "GIF 高", exact: true }).fill("432");
        await expect(dialog.getByRole("alert")).toHaveCount(0);
      }
    }
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => submitted).not.toBeNull();
    const params = submitted!.image_params as Record<string, unknown>;
    if (scenario === "svg") expect(params.svg).toEqual({ model: "fixture-author-id", reasoning: { effort: "max" } });
    else if (scenario === "svg-default") expect(params.svg).toEqual({ model: "fixture-author-id" });
    else expect(params).not.toHaveProperty("svg");
    if (scenario === "gif") expect(params).toMatchObject({ size_mode: "fixed", fixed_size: "768x432" });
    if (scenario === "gif-auto") {
      expect(params.size_mode).toBe("auto");
      expect(params).not.toHaveProperty("fixed_size");
    }
  });
}

test("GIF presets are explicit, editable, and preserve timing across reopening", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  let submitted: Record<string, unknown> | null = null;
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "fixture captured request" }) });
  });
  const historyReady = page.waitForResponse((response) => response.request().method() === "GET" && new URL(response.url()).pathname === "/api/conversations/studio-conversation-1/messages" && response.ok());
  await page.goto("/?conversationId=studio-conversation-1");
  await historyReady;
  if ((page.viewportSize()?.width ?? 0) >= 768) await page.getByRole("button", { name: "展开输入框" }).click();
  await page.getByRole("textbox", { name: "输入提示词" }).fill("A waving robot");
  await page.getByRole("tablist", { name: "模式", exact: true }).getByRole("tab", { name: "生图", exact: true }).click();
  const settings = page.getByRole("button", { name: "执行设置", exact: true });
  await settings.click();
  const dialog = page.getByRole("dialog", { name: /^(高级)?执行设置$/ });
  await dialog.getByRole("combobox", { name: "输出格式", exact: true }).selectOption("gif");
  const preset = dialog.getByRole("combobox", { name: "GIF 尺寸帧数预设", exact: true });
  const frames = dialog.getByRole("spinbutton", { name: "动画帧数", exact: true });
  const duration = dialog.getByRole("spinbutton", { name: "每帧时长", exact: true });
  await duration.fill("20");
  await preset.selectOption("768x432-16");
  await expect(frames).toHaveValue("16");
  await expect(duration).toHaveValue("20");
  await expect(dialog.getByRole("status", { name: "GIF 计时预览" })).toContainText("50 FPS · 每轮 0.32 秒");
  await expect(dialog.getByRole("spinbutton", { name: "GIF 宽", exact: true })).toHaveValue("768");
  await preset.selectOption("960x960-9");
  await expect(frames).toHaveValue("9");
  await expect(duration).toHaveValue("20");
  await frames.fill("16");
  await expect(preset).toHaveValue("");
  await expect(dialog.getByRole("spinbutton", { name: "GIF 宽", exact: true })).toHaveValue("960");
  await duration.fill("33");
  await expect(dialog.getByRole("alert")).toContainText("10 ms");
  await duration.fill("20");
  await preset.selectOption("768x432-16");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await settings.click();
  await expect(preset).toHaveValue("768x432-16");
  await expect(duration).toHaveValue("20");
  await expect(dialog).toContainText("不自动放大");
  await expect(dialog).toContainText("不代表上游接受");
  await expect(dialog).toContainText("flux.infpro.me/v1");
  await page.screenshot({ path: testInfo.outputPath("gif-controls.png") });
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("combobox", { name: "GIF 每帧尺寸", exact: true }).selectOption("auto");
  await expect(preset).toHaveValue("");
  await expect(frames).toHaveValue("16");
  await preset.selectOption("512x512-4");
  await expect(dialog.getByRole("combobox", { name: "GIF 每帧尺寸", exact: true })).toHaveValue("fixed");
  await expect(frames).toHaveValue("4");
  await expect(duration).toHaveValue("20");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted!.image_params).toMatchObject({ fixed_size: "512x512", frame_count: 4, frame_duration_ms: 20, count: 1 });
});

test("provider SVG advice is read-only, scoped, and absent for unverified providers", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/auth/me", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "fixture-admin", email: "fixture@example.com", role: "admin", account_mode: "wallet" }) }));
  let writes = 0;
  const base = {
    base_url: "https://example.test/v1", api_key_hint: "", priority: 0, weight: 1, enabled: true, purposes: ["image"],
    proxy: null, image_jobs_enabled: true, image_streaming_enabled: false, image_jobs_endpoint: "generations",
    image_jobs_endpoint_lock: false, image_jobs_base_url: "", image_edit_input_transport: "url", image_concurrency: 1,
    responses_supported: null, vision_supported: null, agent_api: "openai-responses", agent_context_window: 128000,
    agent_max_output_tokens: 16384, agent_reasoning_supported: true, image_generations_supported: true, image_responses_supported: null,
  };
  await page.route("**/api/admin/**", async (route) => {
    if (route.request().method() !== "GET") writes++;
    const path = new URL(route.request().url()).pathname;
    const body = path === "/api/admin/providers" ? {
      source: "db", proxies: [], items: [
        { ...base, name: "Verified fixture", base_url: "https://flux.infpro.me/v1", svg_author_recommendation: { model: "gpt-6.1-sol", tested_at: "2026-11-05", provider_base_url: "https://flux.infpro.me/v1", access_guaranteed: false, selection_required: true } },
        { ...base, name: "Unverified fixture", svg_author_recommendation: null },
      ],
    } : { items: [] };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/admin");
  if ((page.viewportSize()?.width ?? 0) < 768) await page.getByRole("combobox", { name: "管理后台页面" }).selectOption("providers");
  else await page.getByRole("navigation", { name: "管理后台菜单" }).getByRole("button", { name: "供应商", exact: true }).click();
  const advice = page.getByText(/SVG 作者参考：/);
  await expect(advice).toHaveCount(1);
  await expect(advice).toContainText("gpt-6.1-sol");
  await expect(advice).toContainText("https://flux.infpro.me/v1");
  await expect(advice).toContainText("不会修改配置");
  await expect(page.getByText("Unverified fixture", { exact: true }).last()).toBeVisible();
  await advice.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("provider-svg-advice.png") });
  expect(writes).toBe(0);
});
