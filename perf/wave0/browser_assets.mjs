#!/usr/bin/env node

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const FIXTURE_PATH = join(HERE, "asset_fixture.html");

function parseArgs(argv) {
  let headers = {};
  if (process.env.LUMEN_WAVE0_BROWSER_HEADERS_JSON) {
    headers = JSON.parse(process.env.LUMEN_WAVE0_BROWSER_HEADERS_JSON);
  }
  const options = {
    count: 1000,
    headers,
    json: false,
    tileSelector: ".asset-tile",
    url: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--json") options.json = true;
    else if (value === "--count") options.count = Number(argv[++index]);
    else if (value === "--url") options.url = argv[++index];
    else if (value === "--tile-selector") options.tileSelector = argv[++index];
    else if (value === "--chrome") options.chrome = argv[++index];
  }
  return options;
}

function chromeCandidates(explicit) {
  return [
    explicit,
    process.env.LUMEN_WAVE0_CHROME,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
}

async function executablePath(explicit) {
  const { access } = await import("node:fs/promises");
  for (const candidate of chromeCandidates(explicit)) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Continue to the next known location.
    }
  }
  return null;
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address()));
  });
}

function processExit(child, timeoutMs = 3000) {
  if (child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function removeWithRetry(path) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      await rm(path, { force: true, recursive: true });
      return;
    } catch (error) {
      if (attempt === 4) throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
}

async function freePort() {
  const server = createServer();
  const address = await listen(server);
  const port = address.port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function fixtureServer(count) {
  const html = await readFile(FIXTURE_PATH);
  const pixel = Buffer.from(
    "R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
    "base64",
  );
  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/") {
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": "text/html; charset=utf-8",
      });
      response.end(html);
      return;
    }
    if (url.pathname.startsWith("/img/")) {
      if (url.searchParams.get("fail") === "1") {
        response.writeHead(404, { "cache-control": "no-store" });
        response.end();
        return;
      }
      response.writeHead(200, {
        "cache-control": "public,max-age=31536000,immutable",
        "content-length": pixel.length,
        "content-type": "image/gif",
      });
      response.end(pixel);
      return;
    }
    response.writeHead(404);
    response.end();
  });
  const address = await listen(server);
  return {
    close: () => new Promise((resolve) => server.close(resolve)),
    url: `http://127.0.0.1:${address.port}/?count=${count}`,
  };
}

async function waitForJson(url, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch {
      // Chrome is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timed out waiting for ${url}`);
}

class CdpClient {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.socket = new WebSocket(url);
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", (message) => {
      const payload = JSON.parse(message.data);
      if (payload.id) {
        const pending = this.pending.get(payload.id);
        if (!pending) return;
        this.pending.delete(payload.id);
        if (payload.error) pending.reject(new Error(payload.error.message));
        else pending.resolve(payload.result);
        return;
      }
      for (const listener of this.listeners.get(payload.method) ?? []) {
        listener(payload.params);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { reject, resolve });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  once(method) {
    return new Promise((resolve) => {
      const listener = (params) => {
        this.listeners.set(
          method,
          (this.listeners.get(method) ?? []).filter((item) => item !== listener),
        );
        resolve(params);
      };
      this.listeners.set(method, [
        ...(this.listeners.get(method) ?? []),
        listener,
      ]);
    });
  }

  on(method, listener) {
    this.listeners.set(method, [
      ...(this.listeners.get(method) ?? []),
      listener,
    ]);
  }

  close() {
    this.socket.close();
  }
}

async function runBrowser(options) {
  if (typeof WebSocket === "undefined") {
    return {
      status: "gated",
      reason: "Node runtime does not expose a WebSocket client",
    };
  }
  const chrome = await executablePath(options.chrome);
  if (!chrome) {
    return {
      status: "gated",
      reason: "Chrome/Chromium not found",
      required_environment: ["LUMEN_WAVE0_CHROME"],
    };
  }

  let localFixture = null;
  const targetUrl =
    options.url ?? (localFixture = await fixtureServer(options.count)).url;
  const debugPort = await freePort();
  const userDataDir = await mkdtemp(join(tmpdir(), "lumen-wave0-chrome-"));
  const chromeProcess = spawn(
    chrome,
    [
      "--headless=new",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-sync",
      "--hide-scrollbars",
      "--no-first-run",
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${userDataDir}`,
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    await waitForJson(`http://127.0.0.1:${debugPort}/json/version`);
    const created = await fetch(
      `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(targetUrl)}`,
      { method: "PUT" },
    ).then((response) => response.json());
    const client = new CdpClient(created.webSocketDebuggerUrl);
    await client.open();
    const network = {
      encodedBytes: 0,
      failed: 0,
      requests: 0,
      responses: 0,
    };
    client.on("Network.requestWillBeSent", () => {
      network.requests += 1;
    });
    client.on("Network.responseReceived", () => {
      network.responses += 1;
    });
    client.on("Network.loadingFinished", ({ encodedDataLength }) => {
      network.encodedBytes += encodedDataLength ?? 0;
    });
    client.on("Network.loadingFailed", () => {
      network.failed += 1;
    });
    await Promise.all([
      client.send("Page.enable"),
      client.send("Network.enable"),
      client.send("Performance.enable"),
    ]);
    if (Object.keys(options.headers).length > 0) {
      await client.send("Network.setExtraHTTPHeaders", {
        headers: options.headers,
      });
    }

    const loaded = client.once("Page.loadEventFired");
    await client.send("Page.navigate", { url: targetUrl });
    await loaded;
    await new Promise((resolve) => setTimeout(resolve, 500));
    await client.send("Runtime.evaluate", {
      awaitPromise: true,
      expression: `(async () => {
        const height = Math.max(
          document.body.scrollHeight,
          document.documentElement.scrollHeight
        );
        for (let y = 0; y <= height; y += 900) {
          window.scrollTo(0, y);
          await new Promise((resolve) => requestAnimationFrame(resolve));
        }
        window.scrollTo(0, 0);
        await new Promise((resolve) => setTimeout(resolve, 500));
        return true;
      })()`,
      returnByValue: true,
    });
    const runtime = await client.send("Runtime.evaluate", {
      expression: `(() => {
        const resources = performance.getEntriesByType("resource");
        const navigation = performance.getEntriesByType("navigation")[0];
        return {
          dom_nodes: document.getElementsByTagName("*").length,
          images: document.images.length,
          mounted_tiles: document.querySelectorAll(${JSON.stringify(
            options.tileSelector,
          )}).length,
          resource_entries: resources.length,
          binary_requests: resources.filter((entry) =>
            entry.name.includes("/binary")
          ).length,
          first_interactive_ms:
            performance.getEntriesByName("wave0:first-interactive")[0]?.startTime ??
            navigation?.domInteractive ??
            null,
          fixture: window.__wave0Metrics ?? null,
        };
      })()`,
      returnByValue: true,
    });
    const metrics = await client.send("Performance.getMetrics");
    const metricMap = Object.fromEntries(
      metrics.metrics.map(({ name, value }) => [name, value]),
    );
    client.close();
    return {
      status: "measured",
      mode: options.url ? "target_url" : "synthetic_contract_fixture",
      url: targetUrl,
      request_headers_configured: Object.keys(options.headers).length > 0,
      tile_selector: options.tileSelector,
      network,
      browser: {
        js_heap_used_bytes: metricMap.JSHeapUsedSize ?? null,
        js_heap_total_bytes: metricMap.JSHeapTotalSize ?? null,
        nodes: metricMap.Nodes ?? null,
        layout_count: metricMap.LayoutCount ?? null,
        recalc_style_count: metricMap.RecalcStyleCount ?? null,
        task_duration_seconds: metricMap.TaskDuration ?? null,
      },
      page: runtime.result.value,
    };
  } finally {
    chromeProcess.kill("SIGTERM");
    await processExit(chromeProcess);
    await localFixture?.close();
    await removeWithRetry(userDataDir);
  }
}

const options = parseArgs(process.argv.slice(2));
try {
  const result = await runBrowser(options);
  const rendered = JSON.stringify(result, null, options.json ? 0 : 2);
  process.stdout.write(`${rendered}\n`);
} catch (error) {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 1;
}
