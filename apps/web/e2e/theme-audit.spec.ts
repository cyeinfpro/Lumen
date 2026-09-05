import { expect, test } from "@playwright/test";
import { compileThemeCss } from "../__tests__/helpers/theme-css.mjs";
import { renderThemeButtons } from "../__tests__/helpers/theme-buttons.mjs";

const webRoot = process.cwd();
const css = compileThemeCss(webRoot);
const buttons = renderThemeButtons(webRoot);

// Isolate computed theme/material behavior with production CSS and real Button markup.
// No server, provider, authentication, or network requests are needed for this fixture.
test.beforeEach(async ({ page }) => {
  await page.setContent(`<!doctype html><html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
  </head><body>
    <header class="surface-glass-v2">Lumen</header>
    <main>${buttons}
      <div class="lumen-md"><a id="markdown" href="#target">Reference</a></div>
      <article id="card" class="surface-card surface-card-hover">Content</article>
      <article id="card-v2" class="surface-card-v2">Media item</article>
      <aside id="panel" class="surface-panel">Overlay</aside>
    </main>
  </body></html>`);
  await page.addStyleTag({ content: css });
});

test("theme audit: readable links and solid actions follow explicit and system themes", async ({ page }, testInfo) => {
  for (const system of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: system });
    for (const theme of ["", "theme-light", "theme-dark", "dark"]) {
      await page.locator("html").evaluate((html, value) => { html.setAttribute("class", value); }, theme);
      const light = theme === "theme-light" || (!theme && system === "light");
      const color = light ? "rgb(7, 93, 168)" : "rgb(139, 197, 255)";
      for (const surface of ["--bg-0", "--bg-1", "--bg-2", "--bg-3", "--surface-overlay"]) {
        await page.locator("main").evaluate((main, token) => {
          main.style.backgroundColor = `var(${token})`;
        }, surface);
        await expect(page.locator("#link")).toHaveCSS("color", color);
        await expect(page.locator("#markdown")).toHaveCSS("color", color);
        await page.locator("#link").hover();
        await expect(page.locator("#link")).toHaveCSS("opacity", "1");
        await expect(page.locator("#link")).toHaveCSS("color", color);
      }
      await expect(page.locator("header")).toHaveCSS("background-color", light ? "rgb(255, 255, 255)" : "rgb(13, 14, 17)");
      await expect(page.locator("header")).toHaveCSS("backdrop-filter", "none");
      await expect(page.locator("#primary")).toHaveCSS("background-image", "none");
      await expect(page.locator("#primary")).toHaveCSS("box-shadow", "none");
      await page.locator("#primary").hover();
      await expect(page.locator("#primary")).toHaveCSS("box-shadow", "none");
      await expect(page.locator("#primary")).toHaveCSS("filter", "none");
    }
  }
  await expect(page.locator("#disabled")).toBeDisabled();
  await expect(page.locator("#loading")).toBeDisabled();
  await expect(page.locator("#loading")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#loading svg")).toHaveCount(1);
  await page.locator("#link").focus();
  await expect(page.locator("#link")).toHaveCSS("outline-style", "solid");
  await expect(page.locator("#link")).toHaveCSS("outline-width", "2px");
  await page.locator("html").evaluate((html) => { html.removeAttribute("class"); });
  const screenshotScheme = testInfo.project.use.colorScheme ?? "light";
  await page.emulateMedia({ colorScheme: screenshotScheme });
  await expect(page.locator("#card")).toHaveCSS("background-color", screenshotScheme === "light" ? "rgb(255, 255, 255)" : "rgb(13, 14, 17)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  if (testInfo.project.use.hasTouch) {
    for (const id of ["primary", "secondary", "glass", "link"]) {
      const bounds = await page.locator(`#${id}`).boundingBox();
      expect(bounds?.width).toBeGreaterThanOrEqual(44);
      expect(bounds?.height).toBeGreaterThanOrEqual(44);
    }
  }
  const screenshot = testInfo.outputPath("theme-materials.png");
  await page.screenshot({ path: screenshot, animations: "disabled" });
  await testInfo.attach("theme-materials", { path: screenshot, contentType: "image/png" });
});

test("theme audit: card hover is stable and reduced preferences keep surfaces readable", async ({ page }) => {
  for (const id of ["card", "card-v2"]) {
    const card = page.locator(`#${id}`);
    const before = await card.boundingBox();
    const shadow = await card.evaluate((element) => getComputedStyle(element).boxShadow);
    await card.hover();
    await expect(card).toHaveCSS("transform", "none");
    await expect(card).toHaveCSS("box-shadow", shadow);
    expect(await card.boundingBox()).toEqual(before);
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator("#primary").hover();
  await page.mouse.down();
  await expect(page.locator("#primary")).toHaveCSS("transform", "none");
  await page.mouse.up();
  const session = await page.context().newCDPSession(page);
  await session.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-transparency", value: "reduce" }],
  });
  for (const selector of ["#glass", "#panel", "header"]) {
    const surface = page.locator(selector);
    await expect(surface).toHaveCSS("backdrop-filter", "none");
    const opacity = await surface.evaluate((element) => {
      const probe = document.createElement("span");
      probe.style.backgroundColor = "var(--bg-1)";
      element.append(probe);
      const expected = getComputedStyle(probe).backgroundColor;
      const actual = getComputedStyle(element).backgroundColor;
      probe.remove();
      return { expected, actual };
    });
    expect(opacity.actual).toEqual(opacity.expected);
  }
  await session.detach();
});
