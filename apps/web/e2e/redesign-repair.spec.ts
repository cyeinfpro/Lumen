import { expect, test, type Page } from "@playwright/test";

import { installAgentFixture } from "./agent-fixture";

const NOW = "2026-08-20T08:00:00Z";

async function installLibraryFixture(page: Page) {
  await installAgentFixture(page);
  await page.route("**/api/workflows/apparel-model-library?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: "preset:model-1",
            source: "preset",
            visibility_scope: "global_preset",
            title: "商拍模特",
            age_segment: "young_adult",
            gender: "female",
            appearance_direction: "east_asian",
            style_tags: ["清冷高级"],
            image_url: "/inspiration/editorial-fashion-portrait.webp",
            display_url: "/inspiration/editorial-fashion-portrait.webp",
            thumb_url: "/inspiration/editorial-fashion-portrait.webp",
            image_id: null,
            usage_count: 3,
            created_at: NOW,
          },
        ],
        sync: {
          last_success_at: NOW,
          last_error: null,
          can_sync: false,
          github_contents_url: null,
        },
      }),
    }),
  );
}

async function installRuntimeFailureFixture(
  page: Page,
  status: "degraded" | "unauthorized",
) {
  await page.context().addCookies([
    {
      name: "lumen_runtime_defaults_v1",
      value: encodeURIComponent(
        JSON.stringify({
          canvas_enabled: true,
          agent_enabled: true,
          nav_visibility: {
            studio: true,
            agent: true,
            video: true,
            projects: true,
            assets: true,
          },
        }),
      ),
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  let authCalls = 0;
  await page.route("**/events?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: "event: heartbeat\ndata: {\"schema_version\":1}\n\n",
    }),
  );
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      authCalls += 1;
      if (status === "unauthorized") {
        return route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({
            detail: { error: { code: "unauthorized", message: "unauthorized" } },
          }),
        });
      }
      return route.abort("connectionfailed");
    }
    if (path === "/api/events") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "event: heartbeat\ndata: {\"schema_version\":1}\n\n",
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
  return { authCalls: () => authCalls };
}

async function expectFocusContained(page: Page, dialogSelector: string) {
  await expect
    .poll(() =>
      page.locator(dialogSelector).evaluate((dialog) =>
        dialog.contains(document.activeElement),
      ),
    )
    .toBe(true);
}

for (const runtimeStatus of ["degraded", "unauthorized"] as const) {
  test(`Video and Canvas expose ${runtimeStatus} recovery in custom mobile bars`, async ({
    page,
  }, testInfo) => {
    test.setTimeout(90_000);
    test.skip(
      !["phone-320-light", "phone-375-dark"].includes(testInfo.project.name),
      "mobile recovery route matrix",
    );
    const expectedLabel =
      runtimeStatus === "degraded"
        ? "会话验证暂不可用，重新验证会话"
        : "会话已失效，登录";
    for (const routePath of ["/video", "/projects/canvas/canvas-1"]) {
      const routePage = routePath === "/video" ? page : await page.context().newPage();
      await installRuntimeFailureFixture(routePage, runtimeStatus);
      await routePage.goto(routePath);
      const recovery = routePage.getByRole("button", { name: expectedLabel });
      await expect(recovery).toBeVisible();
      await expect(recovery).toBeEnabled();
      const recoveryBox = await recovery.boundingBox();
      expect(recoveryBox?.width ?? 0).toBeGreaterThanOrEqual(44);
      expect(recoveryBox?.height ?? 0).toBeGreaterThanOrEqual(44);
      await recovery.click();
      if (runtimeStatus === "degraded") {
        await expect(recovery).toBeFocused();
      } else {
        await expect(routePage).toHaveURL(/\/login/u);
      }
      if (routePage !== page) await routePage.close();
    }
  });
}

test("model-library upload dialog traps focus, closes from every path, and restores its trigger", async ({
  page,
}) => {
  await installLibraryFixture(page);
  await page.goto("/library");
  const upload = page.getByRole("button", { name: "上传", exact: true });
  await expect(upload).toBeVisible();
  await upload.click();

  const dialog = page.getByRole("dialog", { name: "上传到模特库" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("textbox", { name: "名称" })).toBeFocused();
  for (let index = 0; index < 14; index += 1) {
    await page.keyboard.press(index % 3 === 0 ? "Shift+Tab" : "Tab");
    await expectFocusContained(page, '[role="dialog"][aria-label="上传到模特库"]');
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(upload).toBeFocused();

  await upload.click();
  await expect(dialog).toBeVisible();
  await page
    .locator('[data-lumen-modal-layer] > div[aria-hidden="true"]')
    .click({ position: { x: 4, y: 4 } });
  await expect(dialog).toHaveCount(0);
  await expect(upload).toBeFocused();
});

test("mobile model filters announce state and keep keyboard focus inside the sheet", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "phone-375-dark",
    "dedicated real-touch mobile project",
  );
  await installLibraryFixture(page);
  await page.goto("/library");
  const filter = page.getByRole("button", { name: "筛选", exact: true });
  await filter.click();

  const sheet = page.getByRole("dialog", { name: "筛选" });
  await expect(sheet).toBeVisible();
  await expectFocusContained(page, '[role="dialog"][aria-labelledby]');
  const ageGroup = sheet.getByRole("group", { name: "年龄段" });
  await expect(ageGroup.getByRole("button", { name: "全部" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await ageGroup.getByRole("button", { name: "青年" }).click();
  await expect(ageGroup.getByRole("button", { name: "青年" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  for (let index = 0; index < 18; index += 1) {
    await page.keyboard.press(index % 4 === 0 ? "Shift+Tab" : "Tab");
    await expectFocusContained(page, '[role="dialog"][aria-labelledby]');
  }
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(filter).toBeFocused();
});

test("real touch long press exposes confirmed mobile asset deletion", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "phone-375-dark",
    "dedicated real-touch mobile project",
  );
  let deletedImageId: string | null = null;
  await installAgentFixture(page);
  await page.route("**/api/images/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deletedImageId = new URL(route.request().url()).pathname.split("/").at(-1) ?? null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
  await page.goto("/stream");
  const tile = page.getByRole("button", { name: "素材参考 1", exact: true });
  await expect(tile).toBeVisible();
  const box = await tile.boundingBox();
  expect(box).not.toBeNull();
  await tile.dispatchEvent("pointerdown", {
    pointerId: 1,
    pointerType: "touch",
    isPrimary: true,
    button: 0,
    clientX: (box?.x ?? 0) + 20,
    clientY: (box?.y ?? 0) + 20,
  });
  await page.waitForTimeout(460);
  await page.getByRole("button", { name: "删除图片" }).click();
  const confirmation = page.getByRole("dialog", { name: "删除这张图片？" });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "删除" }).click();
  await expect.poll(() => deletedImageId).toBe("feed-image-1");
});

test("project badges meet AA and progress rings render authoritative API values", async ({
  page,
}) => {
  await installAgentFixture(page, { canvasEnabled: true });
  await page.route("**/api/workflows?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: "project-0",
            conversation_id: null,
            type: "apparel_model_showcase",
            status: "draft",
            title: "准备项目",
            user_prompt: "",
            product_image_ids: [],
            current_step: "upload_product",
            quality_mode: "premium",
            metadata_jsonb: {},
            created_at: NOW,
            updated_at: NOW,
            output_count: 0,
            completion_percent: 0,
            next_action: "确认商品约束",
          },
          {
            id: "project-50",
            conversation_id: null,
            type: "apparel_model_showcase",
            status: "running",
            title: "生成项目",
            user_prompt: "",
            product_image_ids: [],
            current_step: "showcase_generation",
            quality_mode: "premium",
            metadata_jsonb: {},
            created_at: NOW,
            updated_at: NOW,
            output_count: 2,
            completion_percent: 50,
            next_action: "查看质检",
          },
          {
            id: "project-100",
            conversation_id: null,
            type: "poster_design",
            status: "completed",
            title: "交付项目",
            user_prompt: "",
            product_image_ids: [],
            current_step: "delivery",
            quality_mode: "premium",
            metadata_jsonb: {},
            created_at: NOW,
            updated_at: NOW,
            output_count: 4,
            completion_percent: 100,
            next_action: "查看交付",
          },
        ],
        next_cursor: null,
      }),
    }),
  );
  await page.goto("/projects");
  for (const value of [0, 50, 100]) {
    await expect(page.getByRole("progressbar", { name: `项目完成度 ${value}%` }))
      .toHaveAttribute("aria-valuenow", String(value));
  }

  for (const label of ["正式", "自由", "运行中"]) {
    const contrast = await page.getByText(label, { exact: true }).first().evaluate((node) => {
      const parse = (value: string) => {
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        if (!context) return { r: 0, g: 0, b: 0, a: 0 };
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = value;
        context.fillRect(0, 0, 1, 1);
        const [r, g, b, alpha] = context.getImageData(0, 0, 1, 1).data;
        return { r, g, b, a: alpha / 255 };
      };
      const blend = (
        top: { r: number; g: number; b: number; a: number },
        bottom: { r: number; g: number; b: number; a: number },
      ) => {
        const alpha = top.a + bottom.a * (1 - top.a);
        if (alpha <= 0) return { r: 0, g: 0, b: 0, a: 0 };
        return {
          r: (top.r * top.a + bottom.r * bottom.a * (1 - top.a)) / alpha,
          g: (top.g * top.a + bottom.g * bottom.a * (1 - top.a)) / alpha,
          b: (top.b * top.a + bottom.b * bottom.a * (1 - top.a)) / alpha,
          a: alpha,
        };
      };
      let background = { r: 0, g: 0, b: 0, a: 0 };
      let current: Element | null = node;
      while (current && background.a < 1) {
        const candidate = parse(getComputedStyle(current).backgroundColor);
        if (candidate.a > 0) background = blend(background, candidate);
        current = current.parentElement;
      }
      const computedColor = getComputedStyle(node).color;
      const computedBackground = getComputedStyle(node).backgroundColor;
      const foreground = parse(computedColor);
      const luminance = (color: { r: number; g: number; b: number }) => {
        const channels = [color.r, color.g, color.b].map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const foregroundLuminance = luminance(foreground);
      const backgroundLuminance = luminance(background);
      return {
        ratio:
          (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
          (Math.min(foregroundLuminance, backgroundLuminance) + 0.05),
        color: computedColor,
        background: computedBackground,
        effectiveBackground: background,
        className: (node as HTMLElement).className,
      };
    });
    expect(
      contrast.ratio,
      `${label} contrast: ${JSON.stringify(contrast)}`,
    ).toBeGreaterThanOrEqual(4.5);
  }
});
