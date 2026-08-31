import { expect, test } from "@playwright/test";
import {
  assertImagePixels,
  installAgentFixture,
  openAgent,
} from "./agent-fixture";

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
  await expect(page.getByText(/本轮输入 2 张/)).toBeVisible();
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
