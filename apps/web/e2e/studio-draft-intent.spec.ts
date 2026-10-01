import { expect, test } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";

test("studio draft and open editor survive desktop-mobile remounts without submitting", async ({ page }) => {
  await installAgentFixture(page);
  await page.route(/\/api\/conversations(?:\?.*)?$/u, (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  const submissions: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/(?:.*\/messages|generations)(?:\?|$)/u.test(request.url())) {
      submissions.push(request.url());
    }
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await page.locator("[data-studio-preset]").first().click();
  const input = page.locator("textarea:visible").first();
  await expect(input).toHaveValue(/雨夜东京街角/u);
  await expect(input).toBeFocused();
  const originalDraft = await input.inputValue();

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.locator("[data-topbar-sentinel]")).toHaveCount(1);
  await expect(input).toHaveValue(originalDraft);
  await expect(input).toBeFocused();
  await input.fill(`${originalDraft}，保留切屏后的修改`);

  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator("[data-topbar-sentinel]")).toHaveCount(0);
  await expect(input).toHaveValue(`${originalDraft}，保留切屏后的修改`);
  await expect(input).toBeFocused();
  expect(submissions).toEqual([]);
});
