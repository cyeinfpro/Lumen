import { expect, test } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

test("2.6 markup keeps original orientation, a separate PNG layer, and modal state", async ({ page }, testInfo) => {
  await installAgentFixture(page);
  let uploaded = 0;
  let markupUploads = 0;
  let source = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64");
  let markupSize: number[] = [];
  let message: Record<string, unknown> | null = null;
  await page.route("**/api/images/*/binary", (route) => route.fulfill({ status: 200, contentType: "image/jpeg", body: source }));
  await page.route("**/api/images/upload", async (route) => {
    const raw = route.request().postDataBuffer()!;
    const markup = raw.includes(Buffer.from("image_markup"));
    uploaded += 1;
    if (markup) {
      markupUploads += 1;
      const start = raw.indexOf(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
      expect(start).toBeGreaterThan(-1);
      markupSize = [raw.readUInt32BE(start + 16), raw.readUInt32BE(start + 20)];
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      id: markup ? "markup-layer" : "original-reference", width: 40, height: 80,
      mime: "image/png", url: "/api/images/original-reference/binary",
      display_url: "/api/images/original-reference/binary", preview_url: "/api/images/original-reference/binary",
    }) });
  });
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    message = route.request().postDataJSON();
    return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "fixture captured only" }) });
  });
  const historyReady = page.waitForResponse((response) => response.request().method() === "GET" &&
    new URL(response.url()).pathname === "/api/conversations/studio-conversation-1/messages" && response.ok());
  await page.goto("/?conversationId=studio-conversation-1");
  await historyReady;
  const jpeg = await page.evaluate(() => {
    const c = document.createElement("canvas"); c.width = 80; c.height = 40;
    const ctx = c.getContext("2d")!; ctx.fillStyle = "white"; ctx.fillRect(0, 0, 80, 40);
    return c.toDataURL("image/jpeg").split(",")[1];
  });
  const rawJpeg = Buffer.from(jpeg, "base64");
  // EXIF orientation 6: browser displays the 80 x 40 source as 40 x 80.
  const exif = Buffer.from([255,225,0,34,69,120,105,102,0,0,73,73,42,0,8,0,0,0,1,0,18,1,3,0,1,0,0,0,6,0,0,0,0,0,0,0]);
  source = Buffer.concat([rawJpeg.subarray(0, 2), exif, rawJpeg.subarray(2)]);
  if ((page.viewportSize()?.width ?? 0) >= 768) await page.getByRole("button", { name: "展开输入框" }).click();
  await page.getByRole("textbox", { name: "输入提示词" }).fill("Move the circled object to the left");
  await page.getByRole("tablist", { name: "模式", exact: true }).getByRole("tab", { name: "生图", exact: true }).click();
  await page.locator('input[type="file"][accept="image/*"]').last().setInputFiles({ name: "original.jpg", mimeType: "image/jpeg", buffer: source });
  await expect.poll(() => uploaded).toBe(1);
  await page.getByRole("button", { name: "执行设置", exact: true }).click();
  const settings = page.getByRole("dialog", { name: /^(高级)?执行设置$/ });
  await settings.getByRole("button", { name: "快速预设", exact: true }).click();
  await expect(settings.getByRole("combobox", { name: "生图模型" })).toHaveValue("gpt-image-2.6-flare");
  await settings.getByRole("button", { name: "添加标注", exact: true }).click();
  const canvasDialog = page.getByRole("dialog", { name: "图像标注画布", exact: true });
  await expect(canvasDialog).toBeVisible();
  await canvasDialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(canvasDialog).toHaveCount(0);
  expect(markupUploads).toBe(0);
  await settings.getByRole("button", { name: "添加标注", exact: true }).click();
  const canvas = canvasDialog.locator("[data-mask-canvas-stage] canvas").first();
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width * .3, box!.y + box!.height * .3);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width * .6, box!.y + box!.height * .6, { steps: 5 });
  await page.mouse.up();
  await expect(canvasDialog).toBeVisible();
  await testInfo.attach("markup-editor", { body: await page.screenshot({ path: testInfo.outputPath("markup-editor.png") }), contentType: "image/png" });
  await canvasDialog.getByRole("button", { name: "确认", exact: true }).dblclick();
  await expect.poll(() => markupUploads).toBe(1);
  await expect(canvasDialog).toHaveCount(0);
  expect(markupSize).toEqual([40, 80]);
  await expect(settings.getByRole("button", { name: "清除标注", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => message).not.toBeNull();
  expect(message).toMatchObject({ markup_image_id: "markup-layer", attachment_image_ids: ["original-reference"],
    image_params: { model: "gpt-image-2.6-flare", complexity: "low" } });
  expect(message).not.toHaveProperty("mask_image_id");
});
