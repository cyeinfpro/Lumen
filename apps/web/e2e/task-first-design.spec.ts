import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

async function recordLayout(page: Page, testInfo: TestInfo, name: string) {
  const overflow = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(overflow.document, `${name}: document must not scroll horizontally`).toBeLessThanOrEqual(overflow.viewport + 1);
  expect(overflow.body, `${name}: body must not scroll horizontally`).toBeLessThanOrEqual(overflow.viewport + 1);
  await testInfo.attach(name, { body: await page.screenshot({ fullPage: true, animations: "disabled" }), contentType: "image/png" });
}

function workflow(index: number) {
  return {
    id: `design-project-${index}`,
    type: "poster_design",
    title: `设计项目 ${index}`,
    status: index === 1 ? "completed" : "draft",
    current_step: "design",
    completion_percent: index === 1 ? 100 : 0,
    next_action: index === 1 ? "查看交付" : "继续编辑文案",
    output_count: index === 1 ? 3 : 0,
    created_at: "2026-09-20T08:00:00Z",
    updated_at: "2026-09-21T08:00:00Z",
    product_image_ids: [],
    product_images: [],
  };
}

test("studio uses compact editable presets and preserves deliberate submission", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  const submissions: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/(?:.*\/messages|generations)(?:\?|$)/u.test(request.url())) submissions.push(request.url());
  });
  await page.goto("/");
  const welcome = page.locator("[data-studio-welcome]");
  await expect(welcome).toBeVisible();
  await expect(welcome.getByRole("heading", { level: 1 })).toHaveText("今天想创作什么？");
  const presets = welcome.locator("[data-studio-preset]");
  await expect(presets).toHaveCount(4);
  const rects = await presets.evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, height: rect.height, width: rect.width };
  }));
  expect(Math.abs(rects[0].y - rects[1].y)).toBeLessThan(2);
  for (const rect of rects) {
    expect(rect.height).toBeGreaterThanOrEqual(44);
    expect(rect.width).toBeGreaterThanOrEqual(44);
  }
  if (page.viewportSize()!.width < 1024) expect(rects[2].y).toBeGreaterThan(rects[0].y);
  if (testInfo.project.name.includes("reduced")) {
    await expect.poll(() => welcome.evaluate((element) => getComputedStyle(element).opacity)).toBe("1");
    expect(await welcome.evaluate((element) => element.getAnimations().length)).toBe(0);
  }
  await recordLayout(page, testInfo, "studio-start");
  await presets.first().click();
  const input = page.locator("textarea:visible").first();
  await expect(input).toHaveValue(/雨夜东京街角/u);
  await expect(input).toBeFocused();
  expect(submissions).toEqual([]);
});

test("studio conversation starters are disclosed without submitting", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  const welcome = page.locator("[data-studio-welcome]");
  const disclosure = welcome.locator("details");
  await expect(disclosure).not.toHaveAttribute("open", "");
  const summary = disclosure.locator("summary");
  await summary.focus();
  await expect(summary).toBeFocused();
  await summary.press("Space");
  await expect(disclosure).toHaveAttribute("open", "");
  await summary.press("Enter");
  await expect(disclosure).not.toHaveAttribute("open", "");
  await summary.press("Space");
  await expect(disclosure).toHaveAttribute("open", "");
  await welcome.getByRole("button", { name: "分析这张图的构图和光影", exact: true }).click();
  await expect(page.locator("textarea:visible").first()).toHaveValue("分析这张图的构图和光影");
  await recordLayout(page, testInfo, "studio-chat-draft");
});

test("projects separate reusable resources and disclose workflow details", async ({ page }, testInfo) => {
  await installAgentFixture(page, { canvasEnabled: true });
  await page.route("**/api/workflows**", (route) => route.fulfill({
    json: { items: Array.from({ length: 5 }, (_, index) => workflow(index + 1)), next_cursor: null },
  }));
  await page.goto("/projects");
  const recent = page.getByRole("list", { name: "最近项目", exact: true });
  await expect(recent.locator(":scope > li")).toHaveCount(3);
  await expect(recent.getByRole("progressbar").first()).toHaveAttribute("aria-valuenow", "100");
  await page.getByRole("button", { name: "查看其余 2 个项目", exact: true }).click();
  await expect(recent.locator(":scope > li")).toHaveCount(5);
  await page.getByRole("button", { name: "收起最近项目", exact: true }).click();
  await expect(recent.locator(":scope > li")).toHaveCount(3);
  const cards = page.locator("[data-workflow-card]");
  await expect(cards).toHaveCount(4);
  await expect(cards.getByRole("heading", { name: "风格库", exact: true })).toHaveCount(0);
  const resources = page.getByRole("navigation", { name: "素材与预设" });
  await expect(resources.getByRole("link", { name: "风格库", exact: true })).toHaveAttribute("href", "/poster-styles");
  await expect(resources.getByRole("link", { name: "模特库", exact: true })).toHaveAttribute("href", "/library");
  const details = cards.first().locator("details");
  await expect(details).not.toHaveAttribute("open", "");
  await details.locator("summary").click();
  await expect(details.getByRole("list", { name: "工作流步骤" })).toBeVisible();
  await expect(cards.nth(1).locator("details")).not.toHaveAttribute("open", "");
  await details.locator("summary").click();
  await expect(page.getByText(/约\s*\d+-\d+\s*分钟/u)).toHaveCount(0);
  await recordLayout(page, testInfo, "project-workflows");
});

test("video keeps creation before a content-sized empty preview", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.route("**/api/videos/**", (route) => route.fulfill({
    json: new URL(route.request().url()).pathname.endsWith("/options") ? {
      enabled: true, default_action: "t2v", default_model: "design-model", actions: ["t2v"],
      models: [{ model: "design-model", actions: ["t2v"], resolutions: ["720p"], aspect_ratios: ["16:9"], durations_s: [5], generate_audio: true }],
      resolutions: ["720p"], aspect_ratios: ["16:9"], durations_s: [5], generate_audio: true, pricing: [], hold_estimates: {},
    } : { items: [], next_cursor: null },
  }));
  await page.goto("/video");
  const composer = page.locator("[data-video-composer]");
  const preview = page.locator('[data-video-preview="empty"]');
  await expect(composer).toBeVisible();
  await expect(preview).toBeVisible();
  const composerBox = await composer.boundingBox();
  const previewBox = await preview.boundingBox();
  expect(previewBox!.y).toBeGreaterThanOrEqual(composerBox!.y + composerBox!.height - 1);
  expect(previewBox!.height).toBeLessThan(240);
  await expect(preview.locator("video")).toHaveCount(0);
  await expect(preview.getByRole("heading", { name: "成片预览" })).toBeVisible();
  await recordLayout(page, testInfo, "video-input-first");
  if (page.viewportSize()!.width < 1120) {
    const trigger = page.getByRole("button", { name: /^参数/u }).first();
    await trigger.focus();
    await trigger.press("Enter");
    const panel = page.getByRole("dialog", { name: "视频生成参数", exact: true });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("combobox", { name: "模型", exact: true })).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }
});

test("asset search keeps keyboard focus and does not hide toolbar actions", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  await page.goto("/stream");
  // This route imports its responsive shell client-side after document load.
  const toolbar = page.getByRole("region", { name: "图库工具栏", exact: true });
  await toolbar.waitFor({ state: "visible", timeout: 30_000 });
  const trigger = page.getByRole("button", { name: "搜索素材", exact: true });
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await trigger.press("Enter");
  const input = page.getByRole("textbox", { name: "搜索已加载作品", exact: true });
  await expect(input).toBeFocused();
  await input.fill("测试关键词");
  await page.getByRole("button", { name: "清空", exact: true }).click();
  await expect(input).toHaveValue("");
  await expect(input).toBeFocused();
  await input.press("Escape");
  await expect(trigger).toBeFocused();
  await expect(page.getByRole("textbox", { name: "搜索已加载作品", exact: true })).toHaveCount(0);
  const filters = toolbar.getByRole("group", { name: "筛选", exact: true });
  await expect(filters).toHaveCount(0);
  const filterTrigger = toolbar.getByRole("button", { name: "筛选素材", exact: true });
  await filterTrigger.focus();
  await filterTrigger.press("Enter");
  await expect(filters).toBeVisible();
  await expect(filters.getByRole("button")).toHaveCount(10);
  await expect.poll(async () => toolbar.getByRole("button").evaluateAll((buttons) => buttons.flatMap((button) => {
    const rect = button.getBoundingClientRect();
    return rect.left < 0 || rect.right > document.documentElement.clientWidth + 1
      ? [{ name: button.getAttribute("aria-label") ?? button.textContent, left: rect.left, right: rect.right }]
      : [];
  })), { message: "Every exposed toolbar and filter action must fit the viewport" }).toEqual([]);
  await recordLayout(page, testInfo, "asset-toolbar");
  await filterTrigger.press("Enter");
  await expect(filters).toHaveCount(0);
  await expect(filterTrigger).toBeFocused();
});
