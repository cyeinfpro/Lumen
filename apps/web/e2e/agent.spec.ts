import { expect, test } from "@playwright/test";
import {
  assertImagePixels,
  installAgentFixture,
  openAgent,
} from "./agent-fixture";

const TOOL_DETAILS_NOW = "2026-08-20T08:00:00Z";

function toolDetailsSnapshot() {
  const toolBase = {
    agent_run_id: "run-1",
    status: "succeeded",
    generation_ids: [] as string[],
    error_code: null,
    started_at: TOOL_DETAILS_NOW,
    finished_at: TOOL_DETAILS_NOW,
    created_at: TOOL_DETAILS_NOW,
    updated_at: TOOL_DETAILS_NOW,
  };
  const run = {
    id: "run-1",
    agent_session_id: "session-1",
    user_message_id: "user-1",
    assistant_message_id: "assistant-1",
    status: "succeeded",
    execution_epoch: 1,
    last_event_seq: 8,
    idempotency_key: "tool-details-message",
    model: "fixture-model",
    reasoning_effort: null,
    turn_count: 1,
    tool_call_count: 3,
    usage: {},
    error_code: null,
    error_message: null,
    continuable: false,
    started_at: TOOL_DETAILS_NOW,
    finished_at: TOOL_DETAILS_NOW,
    cancel_requested_at: null,
    created_at: TOOL_DETAILS_NOW,
    updated_at: TOOL_DETAILS_NOW,
    references: [],
    tool_calls: [
      {
        ...toolBase,
        id: "tool-web",
        ordinal: 0,
        name: "lumen_web_search",
        mode: "web_search",
        generation_count: 0,
        details: {
          kind: "web_search",
          query: "2026 极简美妆视觉趋势",
          result_snippets: ["行业报告 - 极简排版与高对比产品摄影持续增长"],
        },
        duration_ms: 920,
      },
      {
        ...toolBase,
        id: "tool-file",
        ordinal: 1,
        name: "lumen_search_files",
        mode: "file_search",
        generation_count: 0,
        details: {
          kind: "file_search",
          file_names: ["brief.md"],
          query: "品牌色",
          line_start: null,
          line_end: null,
          result_snippets: ["brief.md:4 - 品牌色使用暖金与炭黑"],
        },
        duration_ms: 18,
      },
      {
        ...toolBase,
        id: "tool-image",
        ordinal: 2,
        name: "lumen_create_image",
        mode: "image_to_image",
        generation_count: 2,
        details: {
          kind: "image",
          prompt: "暖金与炭黑的极简美妆产品海报",
          reference_count: 1,
          count: 2,
          aspect_ratio: "4:5",
          quality: "2k",
          render_quality: "high",
          background: "opaque",
          output_format: "webp",
        },
        duration_ms: 2_400,
      },
    ],
  };
  return {
    items: [
      {
        id: "user-1",
        conversation_id: "conversation-1",
        role: "user",
        content: { source: "agent", text: "调研并生成视觉方案" },
        intent: "agent",
        status: null,
        parent_message_id: null,
        created_at: TOOL_DETAILS_NOW,
      },
      {
        id: "assistant-1",
        conversation_id: "conversation-1",
        role: "assistant",
        content: {
          source: "agent",
          agent_run_id: "run-1",
          text: "已完成调研与视觉方案。",
        },
        intent: "agent",
        status: "succeeded",
        parent_message_id: "user-1",
        created_at: TOOL_DETAILS_NOW,
      },
    ],
    runs: [run],
    next_cursor: null,
    generations: [],
    completions: [],
    images: [],
  };
}

test("mounted Agent workspace closes its replaced EventSource", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const lifecycle = { created: [] as string[], closed: [] as string[] };
    Object.defineProperty(window, "__agentEventSourceLifecycle", {
      configurable: true,
      value: lifecycle,
    });
    class ControlledEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = true;
      readyState = ControlledEventSource.OPEN;
      onerror: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onopen: ((event: Event) => void) | null = null;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        lifecycle.created.push(this.url);
        queueMicrotask(() => {
          if (this.readyState !== ControlledEventSource.OPEN) return;
          const event = new Event("open");
          this.onopen?.(event);
          this.dispatchEvent(event);
        });
      }

      close() {
        if (this.readyState === ControlledEventSource.CLOSED) return;
        this.readyState = ControlledEventSource.CLOSED;
        lifecycle.closed.push(this.url);
      }
    }
    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: ControlledEventSource,
    });
  });
  await installAgentFixture(page);
  await openAgent(page);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const lifecycle = (
          window as unknown as Window & {
            __agentEventSourceLifecycle: {
              created: string[];
              closed: string[];
            };
          }
        ).__agentEventSourceLifecycle;
        return lifecycle.created.some((url) =>
          decodeURIComponent(url).includes("agent:session-1"),
        );
      }),
    )
    .toBe(true);

  await page
    .getByRole("link", { name: "创作", exact: true })
    .or(page.getByRole("button", { name: "创作", exact: true }))
    .first()
    .click();
  await expect.poll(() => page.evaluate(() => window.location.pathname)).toBe("/");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as Window & {
              __agentEventSourceLifecycle: { closed: string[] };
            }
          ).__agentEventSourceLifecycle.closed.length,
      ),
    )
    .toBeGreaterThan(0);
});

test("mounted Agent coordinates 60 task streams across rapid session and visibility changes", async ({
  page,
}) => {
  test.skip(
    (page.viewportSize()?.width ?? 0) < 1_200,
    "persistent desktop session list required",
  );
  await page.addInitScript(() => {
    let visibility: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
    const lifecycle = { created: [] as string[], closed: [] as string[] };
    Object.defineProperties(window, {
      __agentEventSourceLifecycle: {
        configurable: true,
        value: lifecycle,
      },
      __setAgentVisibility: {
        configurable: true,
        value: (next: DocumentVisibilityState) => {
          visibility = next;
          document.dispatchEvent(new Event("visibilitychange"));
        },
      },
    });
    class ControlledEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = true;
      readyState = ControlledEventSource.OPEN;
      onerror: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onopen: ((event: Event) => void) | null = null;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        lifecycle.created.push(this.url);
        queueMicrotask(() => {
          if (this.readyState !== ControlledEventSource.OPEN) return;
          const event = new Event("open");
          this.onopen?.(event);
          this.dispatchEvent(event);
        });
      }

      close() {
        if (this.readyState === ControlledEventSource.CLOSED) return;
        this.readyState = ControlledEventSource.CLOSED;
        lifecycle.closed.push(this.url);
      }
    }
    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: ControlledEventSource,
    });
  });
  const fixture = await installAgentFixture(page, {
    mode: "active-image",
    sessionCount: 3,
    generationCount: 60,
  });
  await openAgent(page);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const created = (
          window as unknown as Window & {
            __agentEventSourceLifecycle: { created: string[] };
          }
        ).__agentEventSourceLifecycle.created;
        return created.some((raw) => {
          const url = decodeURIComponent(raw);
          return (
            url.includes("agent:session-1") &&
            (url.match(/task:generation-/gu) ?? []).length === 60
          );
        });
      }),
    )
    .toBe(true);

  const selectSession = (sessionId: string) =>
    page.locator(`[data-agent-session-id="${sessionId}"] > div > button`).first();
  await selectSession("session-2").click();
  await selectSession("session-3").click();
  await selectSession("session-1").click();
  await expect(page).toHaveURL(/session=session-1/u);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const lifecycle = (
          window as unknown as Window & {
            __agentEventSourceLifecycle: {
              created: string[];
              closed: string[];
            };
          }
        ).__agentEventSourceLifecycle;
        const replaced = lifecycle.created.filter((raw) =>
          /agent%3A(?:session-2|session-3)|agent:(?:session-2|session-3)/u.test(raw),
        );
        const agentSources = lifecycle.created.filter((raw) =>
          decodeURIComponent(raw).includes("channels=agent:"),
        );
        const latest = decodeURIComponent(agentSources.at(-1) ?? "");
        return (
          agentSources.some((raw) => lifecycle.closed.includes(raw)) &&
          latest.includes("agent:session-1") &&
          replaced.every((raw) => lifecycle.closed.includes(raw))
        );
      }),
    )
    .toBe(true);

  await page.evaluate(() => {
    (
      window as unknown as Window & {
        __setAgentVisibility: (value: DocumentVisibilityState) => void;
      }
    ).__setAgentVisibility("hidden");
    window.dispatchEvent(new Event("focus"));
  });
  const hiddenCalls = fixture.snapshotCalls;
  await page.waitForTimeout(500);
  expect(fixture.snapshotCalls - hiddenCalls).toBeLessThanOrEqual(2);
  await page.evaluate(() => {
    (
      window as unknown as Window & {
        __setAgentVisibility: (value: DocumentVisibilityState) => void;
      }
    ).__setAgentVisibility("visible");
    window.dispatchEvent(new Event("focus"));
  });
  await expect.poll(() => fixture.snapshotCalls).toBeGreaterThan(hiddenCalls);
  await page.waitForTimeout(500);
  expect(fixture.snapshotCalls - hiddenCalls).toBeLessThanOrEqual(8);
});

test("Agent shell keeps six mobile targets stable and content unobscured", async ({
  page,
}, testInfo) => {
  await installAgentFixture(page);
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await openAgent(page);
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  if ((viewport?.width ?? 0) < 768) {
    const tabs = page.locator('nav[aria-label="主导航"] li');
    await expect(tabs).toHaveCount(6);
    const boxes = await tabs.evaluateAll((items) =>
      items.map((item) => {
        const rect = item.getBoundingClientRect();
        const control = item.querySelector("button")?.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          width: control?.width ?? 0,
          height: control?.height ?? 0,
        };
      }),
    );
    for (let index = 0; index < boxes.length; index += 1) {
      expect(boxes[index].width).toBeGreaterThanOrEqual(44);
      expect(boxes[index].height).toBeGreaterThanOrEqual(44);
      if (index > 0)
        expect(boxes[index].left).toBeGreaterThanOrEqual(
          boxes[index - 1].right - 0.5,
        );
    }
    const contextStatus = page.locator(
      '[data-agent-mobile-context] [role="status"]',
    );
    await expect(contextStatus).toBeVisible();
    await expect(contextStatus).not.toHaveText("");
    if (testInfo.project.name.startsWith("phone-") &&
        !testInfo.project.name.includes("keyboard")) {
      expect(
        await page.evaluate(() => matchMedia("(hover: none)").matches),
      ).toBe(true);
    }
  } else {
    await expect(
      page
        .getByTestId("desktop-primary-nav")
        .getByText("Agent", { exact: true }),
    ).toBeVisible();
  }
  const scroll = page.getByTestId("agent-conversation-scroll");
  const scrollBox = await scroll.boundingBox();
  expect(scrollBox?.height ?? 0).toBeGreaterThan(120);
  const suggestionsBox = await page
    .getByTestId("agent-empty-suggestions")
    .boundingBox();
  const composerBox = await page.getByTestId("agent-composer").boundingBox();
  expect(suggestionsBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  if (suggestionsBox && composerBox) {
    expect(suggestionsBox.y + suggestionsBox.height).toBeLessThanOrEqual(
      composerBox.y + 1,
    );
  }
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);
  expect(consoleErrors).toEqual([]);
  await testInfo.attach("agent-shell", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
});

test("short-landscape Agent keeps direct composer tools reachable", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "phone-landscape-dark",
    "explicit short-landscape composer regression",
  );
  await installAgentFixture(page, { mode: "text" });
  await openAgent(page);

  const composer = page.getByTestId("agent-composer");
  const toolbar = page.getByTestId("agent-composer-toolbar");
  const webSearch = page.getByRole("button", { name: "开启联网搜索" });
  const assetPicker = page.getByRole("button", {
    name: "从素材选择参考图",
  });
  await expect(toolbar).toBeVisible();
  await expect(webSearch).toBeVisible();
  await expect(assetPicker).toBeVisible();
  await expect(webSearch).toBeEnabled();
  await expect(assetPicker).toBeEnabled();

  const geometry = await Promise.all(
    [composer, toolbar, webSearch, assetPicker].map((locator) =>
      locator.evaluate((node) => node.getBoundingClientRect().toJSON()),
    ),
  );
  const [composerBox, toolbarBox, webSearchBox, assetPickerBox] = geometry;
  expect(toolbarBox.top).toBeGreaterThanOrEqual(composerBox.top - 1);
  expect(toolbarBox.bottom).toBeLessThanOrEqual(composerBox.bottom + 1);
  for (const target of [webSearchBox, assetPickerBox]) {
    expect(target.width).toBeGreaterThanOrEqual(44);
    expect(target.height).toBeGreaterThanOrEqual(44);
  }
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);

  await webSearch.click();
  await expect(
    page.getByRole("button", { name: "关闭联网搜索" }),
  ).toHaveAttribute("aria-pressed", "true");
  await assetPicker.click();
  await expect(page.getByRole("dialog", { name: "选择参考图" })).toBeVisible();
});

test("limited Agent status stays visible and AA-readable on real touch light mode", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "phone-320-light",
    "explicit real-touch light-mode contrast probe",
  );
  await installAgentFixture(page, { toolGatewayConfigured: false });
  await openAgent(page);
  const status = page.locator('[data-agent-mobile-context] [role="status"]');
  await expect(status).toContainText("能力受限");
  const contrast = await status.evaluate((node) => {
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
    const foreground = parse(getComputedStyle(node).color);
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
      color: getComputedStyle(node).color,
      background: getComputedStyle(node).backgroundColor,
      effectiveBackground: background,
    };
  });
  expect(
    contrast.ratio,
    JSON.stringify(contrast),
  ).toBeGreaterThanOrEqual(4.5);
});

test("Agent capability starters configure real inputs before promising work", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "text" });
  await page.route("**/api/images/upload", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "uploaded-image-1",
        width: 4,
        height: 4,
        mime: "image/png",
        url: "/api/images/uploaded-image-1/binary",
        display_url: "/api/images/uploaded-image-1/binary",
        preview_url: "/api/images/uploaded-image-1/binary",
        thumb_url: "/api/images/uploaded-image-1/binary",
        metadata_jsonb: {},
      }),
    }),
  );
  await openAgent(page);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  const starters = page
    .getByTestId("agent-empty-suggestions")
    .getByRole("button");
  await expect(starters).toHaveCount(3);
  for (const starter of await starters.all()) await expect(starter).toBeVisible();
  const starterMedia = page.getByTestId("agent-empty-suggestions").locator("img");
  await expect(starterMedia).toHaveCount(3);
  await expect
    .poll(() =>
      starterMedia.evaluateAll((images) =>
        images.every((image) => (image as HTMLImageElement).naturalWidth > 0),
      ),
    )
    .toBe(true);
  const starterGeometry = await starters.evaluateAll((nodes) =>
    nodes.map((node) => node.getBoundingClientRect().toJSON()),
  );
  const protectedGeometry = await page
    .locator(
      '[data-agent-mobile-context], [data-testid="agent-composer"], nav[aria-label="主导航"]',
    )
    .evaluateAll((nodes) =>
      nodes
        .filter((node) => getComputedStyle(node).display !== "none")
        .map((node) => node.getBoundingClientRect().toJSON()),
    );
  for (const starter of starterGeometry) {
    for (const protectedRect of protectedGeometry) {
      const intersects = !(
        starter.right <= protectedRect.left ||
        starter.left >= protectedRect.right ||
        starter.bottom <= protectedRect.top ||
        starter.top >= protectedRect.bottom
      );
      expect(intersects).toBe(false);
    }
  }

  await page
    .getByRole("button", { name: "商业与竞品调研，开启联网" })
    .click();
  await expect(page.locator('button[aria-label="关闭联网搜索"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(input).toHaveValue(/联网搜索/u);

  await input.fill("");
  const fileStarter = page.getByRole("button", {
    name: "设计素材批量分析，选择文件",
  });
  const fileChooser = page.waitForEvent("filechooser");
  await fileStarter.click();
  const chooser = await fileChooser;
  await expect(input).toHaveValue("");
  await chooser.setFiles({
    name: "brief.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Brief\nUse current sources."),
  });
  await expect(page.getByText("brief.md", { exact: true })).toBeVisible();
  await expect(input).toHaveValue(/读取我选择的设计素材/u);
  await expect(page.locator('button[aria-label="文件已开启"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  if ((page.viewportSize()?.height ?? 999) < 500) {
    await page.getByRole("button", { name: "移除文件 brief.md" }).click();
  }

  await input.fill("");
  const imageStarter = page.getByRole("button", {
    name: "多模态视觉企划，选择图片",
  });
  const imageChooser = page.waitForEvent("filechooser");
  await imageStarter.click();
  const imageFiles = await imageChooser;
  await expect(input).toHaveValue("");
  await imageFiles.setFiles({
    name: "product.png",
    mimeType: "image/png",
    buffer: Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==",
      "base64",
    ),
  });
  await expect(page.getByAltText("product.png 1")).toBeVisible();
  await expect(input).toHaveValue(/分析我选择的产品图/u);
  await expect(page.locator('button[aria-label="生图已开启"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

test("Agent execution summary and ContextBar update submitted parameters", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  await openAgent(page);

  const summary = page.getByTestId("agent-execution-summary");
  const shortLandscape = (page.viewportSize()?.height ?? 999) < 500;
  if (shortLandscape) {
    await expect(summary).toBeHidden();
    await page.getByRole("button", { name: "Agent 参数与会话设置" }).click();
    await page.getByRole("combobox", { name: "默认图片数量" }).selectOption("3");
    await page.getByRole("button", { name: "竖向 4:5" }).click();
    await page.getByRole("combobox", { name: "默认图片分辨率" }).selectOption("4k");
    await page.getByRole("combobox", { name: "默认渲染质量" }).selectOption("medium");
    await page.getByRole("combobox", { name: "默认背景" }).selectOption("transparent");
  } else {
    await expect(summary).toBeVisible();
    await summary.getByRole("combobox", { name: "执行图片数量" }).selectOption("3");
    await summary.getByRole("combobox", { name: "执行图片比例" }).selectOption("4:5");
    await summary.getByRole("combobox", { name: "执行图片分辨率" }).selectOption("4k");
    await summary.getByRole("combobox", { name: "执行渲染质量" }).selectOption("medium");
    await summary.getByRole("combobox", { name: "执行图片背景" }).selectOption("transparent");
    await expect(summary.getByText("预计扣 ¥1.20", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Agent 参数与会话设置" }).click();
  }
  const model = page.getByRole("combobox", { name: "Agent 模型" });
  await expect(model).toBeVisible();
  await model.selectOption("fixture-fast-model");
  const reasoning = page.getByRole("combobox", { name: "Agent 推理强度" });
  await expect(reasoning).toBeDisabled();
  await expect(reasoning).toHaveValue("none");
  await page.keyboard.press("Escape");

  await page.getByRole("textbox", { name: "发送给 Agent" }).fill("生成参数测试");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => fixture.lastMessageBody).not.toBeNull();
  expect(fixture.lastMessageBody).toMatchObject({
    model: "fixture-fast-model",
    reasoning_effort: "none",
    image_defaults: {
      count: 3,
      aspect_ratio: "4:5",
      quality: "4k",
      render_quality: "medium",
      background: "transparent",
    },
  });
});

test("disabling file tools requires confirmation and removes attached files atomically", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "text" });
  await openAgent(page);
  await page
    .locator('input[type="file"][accept^=".txt"]')
    .setInputFiles({
      name: "brief.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# Brief"),
    });
  const shortLandscape = (page.viewportSize()?.height ?? 999) < 500;
  const fileToggle = shortLandscape
    ? page.getByRole("switch", { name: "文件工具" })
    : page.getByRole("button", { name: "文件已开启" });
  if (shortLandscape) {
    await page.getByRole("button", { name: "Agent 设置" }).click();
    await expect(fileToggle).toHaveAttribute("aria-checked", "true");
  } else {
    await expect(fileToggle).toBeEnabled();
  }
  await fileToggle.click();
  const confirmation = page.getByRole("dialog", { name: "关闭文件工具？" });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "取消" }).click();
  await expect(page.getByText("brief.md", { exact: true })).toBeVisible();
  await expect(fileToggle).toHaveAttribute(
    shortLandscape ? "aria-checked" : "aria-pressed",
    "true",
  );

  await fileToggle.click();
  await page
    .getByRole("dialog", { name: "关闭文件工具？" })
    .getByRole("button", { name: "关闭并移除" })
    .click();
  await expect(page.getByText("brief.md", { exact: true })).toHaveCount(0);
  if (shortLandscape) {
    await expect(fileToggle).toHaveAttribute("aria-checked", "false");
  } else {
    await expect(
      page.getByRole("button", { name: "文件待文件" }),
    ).toHaveAttribute("aria-pressed", "false");
  }
});

test("Agent tool calls expose an accessible sanitized accordion", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "text" });
  await page.route(
    "**/api/agent/sessions/session-1/messages?**",
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(toolDetailsSnapshot()),
      }),
  );
  await openAgent(page);
  await expect(
    page.locator(
      '[data-agent-tool-call] [role="status"] :is(button, a), [data-testid="agent-conversation-scroll"] [role="alert"] :is(button, a), [data-testid="agent-composer"] [role="alert"] :is(button, a)',
    ),
  ).toHaveCount(0);
  await page.emulateMedia({ reducedMotion: "reduce" });
  const disclosure = page.locator(
    '[data-agent-tool-call="tool-web"] [data-agent-tool-disclosure]',
  );
  await expect
    .poll(() =>
      disclosure.evaluate((node) =>
        Number.parseFloat(getComputedStyle(node).transitionDuration),
      ),
    )
    .toBeLessThanOrEqual(0.001);

  const web = page
    .locator('[data-agent-tool-call="tool-web"]')
    .getByRole("button", { name: /联网搜索/u });
  await expect(web).toHaveAttribute("aria-expanded", "false");
  await web.click();
  await expect(web).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("2026 极简美妆视觉趋势", { exact: true })).toBeVisible();
  await expect(page.getByText(/行业报告/u)).toBeVisible();
  await expect(page.getByText("耗时").first()).toBeVisible();

  await page.getByRole("button", { name: /文件内搜索/u }).click();
  const fileDetails = page.getByRole("region", {
    name: "文件内搜索执行详情",
  });
  await expect(fileDetails.getByText("brief.md", { exact: true })).toBeVisible();
  await expect(fileDetails.getByText("品牌色", { exact: true })).toBeVisible();
  await expect(
    fileDetails.getByText("brief.md:4 - 品牌色使用暖金与炭黑", {
      exact: true,
    }),
  ).toBeVisible();

  await page.getByRole("button", { name: /图生图/u }).click();
  await expect(page.getByText("暖金与炭黑的极简美妆产品海报", { exact: true })).toBeVisible();
  await expect(page.getByText(/2 张 · 4:5 · 2K/u)).toBeVisible();
  await expect(page.getByText(/private|api.key|callback|\/srv\//iu)).toHaveCount(0);
});

test("Agent context title, pin, and branch controls preserve focus and state", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page);
  const sessionPatches: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "PATCH" &&
      new URL(request.url()).pathname === "/api/agent/sessions/session-1"
    ) {
      sessionPatches.push(request.postDataJSON() as Record<string, unknown>);
    }
  });
  await openAgent(page);

  await page.getByRole("button", { name: "重命名会话：产品视觉" }).click();
  const title = page.getByRole("textbox", { name: "会话名称" });
  await title.fill("春季视觉企划");
  await title.press("Enter");
  await expect.poll(() => sessionPatches).toContainEqual({
    title: "春季视觉企划",
  });
  const renamedTrigger = page.getByRole("button", {
    name: "重命名会话：春季视觉企划",
  });
  await expect(renamedTrigger).toBeFocused();

  await renamedTrigger.click();
  const cancelledTitle = page.getByRole("textbox", { name: "会话名称" });
  await cancelledTitle.fill("不应保存");
  await cancelledTitle.press("Escape");
  await expect(renamedTrigger).toBeFocused();
  expect(sessionPatches).not.toContainEqual({ title: "不应保存" });

  await renamedTrigger.click();
  const blurredTitle = page.getByRole("textbox", { name: "会话名称" });
  await blurredTitle.fill("春季视觉分镜");
  await blurredTitle.evaluate((element) => (element as HTMLInputElement).blur());
  await expect.poll(() => sessionPatches).toContainEqual({
    title: "春季视觉分镜",
  });
  await expect(
    page.getByRole("button", { name: "重命名会话：春季视觉分镜" }),
  ).toBeFocused();

  await page.getByRole("button", { name: "置顶会话" }).click();
  await expect.poll(() => sessionPatches).toContainEqual({ pinned: true });

  await page.getByRole("button", { name: "分支会话" }).click();
  await expect.poll(() => fixture.branchCalls).toBe(1);
  await expect(page).toHaveURL(/session=session-2/u);
});

test("Agent media drawer has stable horizontal geometry and user turns stay right aligned", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  await openAgent(page);
  await page
    .locator('input[type="file"][accept^=".txt"]')
    .setInputFiles([
      {
        name: "brief.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("# Brief"),
      },
      {
        name: "notes.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("Notes"),
      },
    ]);
  const drawer = page.getByTestId("agent-media-drawer");
  await expect(drawer).toHaveAttribute("data-open", "true");
  await expect
    .poll(() => drawer.evaluate((node) => node.getBoundingClientRect().height))
    .toBeGreaterThan(112.9);
  const initialHeight = await drawer.evaluate((node) =>
    node.getBoundingClientRect().height,
  );
  await page
    .getByRole("button", { name: "移除文件 brief.md" })
    .click();
  await expect(page.getByText("brief.md", { exact: true })).toHaveCount(0);
  await expect
    .poll(async () =>
      Math.abs(
        initialHeight -
          (await drawer.evaluate((node) => node.getBoundingClientRect().height)),
      ) <= 1,
    )
    .toBe(true);
  const reducedHeight = await drawer.evaluate((node) =>
    node.getBoundingClientRect().height,
  );
  expect(Math.abs(initialHeight - reducedHeight)).toBeLessThanOrEqual(1);
  expect(
    await drawer.evaluate((node) => {
      const scroller = node.querySelector<HTMLElement>("[aria-label='本轮媒体']");
      return scroller ? scroller.scrollWidth >= scroller.clientWidth : false;
    }),
  ).toBe(true);

  await page
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("保持清晰的用户气泡");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => fixture.lastMessageBody).not.toBeNull();
  const user = page.locator("#agent-message-user-1");
  await expect(user).toBeVisible();
  const alignment = await user.evaluate((node) => {
    const article = node.getBoundingClientRect();
    const bubble = node.firstElementChild?.getBoundingClientRect();
    return bubble
      ? {
          rightGap: Math.abs(article.right - bubble.right),
          width: bubble.width,
          articleWidth: article.width,
        }
      : null;
  });
  expect(alignment).not.toBeNull();
  expect(alignment?.rightGap ?? 99).toBeLessThanOrEqual(1);
  expect(alignment?.width ?? 999).toBeLessThan(alignment?.articleWidth ?? 0);
});

test("text reply submits and restores from authoritative snapshots", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  await openAgent(page);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  await input.fill("给我一个产品视觉方向");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("已完成产品视觉方向。")).toBeVisible();
  await expect(input).toHaveValue("");
  const assistantBox = await page
    .locator("#agent-message-assistant-1")
    .boundingBox();
  const composerBox = await page
    .getByRole("textbox", { name: "发送给 Agent" })
    .evaluate((node) =>
      node.closest(".surface-panel")?.getBoundingClientRect().toJSON(),
    );
  expect(assistantBox).not.toBeNull();
  expect(composerBox).toBeTruthy();
  const assistantBottom = assistantBox
    ? assistantBox.y + assistantBox.height
    : 0;
  expect(assistantBottom <= (composerBox as DOMRect).top + 1).toBe(true);
  expect(fixture.lastMessageBody?.text).toBe("给我一个产品视觉方向");
  expect(fixture.lastMessageBody).not.toHaveProperty("reasoning_effort");
  await page.reload();
  await expect(page.getByText("已完成产品视觉方向。")).toBeVisible();
});

test("web search and virtual text files serialize from the Agent composer", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  await openAgent(page);
  await page
    .locator('input[type="file"][accept^=".txt"]')
    .setInputFiles({
      name: "brief.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# Brief\nUse current sources."),
    });
  await expect(page.getByText("brief.md", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "开启联网搜索" }).click();
  await page
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("分析文件并核对最新信息");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  await expect.poll(() => fixture.lastMessageBody?.allow_web_search).toBe(true);
  expect(fixture.lastMessageBody?.allow_file_tools).toBe(true);
  expect(fixture.lastMessageBody?.files).toEqual([
    {
      name: "brief.md",
      mime_type: "text/markdown",
      size: 28,
      content: "# Brief\nUse current sources.",
    },
  ]);
});

test("ordered references and roles serialize directly from Agent state", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "text" });
  await openAgent(page);
  await page.getByRole("button", { name: "从素材选择参考图" }).click();
  await page.getByRole("button", { name: /添加参考图：素材参考 1/ }).click();
  await page.getByRole("button", { name: /添加参考图：素材参考 2/ }).click();
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(
    page
      .getByTestId("agent-media-drawer")
      .getByText("本轮输入 2 张", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "参考图 1 角色" })
    .selectOption("product");
  await page
    .getByRole("combobox", { name: "参考图 2 角色" })
    .selectOption("style");
  await page.getByRole("button", { name: "参考图 2 前移" }).click();
  await page.getByRole("textbox", { name: "发送给 Agent" }).fill("按顺序生成");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => fixture.lastMessageBody).not.toBeNull();
  expect(fixture.lastMessageBody?.attachments).toEqual([
    { image_id: "feed-image-2", role: "style", label: "素材图" },
    { image_id: "feed-image-1", role: "product", label: "素材图" },
  ]);
});

test("image tool results survive refresh, open the lightbox, and stay in Agent references", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "image" });
  await openAgent(page);
  await page
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("生成产品主图");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const result = page.getByAltText("明亮产品主图");
  await expect(result).toBeVisible();
  await expect(result).toHaveJSProperty("complete", true);
  await assertImagePixels(page, 'img[alt="明亮产品主图"]');
  await page.getByRole("button", { name: "查看大图" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "用作参考图" }).click();
  await expect(
    page.getByRole("combobox", { name: "参考图 1 角色" }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByAltText("明亮产品主图")).toBeVisible();
});

test("partial image, cancellation, and stable account errors remain actionable", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "partial-image" });
  await openAgent(page);
  await expect(page.getByText("部分完成")).toBeVisible();
  await expect(page.getByAltText("明亮产品主图")).toBeVisible();

  const cancelPage = await page.context().newPage();
  const cancelFixture = await installAgentFixture(cancelPage, {
    mode: "cancel",
  });
  await openAgent(cancelPage);
  await cancelPage.getByRole("button", { name: "停止 Agent 运行" }).click();
  await expect.poll(() => cancelFixture.cancelCalls).toBe(1);

  const errorPage = await page.context().newPage();
  await installAgentFixture(errorPage, {
    mode: "error",
    errorCode: "INSUFFICIENT_BALANCE",
  });
  await openAgent(errorPage);
  await errorPage
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("继续生成");
  await errorPage.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    errorPage.getByText("充值后可继续运行 Agent。").first(),
  ).toBeVisible();
  await expect(
    errorPage.getByRole("link", { name: "查看钱包" }),
  ).toHaveAttribute("href", "/me/wallet");

  const byokPage = await page.context().newPage();
  await installAgentFixture(byokPage, {
    mode: "error",
    errorCode: "NO_ACTIVE_API_KEY",
  });
  await openAgent(byokPage);
  await byokPage
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("文本回复");
  await byokPage.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    byokPage.getByText("添加有效密钥后可继续运行 Agent。").first(),
  ).toBeVisible();
  await expect(
    byokPage.getByRole("link", { name: "管理密钥" }),
  ).toHaveAttribute("href", "/settings/api-key");

  const visionPage = await page.context().newPage();
  await installAgentFixture(visionPage, {
    mode: "error",
    errorCode: "agent_vision_model_unavailable",
  });
  await openAgent(visionPage);
  await visionPage
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("分析参考图");
  await visionPage.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    visionPage.getByText("当前对话模型不支持参考图。").first(),
  ).toBeVisible();

  const runtimePage = await page.context().newPage();
  await installAgentFixture(runtimePage, {
    mode: "error",
    errorCode: "agent_runtime_unavailable",
  });
  await openAgent(runtimePage);
  await runtimePage
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("运行 Agent");
  await runtimePage.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    runtimePage.getByText("运行时暂不可用，稍后重试。").first(),
  ).toBeVisible();
});

test("active Agent keeps the next-turn draft editable while stop remains available", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "active-image" });
  await openAgent(page);
  const input = page.getByRole("textbox", { name: "发送给 Agent" });
  await expect(input).toBeEnabled();
  await expect(input).toHaveAttribute("placeholder", "准备下一轮消息");
  await input.fill("下一轮继续整理来源");
  await page.getByRole("button", { name: "停止 Agent 运行" }).click();
  await expect.poll(() => fixture.cancelCalls).toBe(1);
  await expect(input).toHaveValue("下一轮继续整理来源");
});

test("active Agent snapshot polling stays bounded", async ({ page }) => {
  const fixture = await installAgentFixture(page, { mode: "active-image" });
  await openAgent(page);
  await page.waitForTimeout(2_000);
  const settledCalls = fixture.snapshotCalls;
  await page.waitForTimeout(1_000);
  expect(fixture.snapshotCalls - settledCalls).toBeLessThanOrEqual(4);
});

test("partial Agent run continues without replaying browser inputs", async ({
  page,
}) => {
  const fixture = await installAgentFixture(page, { mode: "partial-image" });
  await openAgent(page);

  await page.getByRole("button", { name: "继续", exact: true }).click();

  await expect.poll(() => fixture.continuationCalls).toBe(1);
  expect(fixture.lastContinuationBody).toEqual({
    idempotency_key: expect.stringMatching(/^agent-continue/u),
  });
});

test("task tray locates the owning Agent assistant message", async ({
  page,
}) => {
  await installAgentFixture(page, { mode: "active-image" });
  await openAgent(page);
  const taskButton = page.getByRole("button", { name: /打开任务中心/ }).first();
  await expect(taskButton).toBeVisible();
  await taskButton.click();
  await page.getByRole("button", { name: "定位任务消息" }).first().click();
  await expect(page).toHaveURL(
    /\/agent\?session=session-1&scrollTo=assistant-1/,
  );
  await expect(page.locator("#agent-message-assistant-1")).toBeVisible();
});

test("two authenticated users do not share Agent drafts", async ({ page }) => {
  await installAgentFixture(page, { userId: "user-a" });
  await openAgent(page);
  await page
    .getByRole("textbox", { name: "发送给 Agent" })
    .fill("user A private draft");
  await expect(page.getByRole("textbox", { name: "发送给 Agent" })).toHaveValue(
    "user A private draft",
  );

  const userBPage = await page.context().newPage();
  await installAgentFixture(userBPage, { userId: "user-b" });
  await openAgent(userBPage);
  await expect(
    userBPage.getByRole("textbox", { name: "发送给 Agent" }),
  ).toHaveValue("");
  await expect(userBPage.getByText("user A private draft")).toHaveCount(0);
});

test("archived deep links load by id and preserve scroll targets outside the first page", async ({
  page,
}) => {
  await installAgentFixture(page, {
    archived: true,
    omitSessionFromList: true,
    mode: "partial-image",
  });
  await page.goto("/agent?session=session-1&scrollTo=assistant-1");
  await expect(page.locator("[data-agent-workspace]")).toBeVisible();
  await expect(page).toHaveURL(/session=session-1&scrollTo=assistant-1/u);
  await expect(page.locator("#agent-message-assistant-1")).toBeVisible();
});
