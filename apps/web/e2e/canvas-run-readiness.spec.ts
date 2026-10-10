import { expect, test, type Page } from "@playwright/test";

import { installAgentFixture } from "./agent-fixture";

type OptionsMode = "available" | "unavailable" | "offline";
type Mutation = {
  operations: Array<{ op: string; node_id?: string; config?: Record<string, unknown> }>;
};

async function installReadinessFixture(page: Page, initialMode: OptionsMode) {
  await installAgentFixture(page, { canvasEnabled: true });
  const graph = {
    schema_version: 1,
    nodes: [
      {
        id: "prompt-1", type: "prompt", schema_version: 1, title: "提示词",
        position: { x: 0, y: 0 }, size: { width: 260, height: 200 },
        config: { text: "镜头缓慢推进", locked: false } as Record<string, unknown>, ui: {},
      },
      ...Array.from({ length: 3 }, (_, index) => ({
        id: `video-${index}`, type: "video_text_generate", schema_version: 1,
        title: `视频 ${index + 1}`, position: { x: 380, y: index * 240 },
        config: {
          mode: "t2v", model: null, duration_s: 5, resolution: "720p",
          aspect_ratio: "16:9", generate_audio: true, seed: null, watermark: false,
        } as Record<string, unknown>,
        ui: {},
      })),
    ],
    edges: Array.from({ length: 3 }, (_, index) => ({
      id: `prompt-video-${index}`, source_node_id: "prompt-1", source_handle: "text",
      target_node_id: `video-${index}`, target_handle: "prompt",
      data_type: "text", binding_mode: "follow_active",
    })),
    frames: [], settings: { snap_to_grid: false, grid_size: 16 },
  };
  let mode = initialMode;
  let revision = 4;
  let optionsRequests = 0;
  let executeRequests = 0;
  let gate: Promise<void> | null = null;
  const document = () => ({
    id: "canvas-readiness", title: "运行条件测试", description: "", revision, graph,
    created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:00Z",
    selections: [], recent_executions: [], active_runs: [],
  });
  await page.route("**/api/videos/options", async (route) => {
    optionsRequests += 1;
    if (gate) await gate;
    if (mode === "offline") {
      return route.fulfill({ status: 503, json: { detail: "Temporary capability failure" } });
    }
    return route.fulfill({ json: {
      enabled: mode === "available",
      unavailable_reason: mode === "unavailable" ? "account_mode_forbidden" : null,
      models: [{
        model: "video-test", actions: ["t2v"], resolutions: ["720p"], durations_s: [5],
      }],
      durations_s: [5], resolutions: ["720p"], aspect_ratios: ["16:9"],
      generate_audio: true, pricing: [], hold_estimates: {},
    } });
  });
  await page.route("**/api/canvases/canvas-readiness**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/execute")) {
      executeRequests += 1;
      return route.fulfill({ status: 400, json: { detail: "This fixture must not execute" } });
    }
    if (pathname.endsWith("/mutations")) {
      for (const operation of (route.request().postDataJSON() as Mutation).operations) {
        const node = graph.nodes.find((item) => item.id === operation.node_id);
        if (operation.op === "update_node_config" && node && operation.config) {
          node.config = operation.config;
        }
      }
      revision += 1;
      return route.fulfill({ json: { revision } });
    }
    return route.fulfill({ json: document() });
  });
  return {
    optionsRequests: () => optionsRequests,
    executeRequests: () => executeRequests,
    setMode: (next: OptionsMode) => { mode = next; },
    delayOptions: () => {
      let release!: () => void;
      gate = new Promise<void>((resolve) => { release = resolve; });
      return () => { gate = null; release(); };
    },
  };
}

function node(page: Page, id = "video-0") {
  return page.locator(`.react-flow__node[data-id="${id}"]`);
}

async function selectFirstVideo(page: Page) {
  await node(page).locator("header").click({ position: { x: 24, y: 16 } });
  await expect(page.getByRole("button", { name: "运行节点", exact: true }).last()).toBeVisible();
}

async function expectMenuRun(page: Page, disabled: boolean, reason?: string) {
  await page.getByRole("button", { name: "打开命令菜单", exact: true }).click();
  const item = page.getByRole("option", { name: /运行当前节点/ });
  if (disabled) await expect(item).toBeDisabled();
  else await expect(item).toBeEnabled();
  if (reason) await expect(item).toContainText(reason);
  await page.keyboard.press("Escape");
}

test.use({ viewport: { width: 1440, height: 1000 } });

test("mobile node, inspector and command menu share the unavailable reason", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const fixture = await installReadinessFixture(page, "unavailable");
  await page.goto("/projects/canvas/canvas-readiness");
  const reason = "BYOK 模式暂不支持视频生成";
  await expect(node(page).getByRole("button", { name: `节点不可运行：${reason}` })).toBeDisabled();
  await node(page).locator("header").click({ position: { x: 24, y: 16 } });
  await page.getByRole("button", { name: "打开检查器", exact: true }).click();
  const inspector = page.getByRole("dialog", { name: "节点检查器" });
  await expect(inspector.getByRole("button", { name: "运行节点", exact: true })).toBeDisabled();
  await expect(inspector.locator("footer").getByText(reason, { exact: true })).toBeVisible();
  await inspector.getByRole("button", { name: "关闭检查器", exact: true }).click();
  // Closing the mobile Inspector intentionally clears selection; select again.
  await node(page).locator("header").click({ position: { x: 24, y: 16 } });
  await page.getByRole("button", { name: "更多画布操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "命令菜单", exact: true }).click();
  const run = page.getByRole("option", { name: /运行当前节点/ });
  await expect(run).toBeDisabled();
  await expect(run).toContainText(reason);
  expect(fixture.executeRequests()).toBe(0);
});

test("canvas nodes, inspector and command menu share one capability preflight", async ({ page }) => {
  const fixture = await installReadinessFixture(page, "unavailable");
  await page.goto("/projects/canvas/canvas-readiness");
  const reason = "BYOK 模式暂不支持视频生成";
  for (let index = 0; index < 3; index += 1) {
    await expect(node(page, `video-${index}`).getByRole("button", { name: `节点不可运行：${reason}` })).toBeDisabled();
  }
  await selectFirstVideo(page);
  await expect(page.getByRole("button", { name: "运行节点", exact: true })).toBeDisabled();
  await expect(page.locator("[data-canvas-selected-runnable]")).toHaveAttribute("data-canvas-selected-runnable", "false");
  await expectMenuRun(page, true, reason);
  expect(fixture.optionsRequests()).toBe(1);
  expect(fixture.executeRequests()).toBe(0);
});

test("a transient preflight failure is retryable and never bypasses the final capability check", async ({ page }) => {
  const fixture = await installReadinessFixture(page, "offline");
  await page.goto("/projects/canvas/canvas-readiness");
  await selectFirstVideo(page);
  await expect(page.getByText("视频能力检查暂不可用，可重试，或运行时重新检查", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "运行节点", exact: true }).last()).toBeEnabled();
  await expectMenuRun(page, false);
  fixture.setMode("available");
  await page.getByRole("button", { name: "重试加载", exact: true }).click();
  await expect(page.getByText("视频能力检查暂不可用，可重试，或运行时重新检查", { exact: true })).toBeHidden();
  await expect(node(page).getByRole("button", { name: "运行节点", exact: true })).toBeEnabled();
  const beforeRun = fixture.optionsRequests();
  fixture.setMode("unavailable");
  // Refit after the Inspector claims layout width, then use the real node target.
  await page.getByRole("button", { name: "适应视图", exact: true }).filter({ visible: true }).first().click();
  await node(page).getByRole("button", { name: "运行节点", exact: true }).click();
  await expect.poll(fixture.optionsRequests).toBeGreaterThan(beforeRun);
  await expect(page.getByText("BYOK 模式暂不支持视频生成", { exact: true })).toBeVisible();
  expect(fixture.executeRequests()).toBe(0);
});

test("configuration changes while the final check is pending cannot submit the stale intent", async ({ page }) => {
  const fixture = await installReadinessFixture(page, "available");
  await page.goto("/projects/canvas/canvas-readiness");
  const run = node(page).getByRole("button", { name: "运行节点", exact: true });
  await expect(run).toBeEnabled();
  const beforeRun = fixture.optionsRequests();
  const release = fixture.delayOptions();
  await run.click();
  await expect.poll(fixture.optionsRequests).toBeGreaterThan(beforeRun);
  const prompt = page.getByRole("textbox", { name: "编辑提示词内容" });
  await prompt.fill("运行检查期间修改的提示词");
  await prompt.blur();
  release();
  await expect(page.getByText("节点配置已变化，请重新运行", { exact: true })).toBeVisible();
  expect(fixture.executeRequests()).toBe(0);
  await expect(node(page).getByRole("button", { name: "运行节点", exact: true })).toBeEnabled();
});
