#!/usr/bin/env node

import {
  fetchJsonWithTimeout,
  PAGE_IDENTITY_EXPRESSION,
  pageIdentityErrors,
} from "./cdp-page-validation.mjs";

const HOST = process.env.CDP_HOST ?? "http://127.0.0.1:9222";
const BASE = process.env.APP_BASE_URL ?? "http://localhost:3000";
const CDP_TIMEOUT_MS = 15_000;
const CDP_CLOSE_TIMEOUT_MS = 3_000;
const HTTP_TIMEOUT_MS = 5_000;

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

function send(ws, method, params = {}) {
  const id = ++send.id;
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timeout);
      ws.removeEventListener("message", onMessage);
      ws.removeEventListener("close", onClose);
      ws.removeEventListener("error", onError);
    };
    const finish = (callback, value) => {
      cleanup();
      callback(value);
    };
    const onMessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (error) {
        finish(reject, error);
        return;
      }
      if (msg.id !== id) return;
      if (msg.error) {
        finish(reject, new Error(`${method}: ${msg.error.message}`));
      } else {
        finish(resolve, msg.result);
      }
    };
    const onClose = () =>
      finish(reject, new Error(`${method}: CDP socket closed`));
    const onError = () =>
      finish(reject, new Error(`${method}: CDP socket error`));
    const timeout = setTimeout(
      () => finish(reject, new Error(`${method}: timed out`)),
      CDP_TIMEOUT_MS,
    );
    ws.addEventListener("message", onMessage);
    ws.addEventListener("close", onClose, { once: true });
    ws.addEventListener("error", onError, { once: true });
    try {
      ws.send(JSON.stringify({ id, method, params }));
    } catch (error) {
      finish(reject, error);
    }
  });
}
send.id = 0;

async function waitForSocketOpen(ws) {
  await new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timeout);
      ws.removeEventListener("open", onOpen);
      ws.removeEventListener("close", onClose);
      ws.removeEventListener("error", onError);
    };
    const onOpen = () => {
      cleanup();
      resolve();
    };
    const onClose = () => {
      cleanup();
      reject(new Error("CDP socket closed before opening"));
    };
    const onError = () => {
      cleanup();
      reject(new Error("CDP socket failed to open"));
    };
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error("CDP socket open timed out"));
    }, CDP_TIMEOUT_MS);
    ws.addEventListener("open", onOpen, { once: true });
    ws.addEventListener("close", onClose, { once: true });
    ws.addEventListener("error", onError, { once: true });
  });
}

async function closeCdpSession(tab, ws) {
  if (tab?.id) {
    await fetch(`${HOST}/json/close/${encodeURIComponent(tab.id)}`, {
      signal: AbortSignal.timeout(CDP_CLOSE_TIMEOUT_MS),
    }).catch((error) => {
      console.warn(`Failed to close CDP target ${tab.id}: ${error}`);
    });
  }
  if (
    ws &&
    (ws.readyState === WebSocket.CONNECTING ||
      ws.readyState === WebSocket.OPEN)
  ) {
    try {
      ws.close();
    } catch (error) {
      console.warn(`Failed to close CDP socket: ${error}`);
    }
  }
}

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
  const requestedUrl = new URL(path, BASE).href;
  const navigation = await send(ws, "Page.navigate", {
    url: requestedUrl,
  });
  await waitForLoad(ws);
  await new Promise((resolve) => setTimeout(resolve, 250));
  const identity = await evaluate(ws, PAGE_IDENTITY_EXPRESSION);
  const identityErrors = [
    ...(navigation.errorText
      ? [`navigation failed: ${navigation.errorText}`]
      : []),
    ...pageIdentityErrors(requestedUrl, identity),
  ];

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
  return {
    requestedPath: path,
    identity,
    identityErrors,
    ...result,
  };
}

async function main() {
  let tab;
  let ws;
  try {
    tab = await fetchJsonWithTimeout(
      `${HOST}/json/new?${encodeURIComponent(`${BASE}/`)}`,
      { method: "PUT" },
      { timeoutMs: HTTP_TIMEOUT_MS },
    );
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await waitForSocketOpen(ws);
    await send(ws, "Page.enable");
    await send(ws, "Runtime.enable");

    const results = [];
    for (const viewport of viewports) {
      for (const path of pages) {
        results.push(await runCase(ws, viewport, path));
      }
    }

    let failed = false;
    for (const result of results) {
      const ok =
        result.identityErrors.length === 0 &&
        (result.changed || result.scrollableCount === 0);
      if (!ok) failed = true;
      console.log(
        `${ok ? "✓" : "✗"} ${result.viewport} ${result.requestedPath} -> ${result.identity.pathname} status=${result.identity.responseStatus} scrollables=${result.scrollableCount} changed=${result.changed} maxDelta=${result.maxDelta}`,
      );
      if (result.identityErrors.length > 0) {
        console.error(`  ${result.identityErrors.join("; ")}`);
      }
    }
    if (failed) throw new Error("scroll verification failed");
  } finally {
    await closeCdpSession(tab, ws);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
