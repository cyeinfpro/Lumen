#!/usr/bin/env node

const HOST = "http://[::1]:9222";
const BASE = "http://localhost:3000";

const pages = [
  "/",
  "/login",
  "/admin",
  "/stream",
  "/projects",
  "/projects/new",
  "/projects/apparel-model-showcase/new",
  "/projects/scroll-check",
  "/settings/usage",
  "/settings/privacy",
  "/settings/prompts",
  "/me",
  "/reset-password",
  "/reset-password/scroll-check",
  "/invite/scroll-check",
  "/share/scroll-check",
];

const viewports = [
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1280, height: 900 },
];

async function json(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${url}`);
  return res.json();
}

function send(ws, method, params = {}) {
  const id = ++send.id;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const onMessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== id) return;
      ws.removeEventListener("message", onMessage);
      if (msg.error) reject(new Error(`${method}: ${msg.error.message}`));
      else resolve(msg.result);
    };
    ws.addEventListener("message", onMessage);
  });
}
send.id = 0;

async function waitForLoad(ws) {
  for (let i = 0; i < 60; i += 1) {
    const result = await send(ws, "Runtime.evaluate", {
      expression: "document.readyState",
      returnByValue: true,
    });
    if (result.result?.value === "complete") return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("page did not reach complete readyState");
}

async function evaluate(ws, expression) {
  const result = await send(ws, "Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Runtime.evaluate failed");
  }
  return result.result.value;
}

async function runCase(ws, viewport, path) {
  await send(ws, "Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: viewport.name === "mobile" ? 3 : 1,
    mobile: viewport.name === "mobile",
  });
  await send(ws, "Page.navigate", { url: `${BASE}${path}` });
  await waitForLoad(ws);
  await new Promise((resolve) => setTimeout(resolve, 250));

  const result = await evaluate(
    ws,
    `(() => {
      const candidates = [document.scrollingElement, ...document.querySelectorAll('main, [class*="overflow-y-auto"]')]
        .filter(Boolean);
      const unique = Array.from(new Set(candidates));
      const scrollables = unique.filter((el) => el.scrollHeight > el.clientHeight + 2);
      for (const el of scrollables) el.scrollTop = 0;
      const before = scrollables.map((el) => el.scrollTop);
      for (const el of scrollables) {
        const target = Math.min(180, el.scrollHeight - el.clientHeight);
        el.scrollTop = target;
      }
      const after = scrollables.map((el) => el.scrollTop);
      const changed = after.some((value, index) => value > before[index]);
      return {
        path: location.pathname,
        viewport: ${JSON.stringify(viewport.name)},
        documentScrollable: document.scrollingElement.scrollHeight > document.scrollingElement.clientHeight + 2,
        scrollableCount: scrollables.length,
        changed,
        maxDelta: Math.max(0, ...after.map((value, index) => value - before[index])),
        bodyOverflowY: getComputedStyle(document.body).overflowY,
        htmlMinHeight: getComputedStyle(document.documentElement).minHeight,
        main: Array.from(document.querySelectorAll('main')).map((el) => ({
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight,
          overflowY: getComputedStyle(el).overflowY,
        })),
      };
    })()`,
  );
  return result;
}

async function main() {
  const tab = await json(`${HOST}/json/new?${encodeURIComponent(`${BASE}/`)}`, {
    method: "PUT",
  });
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  await send(ws, "Page.enable");
  await send(ws, "Runtime.enable");

  const results = [];
  for (const viewport of viewports) {
    for (const path of pages) {
      results.push(await runCase(ws, viewport, path));
    }
  }
  ws.close();
  await fetch(`${HOST}/json/close/${tab.id}`).catch(() => {});

  let failed = false;
  for (const result of results) {
    const ok = result.changed || result.scrollableCount === 0;
    if (!ok) failed = true;
    console.log(
      `${ok ? "✓" : "✗"} ${result.viewport} ${result.path} scrollables=${result.scrollableCount} changed=${result.changed} maxDelta=${result.maxDelta}`,
    );
  }
  if (failed) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
