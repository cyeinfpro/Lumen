#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DEFAULT_FIXTURE,
  buildAssets,
  filterAssets,
  summarizeScenarios,
  targetAcceptance,
} from "./model.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const FIXTURE_PATH = join(HERE, "asset_fixture.html");
const PIXEL = Buffer.from(
  "R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
  "base64",
);

function parseArgs(argv) {
  const options = {
    chrome: null,
    count: DEFAULT_FIXTURE.count,
    effectiveType: "4g",
    headers: {},
    json: false,
    mobile: false,
    mode: "legacy",
    saveData: false,
    tileSelector: ".asset-tile",
    url: null,
  };
  if (process.env.LUMEN_WAVE3_BROWSER_HEADERS_JSON) {
    options.headers = JSON.parse(
      process.env.LUMEN_WAVE3_BROWSER_HEADERS_JSON,
    );
  }
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--chrome") options.chrome = argv[++index];
    else if (value === "--count") options.count = Number(argv[++index]);
    else if (value === "--effective-type") {
      options.effectiveType = argv[++index];
    } else if (value === "--json") options.json = true;
    else if (value === "--mobile") options.mobile = true;
    else if (value === "--mode") options.mode = argv[++index];
    else if (value === "--save-data") options.saveData = true;
    else if (value === "--tile-selector") {
      options.tileSelector = argv[++index];
    } else if (value === "--url") options.url = argv[++index];
    else throw new Error(`unknown argument: ${value}`);
  }
  if (!["legacy", "target"].includes(options.mode)) {
    throw new Error("--mode must be legacy or target");
  }
  if (options.url && options.tileSelector === ".asset-tile") {
    options.tileSelector = "[data-mounted-tile]";
  }
  return options;
}

function chromeCandidates(explicit) {
  return [
    explicit,
    process.env.LUMEN_WAVE3_CHROME,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
}

async function executablePath(explicit) {
  for (const candidate of chromeCandidates(explicit)) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next known Chrome/Chromium path.
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

function requestKind(pathname) {
  if (pathname.includes("/thumb")) return "thumb";
  if (pathname.includes("/preview")) return "preview";
  if (pathname.includes("/display")) return "display";
  if (pathname.includes("/binary")) return "binary";
  return "other";
}

async function fixtureServer(count, requestMetrics) {
  const html = await readFile(FIXTURE_PATH);
  const assets = buildAssets({ count });
  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    requestMetrics.total += 1;
    const kind = requestKind(url.pathname);
    requestMetrics.byKind[kind] = (requestMetrics.byKind[kind] ?? 0) + 1;
    if (url.pathname === "/") {
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": "text/html; charset=utf-8",
      });
      response.end(html);
      return;
    }
    if (url.pathname === "/fixture/assets") {
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": "application/json",
      });
      response.end(
        JSON.stringify({
          items: assets,
          scenario_counts: summarizeScenarios(assets),
        }),
      );
      return;
    }
    if (url.pathname === "/api/generations/feed") {
      requestMetrics.serverSearchQueries.push({
        pageSize: Number(url.searchParams.get("page_size") || 0),
        q: url.searchParams.get("q"),
      });
      const items = filterAssets(assets, url.searchParams.get("q"));
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": "application/json",
      });
      response.end(JSON.stringify({ items, next_cursor: null }));
      return;
    }
    if (
      url.pathname.startsWith("/img/") ||
      url.pathname.startsWith("/api/images/")
    ) {
      const writeImage = () => {
        if (url.searchParams.get("fail") === "1") {
          response.writeHead(404, { "cache-control": "no-store" });
          response.end();
          return;
        }
        response.writeHead(200, {
          "cache-control": "public,max-age=31536000,immutable",
          "content-length": PIXEL.length,
          "content-type": "image/gif",
        });
        response.end(PIXEL);
      };
      const delay = Number(url.searchParams.get("delay") || 0);
      if (delay > 0) setTimeout(writeImage, delay);
      else writeImage();
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

async function evaluate(client, expression) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const response = await client.send("Runtime.evaluate", {
        awaitPromise: true,
        expression,
        returnByValue: true,
      });
      if (response.exceptionDetails) {
        throw new Error(
          response.exceptionDetails.exception?.description ??
            response.exceptionDetails.text,
        );
      }
      return response.result.value;
    } catch (error) {
      const message = String(error?.message ?? error);
      const navigationRace =
        message.includes("Inspected target navigated or closed") ||
        message.includes("Execution context was destroyed") ||
        message.includes("Cannot find context");
      if (!navigationRace || attempt === 19) throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw new Error("runtime evaluation retry budget exhausted");
}

async function performanceMetrics(client) {
  const metrics = await client.send("Performance.getMetrics");
  return Object.fromEntries(
    metrics.metrics.map(({ name, value }) => [name, value]),
  );
}

async function runBrowser(options) {
  if (typeof WebSocket === "undefined") {
    return {
      reason: "Node runtime does not expose a WebSocket client",
      status: "gated",
    };
  }
  const chrome = await executablePath(options.chrome);
  if (!chrome) {
    return {
      reason: "Chrome/Chromium not found",
      required_environment: ["LUMEN_WAVE3_CHROME"],
      status: "gated",
    };
  }

  const requestMetrics = {
    byKind: {},
    serverSearchQueries: [],
    total: 0,
  };
  let localFixture = null;
  const fixtureBase =
    options.url ?? (localFixture = await fixtureServer(options.count, requestMetrics)).url;
  const fixtureUrl = new URL(fixtureBase);
  if (!options.url) {
    fixtureUrl.searchParams.set("mode", options.mode);
    fixtureUrl.searchParams.set("mobile", options.mobile ? "1" : "0");
  }
  const targetUrl = fixtureUrl.toString();
  const debugPort = await freePort();
  const userDataDir = await mkdtemp(join(tmpdir(), "lumen-wave3-chrome-"));
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
      binaryRequests: 0,
      displayRequests: 0,
      encodedBytes: 0,
      failed: 0,
      requests: 0,
      responses: 0,
      searchRequests: 0,
    };
    const requestCounts = new Map();
    const failedThumbCounts = new Map();
    let initialFeedRequests = 0;
    client.on("Network.requestWillBeSent", ({ request }) => {
      network.requests += 1;
      requestCounts.set(request.url, (requestCounts.get(request.url) ?? 0) + 1);
      if (request.url.includes("/binary")) network.binaryRequests += 1;
      if (request.url.includes("/display")) network.displayRequests += 1;
      if (request.url.includes("/api/generations/feed")) {
        if (new URL(request.url).searchParams.has("q")) {
          network.searchRequests += 1;
        } else {
          initialFeedRequests += 1;
        }
      }
    });
    client.on("Network.responseReceived", ({ response }) => {
      network.responses += 1;
      if (
        response.status >= 400 &&
        response.url.includes("/variants/thumb256")
      ) {
        failedThumbCounts.set(
          response.url,
          (failedThumbCounts.get(response.url) ?? 0) + 1,
        );
      }
    });
    client.on("Network.loadingFinished", ({ encodedDataLength }) => {
      network.encodedBytes += encodedDataLength ?? 0;
    });
    client.on("Network.loadingFailed", () => {
      network.failed += 1;
    });
    await Promise.all([
      client.send("HeapProfiler.enable"),
      client.send("Network.enable"),
      client.send("Page.enable"),
      client.send("Performance.enable"),
      client.send("Runtime.enable"),
    ]);
    await client.send("Emulation.setDeviceMetricsOverride", {
      deviceScaleFactor: options.mobile ? 3 : 1,
      height: options.mobile ? 844 : 900,
      mobile: options.mobile,
      width: options.mobile ? 390 : 1440,
    });
    const weakNetwork = options.effectiveType !== "4g";
    if (weakNetwork) {
      await client.send("Network.emulateNetworkConditions", {
        connectionType: options.effectiveType === "3g" ? "cellular3g" : "cellular2g",
        downloadThroughput: options.effectiveType === "3g" ? 192000 : 56000,
        latency: options.effectiveType === "3g" ? 180 : 450,
        offline: false,
        uploadThroughput: options.effectiveType === "3g" ? 96000 : 28000,
      });
    }
    const connectionOverride = `(() => {
      const connection = {
        effectiveType: ${JSON.stringify(options.effectiveType)},
        saveData: ${JSON.stringify(options.saveData)}
      };
      Object.defineProperty(Navigator.prototype, "connection", {
        configurable: true,
        get: () => connection
      });
    })();`;
    await client.send("Page.addScriptToEvaluateOnNewDocument", {
      source: connectionOverride,
    });
    const headers = { ...options.headers };
    const cookieHeaderKey = Object.keys(headers).find(
      (key) => key.toLowerCase() === "cookie",
    );
    let configuredCookies = 0;
    if (cookieHeaderKey) {
      const cookieHeader = String(headers[cookieHeaderKey] ?? "");
      delete headers[cookieHeaderKey];
      for (const pair of cookieHeader.split(";")) {
        const separator = pair.indexOf("=");
        if (separator <= 0) continue;
        const name = pair.slice(0, separator).trim();
        const value = pair.slice(separator + 1).trim();
        if (!name || !value) continue;
        const configured = await client.send("Network.setCookie", {
          name,
          url: targetUrl,
          value,
        });
        if (configured.success) configuredCookies += 1;
      }
    }
    if (options.saveData) headers["Save-Data"] = "on";
    if (Object.keys(headers).length > 0) {
      await client.send("Network.setExtraHTTPHeaders", { headers });
    }

    const loaded = client.once("Page.loadEventFired");
    await client.send("Page.navigate", { url: targetUrl });
    await loaded;
    await evaluate(
      client,
      `(async () => {
        const deadline = Date.now() + ${options.url ? 30000 : 10000};
        while (true) {
          const fixtureReady =
            document.documentElement.dataset.ready === "true";
          const targetReady =
            document.readyState === "complete" &&
            document.querySelector(${JSON.stringify(options.tileSelector)});
          if (${JSON.stringify(Boolean(options.url))} ? targetReady : fixtureReady) {
            break;
          }
          if (Date.now() > deadline) {
            throw new Error(${JSON.stringify(
              options.url
                ? "target page readiness timeout"
                : "fixture readiness timeout",
            )});
          }
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
        return true;
      })()`,
    );
    const initialFeedRequestsBeforeSearch = initialFeedRequests;
    const productSearchEvidence = options.url
      ? await evaluate(
          client,
          `(async () => {
            const response = await fetch(
              "/api/generations/feed?limit=50&q=${encodeURIComponent(
                DEFAULT_FIXTURE.searchQuery,
              )}",
              { credentials: "include" }
            );
            const payload = await response.json();
            return {
              matchedQuery: Array.isArray(payload.items)
                ? payload.items.some((item) =>
                    String(item.prompt ?? "")
                      .toLowerCase()
                      .includes(${JSON.stringify(
                        DEFAULT_FIXTURE.searchQuery,
                      )})
                  )
                : false,
              resultIds: Array.isArray(payload.items)
                ? payload.items.map((item) => item.id)
                : [],
              searchStatus: response.status
            };
          })()`,
        )
      : null;
    await client.send("HeapProfiler.collectGarbage");
    const initialPerformance = await performanceMetrics(client);

    const scrollSamples = await evaluate(
      client,
      `(async () => {
        const samples = [];
        const scrollingElement = document.scrollingElement;
        const candidates = [
          scrollingElement,
          ...Array.from(document.querySelectorAll("*")).filter((element) => {
            const style = getComputedStyle(element);
            return (
              /(auto|scroll)/.test(style.overflowY) &&
              element.scrollHeight > element.clientHeight + 4
            );
          })
        ].filter(Boolean);
        const scroller = candidates.sort(
          (a, b) =>
            (b.scrollHeight - b.clientHeight) -
            (a.scrollHeight - a.clientHeight)
        )[0] ?? scrollingElement;
        const setScroll = (top) => {
          if (scroller === scrollingElement) window.scrollTo(0, top);
          else {
            scroller.scrollTop = top;
            scroller.dispatchEvent(new Event("scroll"));
          }
        };
        const scrollTop = () =>
          scroller === scrollingElement ? window.scrollY : scroller.scrollTop;
        const sample = () => {
          samples.push({
            mounted: document.querySelectorAll(${JSON.stringify(
              options.tileSelector,
            )}).length,
            scrollY: scrollTop(),
            total: Number(
              document.querySelector("#stream-masonry")?.dataset.virtualTotal ?? 0
            )
          });
        };
        sample();
        if (${JSON.stringify(Boolean(options.url))}) {
          let stableRounds = 0;
          let previousTotal = -1;
          for (let round = 0; round < 40 && stableRounds < 5; round += 1) {
            setScroll(Math.max(0, scroller.scrollHeight - scroller.clientHeight));
            await new Promise((resolve) => setTimeout(resolve, 250));
            sample();
            const currentTotal = samples.at(-1)?.total ?? 0;
            stableRounds =
              currentTotal === previousTotal ? stableRounds + 1 : 0;
            previousTotal = currentTotal;
          }
        }
        for (let cycle = 0; cycle < 2; cycle += 1) {
          const height = Math.max(
            0,
            scroller.scrollHeight - scroller.clientHeight
          );
          for (let y = 0; y <= height; y += 1200) {
            setScroll(y);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            sample();
          }
          for (let y = height; y >= 0; y -= 1200) {
            setScroll(y);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            sample();
          }
        }
        setScroll(0);
        await new Promise((resolve) => setTimeout(resolve, 150));
        return samples;
      })()`,
    );
    await client.send("HeapProfiler.collectGarbage");
    const afterScrollPerformance = await performanceMetrics(client);

    const displayRequestsBeforeHover = network.displayRequests;
    const binaryRequestsBeforeOpen = network.binaryRequests;
    const genericHoverResult = options.url
      ? await evaluate(
          client,
          `(async () => {
            const tiles = Array.from(document.querySelectorAll(${JSON.stringify(
              options.tileSelector,
            )}));
            for (const tile of tiles) {
              tile.dispatchEvent(new PointerEvent("pointerover", {
                bubbles: true,
                pointerId: 1,
                pointerType: "mouse"
              }));
              tile.dispatchEvent(new PointerEvent("pointerout", {
                bubbles: true,
                pointerId: 1,
                pointerType: "mouse"
              }));
            }
            await new Promise((resolve) => setTimeout(resolve, 250));
            return { hovered: tiles.length };
          })()`,
        )
      : null;
    const hoverDisplayRequests =
      network.displayRequests - displayRequestsBeforeHover;

    const actionResult = await evaluate(
      client,
      `(async () => {
        if (window.__wave3Actions) {
          await window.__wave3Actions.stressPrewarm(500);
          await window.__wave3Actions.openVisibleTile();
          await new Promise((resolve) => setTimeout(resolve, 80));
          await window.__wave3Actions.closeLightbox();
          const resultIds = await window.__wave3Actions.search(${JSON.stringify(
            DEFAULT_FIXTURE.searchQuery,
          )});
          return { resultIds, status: "measured" };
        }

        const firstTile = document.querySelector(${JSON.stringify(
          options.tileSelector,
        )});
        firstTile?.querySelector('[role="button"]')?.click();
        await new Promise((resolve) => setTimeout(resolve, 80));
        document.dispatchEvent(new KeyboardEvent("keydown", {
          bubbles: true,
          key: "Escape"
        }));
        return {
          lightboxOpened: Boolean(firstTile),
          status: "measured"
        };
      })()`,
    );
    if (options.url && productSearchEvidence) {
      actionResult.resultIds = productSearchEvidence.resultIds;
      actionResult.searchStatus = productSearchEvidence.searchStatus;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
    await client.send("HeapProfiler.collectGarbage");
    const finalPerformance = await performanceMetrics(client);
    const page = await evaluate(
      client,
      `(() => {
        const resources = performance.getEntriesByType("resource");
        const metrics = window.__wave3Metrics ?? {};
        const brokenThumbs = Array.from(document.images).filter((image) =>
          image.complete &&
          image.naturalWidth === 0 &&
          image.srcset.includes("thumb256")
        ).length;
        return {
          diagnostics: {
            ...(metrics.diagnostics ?? {}),
            failedThumbStillInSrcSet:
              metrics.diagnostics?.failedThumbStillInSrcSet ?? brokenThumbs
          },
          domNodes: document.getElementsByTagName("*").length,
          firstInteractiveMs:
            performance.getEntriesByName("wave3:first-interactive")[0]?.startTime ??
            performance.getEntriesByType("navigation")[0]?.domInteractive ??
            null,
          imageCount: document.images.length,
          longTasks: metrics.longTasks ?? null,
          maxMountedTiles: Math.max(
            metrics.maxMountedTiles ?? 0,
            document.querySelectorAll(${JSON.stringify(options.tileSelector)}).length
          ),
          mountedTiles: document.querySelectorAll(${JSON.stringify(
            options.tileSelector,
          )}).length,
          resourceEntries: resources.length,
          search: metrics.search ?? null
        };
      })()`,
    );
    if (options.url) {
      page.diagnostics = {
        ...page.diagnostics,
        displayRequestsByReason: {
          ...(page.diagnostics?.displayRequestsByReason ?? {}),
          hover: hoverDisplayRequests,
        },
        gridBinaryRequests: binaryRequestsBeforeOpen,
        repeatedFailedThumbRequests: Array.from(
          failedThumbCounts.values(),
        ).reduce((total, count) => total + Math.max(0, count - 1), 0),
      };
      page.search = {
        loadedPagesBeforeSearch: initialFeedRequestsBeforeSearch,
        matchedQuery: productSearchEvidence?.matchedQuery ?? false,
        normalizedQuery: DEFAULT_FIXTURE.searchQuery,
        requestCount: network.searchRequests,
        resultIds: actionResult.resultIds ?? [],
      };
    }
    client.close();
    const heapInitial = initialPerformance.JSHeapUsedSize ?? null;
    const heapFinal = finalPerformance.JSHeapUsedSize ?? null;
    const heapGrowthPercent =
      heapInitial && heapFinal
        ? ((heapFinal - heapInitial) / heapInitial) * 100
        : null;
    const result = {
      actionResult,
      browser: {
        final: {
          jsHeapTotalBytes: finalPerformance.JSHeapTotalSize ?? null,
          jsHeapUsedBytes: heapFinal,
          layoutCount: finalPerformance.LayoutCount ?? null,
          nodes: finalPerformance.Nodes ?? null,
          recalcStyleCount: finalPerformance.RecalcStyleCount ?? null,
          taskDurationSeconds: finalPerformance.TaskDuration ?? null,
        },
        forcedGcHeapGrowthPercent: heapGrowthPercent,
        initial: {
          jsHeapTotalBytes: initialPerformance.JSHeapTotalSize ?? null,
          jsHeapUsedBytes: heapInitial,
          taskDurationSeconds: initialPerformance.TaskDuration ?? null,
        },
        postScroll: {
          jsHeapTotalBytes: afterScrollPerformance.JSHeapTotalSize ?? null,
          jsHeapUsedBytes: afterScrollPerformance.JSHeapUsedSize ?? null,
          taskDurationSeconds: afterScrollPerformance.TaskDuration ?? null,
        },
      },
      environment: {
        effectiveType: options.effectiveType,
        mobile: options.mobile,
        saveData: options.saveData,
      },
      mode: options.url ? "target_url" : `${options.mode}_synthetic_fixture`,
      network,
      page,
      productActions: options.url
        ? {
            ...genericHoverResult,
            hoverDisplayRequests,
          }
        : null,
      requestHeadersConfigured:
        configuredCookies > 0 || Object.keys(headers).length > 0,
      server: options.url ? null : requestMetrics,
      status: "measured",
      targetUrl,
      tileSelector: options.tileSelector,
      workload: {
        cycles: 2,
        prewarmCandidates: 500,
        scrollSamples: {
          count: scrollSamples.length,
          maxLoaded: Math.max(
            ...scrollSamples.map((sample) => sample.total ?? 0),
          ),
          maxMounted: Math.max(...scrollSamples.map((sample) => sample.mounted)),
          minMounted: Math.min(...scrollSamples.map((sample) => sample.mounted)),
        },
        searchQuery: DEFAULT_FIXTURE.searchQuery,
      },
    };
    result.page.maxMountedTiles = Math.max(
      result.page.maxMountedTiles ?? 0,
      result.workload.scrollSamples.maxMounted,
    );
    result.acceptance = targetAcceptance(
      result,
      options.mobile ? "mobile" : "desktop",
    );
    return result;
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
  process.stdout.write(
    `${JSON.stringify(result, null, options.json ? 0 : 2)}\n`,
  );
} catch (error) {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exitCode = 1;
}
