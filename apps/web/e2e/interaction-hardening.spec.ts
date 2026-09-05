import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture, openAgent } from "./agent-fixture";

async function openPalette(page: Page) {
  await expect.poll(async () => {
    await page.evaluate(() => window.dispatchEvent(new CustomEvent("lumen:command-palette-open")));
    return page.getByRole("dialog", { name: "命令面板" }).count();
  }).toBe(1);
  const input = page.getByRole("combobox", { name: "搜索命令或页面" });
  await expect(input).toBeFocused();
  return input;
}

async function expectActiveVisible(page: Page) {
  await expect.poll(async () => page.getByRole("combobox").evaluate((input) => {
    const id = input.getAttribute("aria-activedescendant");
    const option = id ? document.getElementById(id) : null;
    const list = document.getElementById(input.getAttribute("aria-controls") ?? "");
    if (!option || !list) return false;
    const bounds = option.getBoundingClientRect();
    const viewport = list.getBoundingClientRect();
    return bounds.top >= viewport.top - 1 && bounds.bottom <= viewport.bottom + 1;
  })).toBe(true);
  await expect(page.getByRole("combobox")).toBeFocused();
}

test("interaction: palette ignores IME and repeat, then closes and restores its original focus", async ({ page }) => {
  await installAgentFixture(page);
  await openAgent(page);
  const original = page.getByTestId("agent-composer").locator("textarea").first();
  await original.focus();
  for (const flags of [{ isComposing: true }, { repeat: true }]) {
    await original.dispatchEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, ...flags });
    await expect(page.getByRole("dialog", { name: "命令面板" })).toHaveCount(0);
  }
  const input = await openPalette(page);
  for (const flags of [{ isComposing: true }, { repeat: true }]) {
    await input.dispatchEvent("keydown", { key: "Escape", bubbles: true, ...flags });
    await expect(input).toBeVisible();
    await input.dispatchEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, ...flags });
    await expect(input).toBeVisible();
  }
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "命令面板" })).toHaveCount(0);
  await expect(original).toBeFocused();
});

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test(`interaction: mobile palette exit restores focus without stealing it on reopen or navigation (${reducedMotion})`, async ({ page }) => {
    test.skip((page.viewportSize()?.width ?? 1440) >= 768, "mobile exit lifecycle");
    await installAgentFixture(page);
    await page.emulateMedia({ reducedMotion });
    await openAgent(page);
    const original = page.getByRole("textbox", { name: "发送给 Agent" });
    await original.focus();
    await openPalette(page);
    await page.getByRole("button", { name: "关闭命令面板" }).click();
    await expect(page.getByRole("dialog", { name: "命令面板" })).toHaveCount(0);
    await expect(original).toBeFocused();
    await openPalette(page);
    await page.keyboard.press("Escape");
    const input = await openPalette(page);
    await expect(input).toBeFocused();
    await input.fill("提示词设置");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/settings\/prompts$/u);
    await expect(page.getByRole("dialog", { name: "命令面板" })).toHaveCount(0);
    await expect(original).toHaveCount(0);
    expect(await page.evaluate(() => document.activeElement?.closest('[inert]') !== null)).toBe(false);
  });
}

test("interaction: Tab never enters palette options or disabled/hidden/negative candidates", async ({ page }) => {
  await installAgentFixture(page);
  await openAgent(page);
  const input = await openPalette(page);
  const dialog = page.getByRole("dialog", { name: "命令面板" });
  await dialog.evaluate((root) => {
    const candidates = document.createElement("div");
    candidates.dataset.interactionCandidates = "true";
    for (const index of [-1, -2, -20]) {
      const button = document.createElement("button");
      button.tabIndex = index;
      button.textContent = `negative ${index}`;
      candidates.append(button);
    }
    const disabled = document.createElement("button");
    disabled.disabled = true;
    disabled.tabIndex = 0;
    candidates.append(disabled);
    const fieldset = document.createElement("fieldset");
    fieldset.disabled = true;
    const fieldButton = document.createElement("button");
    fieldButton.tabIndex = 0;
    fieldset.append(fieldButton);
    candidates.append(fieldset);
    for (const kind of ["hidden", "inert", "visibility", "display"]) {
      const wrapper = document.createElement("div");
      if (kind === "hidden") wrapper.hidden = true;
      if (kind === "inert") wrapper.inert = true;
      if (kind === "visibility") wrapper.style.visibility = "hidden";
      if (kind === "display") wrapper.style.display = "none";
      const button = document.createElement("button");
      button.tabIndex = 0;
      wrapper.append(button);
      candidates.append(wrapper);
    }
    root.append(candidates);
  });
  await input.focus();
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press(i % 3 === 0 ? "Shift+Tab" : "Tab");
    await expect.poll(() => dialog.evaluate((root) =>
      root.contains(document.activeElement) &&
      !document.activeElement?.closest('[role="option"], [data-interaction-candidates]'),
    )).toBe(true);
  }
  const close = page.getByRole("button", { name: "关闭命令面板" });
  await close.focus();
  await page.keyboard.press("Tab");
  await expect.poll(() => dialog.evaluate((root) =>
    root.contains(document.activeElement) && !document.activeElement?.closest('[role="option"]'),
  )).toBe(true);
});

test("interaction: active command stays visible through filtering, resizing and presentation remount", async ({ page }) => {
  await installAgentFixture(page);
  await openAgent(page);
  let input = await openPalette(page);
  const count = await page.getByRole("option").count();
  for (let i = 0; i < count - 1; i++) {
    await page.keyboard.press("ArrowDown");
    await expectActiveVisible(page);
  }
  await input.fill("设置");
  await expectActiveVisible(page);
  await input.fill("");
  await page.keyboard.press("ArrowUp");
  await expectActiveVisible(page);
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: (viewport?.width ?? 375) < 768 ? 1024 : 375, height: 700 });
  input = page.getByRole("combobox", { name: "搜索命令或页面" });
  await expect(input).toBeFocused();
  await expectActiveVisible(page);
  await page.setViewportSize({ width: 375, height: 520 });
  await expectActiveVisible(page);
  await input.fill("no-matching-interaction-command");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(input).not.toHaveAttribute("aria-activedescendant", /.+/);
});

test("interaction: narrow desktop navigation keeps controls separate and skip-main preserves scroll ownership", async ({ page }, testInfo) => {
  test.skip((page.viewportSize()?.width ?? 0) < 768, "desktop navigation contract");
  await installAgentFixture(page);
  await openAgent(page);
  for (const width of [768, 820, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const nav = page.getByTestId("desktop-primary-nav");
    const actions = page.getByTestId("desktop-global-actions");
    await expect(nav).toBeVisible();
    const navBox = await nav.boundingBox();
    const actionsBox = await actions.boundingBox();
    expect(navBox).not.toBeNull();
    expect(actionsBox).not.toBeNull();
    expect((navBox?.x ?? 0) + (navBox?.width ?? 0)).toBeLessThanOrEqual((actionsBox?.x ?? 0) + 1);
    await nav.getByRole("link").last().focus();
    const lastBox = await nav.getByRole("link").last().boundingBox();
    expect((lastBox?.x ?? 0) + (lastBox?.width ?? 0)).toBeLessThanOrEqual((actionsBox?.x ?? 0) + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    await testInfo.attach(`interaction-navigation-${width}`, { body: await page.screenshot(), contentType: "image/png" });
  }
  const skip = page.getByRole("link", { name: "跳到工作区" });
  await skip.focus();
  await skip.press("Enter");
  await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).toBe("MAIN");
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});
