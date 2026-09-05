import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";

import { installAgentFixture } from "./agent-fixture";

interface Mutation {
  base_revision: number;
  client_id: string;
  mutation_id: string;
  operations: Array<{ op: string; node_id?: string; config?: Record<string, unknown> }>;
}

async function installCanvasFixture(page: Page, initialMode: "network" | "lost-ack" | "conflict" = "network") {
  await installAgentFixture(page, { canvasEnabled: true });
  const graph = {
    schema_version: 1,
    nodes: [{
      id: "prompt-1", type: "prompt", schema_version: 1, title: "提示词",
      position: { x: 0, y: 0 }, size: { width: 260, height: 200 },
      config: { text: "Remote prompt", locked: false } as Record<string, unknown>, ui: {},
    }],
    edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 },
  };
  let mode: typeof initialMode | "success" = initialMode;
  let revision = 4;
  const requests: Mutation[] = [];
  const accepted = new Map<string, number>();
  const document = () => ({
    id: "canvas-durability", title: "保存测试", description: "", revision, graph,
    created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:00Z",
    selections: [], recent_executions: [], active_runs: [],
  });
  await page.route("**/api/canvases/canvas-durability**", async (route) => {
    if (!new URL(route.request().url()).pathname.endsWith("/mutations")) {
      return route.fulfill({ json: document() });
    }
    const body = route.request().postDataJSON() as Mutation;
    requests.push(body);
    if (mode === "conflict") {
      revision = 9;
      graph.nodes[0]!.config.text = "Other tab edit";
      return route.fulfill({ status: 409, json: { detail: { error: { code: "canvas_revision_conflict", message: "Revision conflict" } } } });
    }
    if (mode === "network") return route.abort("connectionfailed");
    if (!accepted.has(body.mutation_id)) {
      for (const operation of body.operations) {
        if (operation.op === "update_node_config") graph.nodes[0]!.config = operation.config!;
      }
      revision += 1;
      accepted.set(body.mutation_id, revision);
    }
    if (mode === "lost-ack") return route.abort("connectionfailed");
    return route.fulfill({ json: { revision: accepted.get(body.mutation_id) } });
  });
  return { requests, succeed: () => { mode = "success"; }, revision: () => revision };
}

async function editPrompt(page: Page, text = "Local pending edit") {
  const editor = page.getByRole("textbox", { name: "编辑提示词内容" });
  await expect(editor).toBeEditable();
  await editor.fill(text);
  await editor.blur();
}

async function expectCanvasGeometry(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const status = await page.locator("[data-canvas-save-status]").boundingBox();
  const viewport = await page.locator('[aria-label="无限画布编辑区"]').boundingBox();
  expect(status).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(viewport!.height).toBeGreaterThan(32);
  expect(viewport!.y).toBeGreaterThanOrEqual(status!.y + status!.height);
}

async function exportCopy(page: Page) {
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出当前副本", exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/^lumen-canvas-canvas-durability-\d+\.json$/u);
  return JSON.parse(await readFile((await download.path())!, "utf8"));
}

test("canvas draft status waits for the writer ACK and invalidates it for newer edits", async ({ page }) => {
  await installCanvasFixture(page);
  await page.goto("/projects/canvas/canvas-durability");
  const status = page.locator("[data-canvas-save-status]");
  await expect(status).toContainText("已保存 · 版本 4");
  await expect(status).toHaveAttribute("data-canvas-local-durable", "true");
  await page.evaluate(() => {
    const descriptor = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, "oncomplete")!;
    const acknowledgements: Array<() => void> = [];
    Object.defineProperty(window, "releaseCanvasDraftAcks", { value: () => acknowledgements.splice(0).forEach((ack) => ack()) });
    Object.defineProperty(IDBTransaction.prototype, "oncomplete", {
      configurable: true,
      get: descriptor.get,
      set(this: IDBTransaction, listener: ((event: Event) => void) | null) {
        descriptor.set!.call(this, listener && ((event: Event) => {
          if (this.mode === "readwrite" && this.objectStoreNames.contains("drafts")) {
            acknowledgements.push(() => listener.call(this, event));
          } else listener.call(this, event);
        }));
      },
    });
  });
  await editPrompt(page);
  await expect(status).toContainText("本地副本待确认");
  await expect(status).not.toContainText("本地副本可用");
  await expect(status).toContainText("保存失败");
  await page.evaluate(() => (window as unknown as { releaseCanvasDraftAcks: () => void }).releaseCanvasDraftAcks());
  await expect(status).toContainText("本地副本可用");
  await editPrompt(page, "Newer pending edit");
  await expect(status).toContainText("本地副本待确认");
});

for (const unavailable of ["indexedDB", "localStorage"] as const) {
  test(`canvas ${unavailable} failure stays honest and current-copy export works`, async ({ page }, testInfo) => {
    const fixture = await installCanvasFixture(page);
    await page.addInitScript((kind) => {
      if (kind === "indexedDB") {
        const open = IDBFactory.prototype.open;
        IDBFactory.prototype.open = function (name, version) {
          if (name === "lumen-canvas") throw new DOMException("Blocked", "SecurityError");
          return open.call(this, name, version);
        };
      } else {
        const setItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
          if (key.startsWith("lumen:canvas")) throw new DOMException("Blocked", "QuotaExceededError");
          setItem.call(this, key, value);
        };
      }
    }, unavailable);
    await page.goto("/projects/canvas/canvas-durability");
    await editPrompt(page);
    const status = page.locator("[data-canvas-save-status]");
    await expect(status).toContainText("本地恢复不可用");
    await expect(status).not.toContainText("本地副本可用");
    await expect(page.getByRole("button", { name: "重试保存", exact: true })).toBeVisible();
    const copy = await exportCopy(page);
    expect(copy.base_revision).toBe(4);
    expect(copy.graph.nodes[0].config.text).toBe("Local pending edit");
    expect(copy.operations.length).toBeGreaterThan(0);
    expect(copy.operation_group_sizes.reduce((sum: number, size: number) => sum + size, 0)).toBe(copy.operations.length);
    await expectCanvasGeometry(page);
    await testInfo.attach(`canvas-${unavailable}-unavailable`, {
      body: await page.screenshot(), contentType: "image/png",
    });
    fixture.succeed();
    await page.getByRole("button", { name: "重试保存", exact: true }).click();
    await expect(status).toContainText("已保存 · 版本 5");
    await expect(status).toContainText("本地恢复不可用");
  });
}

test("canvas failed initial owner fence never claims reload recovery after storage becomes writable", async ({ page }, testInfo) => {
  await installCanvasFixture(page);
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === "lumen:canvas-owner:v1" && !sessionStorage.getItem("allow-canvas-owner")) {
        throw new DOMException("Owner write blocked", "QuotaExceededError");
      }
      setItem.call(this, key, value);
    };
  });
  await page.goto("/projects/canvas/canvas-durability");
  await editPrompt(page, "Not fenced local edit");
  const status = page.locator("[data-canvas-save-status]");
  await expect(status).toContainText("保存失败");
  await expect(status).toContainText("本地恢复不可用");
  await page.evaluate(() => sessionStorage.setItem("allow-canvas-owner", "1"));
  await page.getByRole("button", { name: "重试保存", exact: true }).click();
  await expect(status).toContainText("保存失败");
  await expect(status).toContainText("本地恢复不可用");
  await expect(status).not.toContainText("本地副本可用");
  const exported = await exportCopy(page);
  expect(exported.graph.nodes[0].config.text).toBe("Not fenced local edit");
  await testInfo.attach("canvas-owner-fence-unavailable", { body: await page.screenshot(), contentType: "image/png" });
  await page.reload();
  await expect(page.getByRole("textbox", { name: "编辑提示词内容" })).toHaveValue("Remote prompt");
  expect(await page.evaluate(() => localStorage.getItem("lumen:canvas-owner:v1"))).toBe("user-a");
});

test("canvas retries a lost server ACK with the original mutation identity", async ({ page }) => {
  const fixture = await installCanvasFixture(page, "lost-ack");
  await page.goto("/projects/canvas/canvas-durability");
  await editPrompt(page);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("保存失败");
  const first = structuredClone(fixture.requests[0]!);
  fixture.succeed();
  await page.getByRole("button", { name: "重试保存", exact: true }).click();
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存 · 版本 5");
  expect(fixture.requests.length).toBeGreaterThanOrEqual(2);
  for (const request of fixture.requests) expect(request).toEqual(first);
  expect(fixture.revision()).toBe(5);
});

test("canvas conflict shows remote revision, local baseline and pending edits without replacing local content", async ({ page }, testInfo) => {
  await installCanvasFixture(page, "conflict");
  await page.goto("/projects/canvas/canvas-durability");
  await editPrompt(page);
  await expect(page.locator("[data-canvas-save-status]")).toContainText("版本冲突");
  const comparison = page.locator('[aria-label="冲突版本"] dd');
  await expect(comparison).toHaveText(["9", "4", "1"]);
  await expect(page.getByRole("textbox", { name: "编辑提示词内容" })).toHaveValue("Local pending edit");
  await page.getByRole("button", { name: "重新检查版本", exact: true }).click();
  const copy = await exportCopy(page);
  expect(copy.base_revision).toBe(4);
  expect(copy.graph.nodes[0].config.text).toBe("Local pending edit");
  await expectCanvasGeometry(page);
  await testInfo.attach("canvas-conflict", {
    body: await page.screenshot(), contentType: "image/png",
  });
  await page.getByRole("button", { name: "采用远端", exact: true }).click();
  await expect(page.locator("[data-canvas-save-status]")).toContainText("已保存 · 版本 9");
  await expect(page.getByRole("textbox", { name: "编辑提示词内容" })).toHaveValue("Other tab edit");
});

test("canvas pagehide and reload recover pending drafts and the unconfirmed request without duplicating it", async ({ page }) => {
  const fixture = await installCanvasFixture(page);
  await page.goto("/projects/canvas/canvas-durability");
  await editPrompt(page);
  const status = page.locator("[data-canvas-save-status]");
  await expect(status).toContainText("保存失败");
  await expect(status).toContainText("本地副本可用");
  const first = structuredClone(fixture.requests[0]!);
  const emergency = await page.evaluate(() => {
    window.dispatchEvent(new Event("pagehide"));
    // The successful IndexedDB writer may retire this emergency copy on its next ACK.
    return localStorage.getItem("lumen:canvas-emergency-drafts:v1");
  });
  expect(emergency).not.toBeNull();
  fixture.succeed();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "编辑提示词内容" })).toHaveValue("Local pending edit");
  await expect(status).toContainText("已保存 · 版本 5");
  for (const request of fixture.requests) expect(request).toEqual(first);
  expect(fixture.revision()).toBe(5);
});
