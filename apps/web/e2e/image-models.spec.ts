import { expect, test } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

test("image model menu filters quality and submits the selected model", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  let submitted: Record<string, unknown> | null = null;
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "fixture captured request" }) });
  });
  const historyReady = page.waitForResponse((response) =>
    response.request().method() === "GET" &&
    new URL(response.url()).pathname === "/api/conversations/studio-conversation-1/messages" && response.ok(),
  );
  await page.goto("/?conversationId=studio-conversation-1");
  await historyReady;
  if ((page.viewportSize()?.width ?? 0) >= 768) {
    await page.getByRole("button", { name: "展开输入框" }).click();
  }
  await page.getByRole("textbox", { name: "输入提示词" }).fill("A paper lantern beside a blue lake");
  await page.getByRole("tablist", { name: "模式", exact: true }).getByRole("tab", { name: "生图", exact: true }).click();
  await page.getByRole("button", { name: "执行设置", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: /^(高级)?执行设置$/ });
  const models = dialog.getByRole("combobox", { name: "生图模型" });
  const quality = dialog.getByRole("combobox", { name: "质量", exact: true });
  await expect(models.locator("option")).toHaveCount(3);
  await expect(models).toHaveValue("gpt-image-2");
  await expect(quality.locator("option")).toHaveCount(3);
  for (const model of ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"]) {
    await models.selectOption(model);
    await expect(quality.locator("option")).toHaveCount(5);
    await quality.selectOption("max");
    await expect(quality).toHaveValue("max");
  }
  await models.selectOption("gpt-image-2");
  await expect(quality).toHaveValue("high");
  await models.selectOption("gpt-image-2.5-sunburst");
  await quality.selectOption("xhigh");
  await testInfo.attach("image-model-menu", { body: await page.screenshot({ path: testInfo.outputPath("image-model-menu.png") }), contentType: "image/png" });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted).toMatchObject({
    intent: "text_to_image",
    image_params: { model: "gpt-image-2.5-sunburst", render_quality: "xhigh" },
  });
});
