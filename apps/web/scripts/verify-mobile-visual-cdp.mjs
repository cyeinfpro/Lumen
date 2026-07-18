#!/usr/bin/env node

import { mkdir, writeFile } from "node:fs/promises";

import {
  fetchJsonWithTimeout,
  PAGE_IDENTITY_EXPRESSION,
  pageIdentityErrors,
} from "./cdp-page-validation.mjs";

const HOST = process.env.CDP_HOST ?? "http://127.0.0.1:9222";
const BASE = process.env.APP_BASE_URL ?? "http://localhost:3000";
const OUTPUT_DIR =
  process.env.MOBILE_UI_ARTIFACTS ?? "/tmp/lumen-mobile-ui";
const CDP_TIMEOUT_MS = 15_000;
const CDP_CLOSE_TIMEOUT_MS = 3_000;
const HTTP_TIMEOUT_MS = 5_000;

const pages = [
  "/",
  "/login",
  "/signup",
  "/reset-password",
  "/projects",
  "/projects/new",
  "/projects/apparel-model-showcase",
  "/projects/apparel-model-showcase/new",
  "/projects/storyboard",
  "/library",
  "/poster-styles",
  "/video",
  "/stream",
  "/settings/usage",
  "/settings/privacy",
  "/settings/prompts",
  "/me",
  "/admin",
  "/missing-mobile-route",
];

const viewports = [
  { name: "mobile-320", width: 320, height: 568, scale: 2 },
  { name: "mobile-375", width: 375, height: 812, scale: 3 },
  { name: "mobile-390", width: 390, height: 844, scale: 3 },
  { name: "mobile-430", width: 430, height: 932, scale: 3 },
  { name: "mobile-landscape", width: 844, height: 390, scale: 2 },
  { name: "desktop", width: 1280, height: 900, scale: 1 },
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
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        finish(reject, error);
        return;
      }
      if (message.id !== id) return;
      if (message.error) {
        finish(reject, new Error(`${method}: ${message.error.message}`));
      } else {
        finish(resolve, message.result);
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

async function waitForPage(ws) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const state = await evaluate(ws, "document.readyState");
    if (state === "complete") {
      await evaluate(
        ws,
        "document.fonts?.ready?.catch?.(() => undefined) ?? Promise.resolve()",
      );
      await new Promise((resolve) => setTimeout(resolve, 500));
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("page did not reach complete readyState");
}

function artifactName(viewport, path) {
  const route =
    path === "/" ? "home" : path.replace(/^\/|\/$/g, "").replaceAll("/", "__");
  return `${viewport.name}__${route || "home"}.png`;
}

async function inspectPage(ws) {
  return evaluate(
    ws,
    `(() => {
      const viewportWidth = window.innerWidth;
      const rootOverflow = Math.max(
        document.documentElement.scrollWidth,
        document.body?.scrollWidth ?? 0,
      ) - viewportWidth;
      const outliers = [];
      const nodes = document.querySelectorAll("body *");
      for (const element of nodes) {
        if (!(element instanceof HTMLElement || element instanceof SVGElement)) {
          continue;
        }
        if (element.closest('[aria-hidden="true"], [inert]')) continue;
        const style = getComputedStyle(element);
        if (
          style.display === "none" ||
          style.visibility === "hidden" ||
          Number(style.opacity) < 0.02 ||
          style.pointerEvents === "none"
        ) {
          continue;
        }
        const rect = element.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        const leftOverflow = Math.max(0, -rect.left);
        const rightOverflow = Math.max(0, rect.right - viewportWidth);
        if (leftOverflow <= 2 && rightOverflow <= 2) continue;
        const intentionalScroller =
          style.overflowX === "auto" ||
          style.overflowX === "scroll" ||
          element.closest('[class*="overflow-x-auto"], [class*="no-scrollbar"]');
        if (intentionalScroller) continue;
        outliers.push({
          tag: element.tagName.toLowerCase(),
          id: element.id,
          className:
            typeof element.className === "string"
              ? element.className.slice(0, 140)
              : "",
          leftOverflow: Math.round(leftOverflow),
          rightOverflow: Math.round(rightOverflow),
        });
        if (outliers.length >= 12) break;
      }
      return {
        path: location.pathname,
        title: document.title,
        rootOverflow: Math.round(rootOverflow),
        outliers,
      };
    })()`,
  );
}

async function main() {
  await mkdir(OUTPUT_DIR, { recursive: true });
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

    const failures = [];
    for (const viewport of viewports) {
      await send(ws, "Emulation.setDeviceMetricsOverride", {
        width: viewport.width,
        height: viewport.height,
        deviceScaleFactor: viewport.scale,
        mobile: viewport.name.startsWith("mobile"),
      });
      for (const path of pages) {
        const requestedUrl = new URL(path, BASE).href;
        const navigation = await send(ws, "Page.navigate", {
          url: requestedUrl,
        });
        await waitForPage(ws);
        const identity = await evaluate(ws, PAGE_IDENTITY_EXPRESSION);
        const identityErrors = [
          ...(navigation.errorText
            ? [`navigation failed: ${navigation.errorText}`]
            : []),
          ...pageIdentityErrors(requestedUrl, identity, {
            expectedStatuses:
              path === "/missing-mobile-route" ? [404] : [200],
          }),
        ];
        const inspection = await inspectPage(ws);
        const screenshot = await send(ws, "Page.captureScreenshot", {
          format: "png",
          fromSurface: true,
          captureBeyondViewport: false,
        });
        const fileName = artifactName(viewport, path);
        await writeFile(
          `${OUTPUT_DIR}/${fileName}`,
          Buffer.from(screenshot.data, "base64"),
        );
        const ok =
          identityErrors.length === 0 &&
          inspection.rootOverflow <= 2 &&
          inspection.outliers.length === 0;
        if (!ok) {
          failures.push({
            viewport: viewport.name,
            requestedPath: path,
            identity,
            identityErrors,
            ...inspection,
          });
        }
        console.log(
          `${ok ? "✓" : "✗"} ${viewport.name} ${path} -> ${identity.pathname} status=${identity.responseStatus} overflow=${inspection.rootOverflow} outliers=${inspection.outliers.length}`,
        );
      }
    }

    console.log(`Screenshots: ${OUTPUT_DIR}`);
    if (failures.length > 0) {
      console.error(JSON.stringify(failures, null, 2));
      throw new Error("mobile visual verification failed");
    }
  } finally {
    await closeCdpSession(tab, ws);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
