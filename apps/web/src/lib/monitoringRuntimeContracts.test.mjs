import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

// Execute the actual TypeScript sources with SDK-only mocks. No telemetry/network,
// SDK installation, Next server, or production state is involved in these contracts.
const sourceRoot = new URL("../", import.meta.url);
function evaluate(relative, env, requireModule, globals = {}) {
  const source = readFileSync(new URL(relative, sourceRoot), "utf8");
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: relative,
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, {
    exports, require: requireModule, process: { env }, URL, ...globals,
  }, { filename: relative });
  return exports;
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise((resolve) => setImmediate(resolve));
const request = { method: "GET", path: "/invite/secret?token=secret", headers: { authorization: "secret" } };
const context = { routerKind: "App Router", routePath: "/invite/[token]", routeType: "render" };

function server(env, overrides = {}) {
  const imports = [], calls = [];
  let options;
  const sdk = {
    init(value) { options = value; calls.push(["init"]); },
    withScope(callback) {
      calls.push(["scope"]);
      callback({
        setContext(...args) { calls.push(["context", ...args]); },
        setTransactionName(...args) { calls.push(["transaction", ...args]); },
      });
    },
    captureException(...args) { calls.push(["capture", ...args]); },
    async flush(timeout) { calls.push(["flush", timeout]); },
    ...overrides,
  };
  const api = evaluate("instrumentation.ts", env, (name) => {
    imports.push(name);
    if (overrides.importError) throw new Error("SDK unavailable");
    assert.ok(["@sentry/node", "@sentry/vercel-edge"].includes(name));
    return sdk;
  });
  return { api, imports, calls, options: () => options };
}

function browser(env = { NEXT_PUBLIC_SENTRY_DSN: "https://public@example.invalid/1" }, overrides = {}) {
  const imports = [], calls = [], listeners = new Map();
  let options, now = 1000;
  const client = {};
  const sdk = {
    init(value) { options = value; calls.push(["init"]); },
    browserTracingIntegration(value) { calls.push(["integration", value]); return {}; },
    getClient() { return client; },
    addBreadcrumb(value) { calls.push(["breadcrumb", value]); },
    startBrowserTracingNavigationSpan(actualClient, value) {
      assert.equal(actualClient, client); calls.push(["span", value]);
    },
    ...overrides,
  };
  const window = {
    location: { href: "https://app.example.invalid/start" },
    addEventListener(type, callback) { listeners.set(type, callback); },
  };
  const route = evaluate("lib/monitoringRoute.ts", {}, () => { throw new Error("unexpected helper import"); });
  const api = evaluate("instrumentation-client.ts", env, (name) => {
    if (name === "./lib/monitoringRoute") return route;
    imports.push(name);
    assert.equal(name, "@sentry/react");
    if (overrides.importError) throw new Error("SDK unavailable");
    return sdk;
  }, { window, Date: { now: () => now } });
  return { api, imports, calls, listeners, window, options: () => options,
    advance(ms) { now += ms; } };
}

test("DSN disabled: neither server hook nor client navigation loads an SDK", async () => {
  const s = server({});
  await s.api.register();
  await s.api.onRequestError(new Error("example"), request, context);
  assert.deepEqual(s.imports, []);
  const b = browser({});
  b.api.onRouterTransitionStart("/share/private", "push");
  await settle();
  assert.deepEqual(b.imports, []);
  assert.equal(b.listeners.size, 0);
  assert.deepEqual(b.calls, []);
});

for (const [runtime, expected] of [["nodejs", "@sentry/node"], ["edge", "@sentry/vercel-edge"]]) {
  test(`server selects only ${expected} and captures scoped request errors before awaited flush`, async () => {
    let finishFlush;
    const s = server({ SENTRY_DSN: "server-dsn", NEXT_RUNTIME: runtime }, {
      flush(timeout) {
        s.calls.push(["flush", timeout]);
        return new Promise((resolve) => { finishFlush = resolve; });
      },
    });
    await s.api.register();
    assert.equal(s.options().dsn, "server-dsn");
    const error = new Error("example");
    let resolved = false;
    const pending = s.api.onRequestError(error, request, context).then(() => { resolved = true; });
    await settle();
    assert.equal(resolved, false);
    assert.deepEqual(s.imports, [expected, expected]);
    assert.deepEqual(s.calls.map(([name]) => name), ["init", "scope", "context", "transaction", "capture", "flush"]);
    assert.deepEqual(plain(s.calls[2]), ["context", "nextjs", {
      router_kind: "App Router", router_path: "/invite/[token]", route_type: "render", request_method: "GET",
    }]);
    assert.deepEqual(s.calls[3], ["transaction", "GET /invite/[token]"]);
    assert.equal(s.calls[4][1], error);
    assert.deepEqual(plain(s.calls[4][2]), { mechanism: {
      handled: false, type: "auto.function.nextjs.on_request_error",
    } });
    assert.deepEqual(s.calls[5], ["flush", 2000]);
    assert.ok(!JSON.stringify(s.calls).includes("secret"));
    finishFlush(true);
    await pending;
    assert.equal(resolved, true);
  });
}

test("server monitoring failures do not reject register or request hook", async () => {
  for (const overrides of [
    { importError: true },
    { init() { throw new Error("init failed"); } },
    { captureException() { throw new Error("capture failed"); } },
    { flush() { return Promise.reject(new Error("flush failed")); } },
  ]) {
    const s = server({ SENTRY_DSN: "server-dsn" }, overrides);
    await assert.doesNotReject(s.api.register());
    await assert.doesNotReject(s.api.onRequestError(new Error("example"), request, context));
  }
});

test("client enables tracing without replay and shares one lazy SDK import", async () => {
  const b = browser();
  await settle();
  assert.deepEqual(plain(b.calls.find(([name]) => name === "integration")[1]), { instrumentNavigation: false });
  assert.equal(b.options().replaysSessionSampleRate, 0);
  assert.equal(b.options().replaysOnErrorSampleRate, 0);
  b.api.onRouterTransitionStart("/gallery?private=query#secret", "push");
  b.api.onRouterTransitionStart("/settings", "replace");
  await settle();
  assert.deepEqual(b.imports, ["@sentry/react"]);
  const spans = b.calls.filter(([name]) => name === "span").map(([, value]) => value);
  assert.deepEqual(plain(spans).map((span) => [span.name, span.attributes["navigation.type"]]),
    [["/gallery", "push"], ["/settings", "replace"]]);
});

test("popstate plus Next traverse hook emits once inside dedup window and again after it", async () => {
  const b = browser();
  await settle();
  assert.equal(typeof b.listeners.get("popstate"), "function");
  b.window.location.href = "https://app.example.invalid/share/super-secret?token=hidden#hidden";
  b.listeners.get("popstate")();
  b.api.onRouterTransitionStart(b.window.location.href, "traverse");
  await settle();
  let spans = b.calls.filter(([name]) => name === "span");
  assert.equal(spans.length, 1);
  assert.equal(spans[0][1].name, "/share/[token]");
  b.advance(201);
  b.listeners.get("popstate")();
  await settle();
  spans = b.calls.filter(([name]) => name === "span");
  assert.equal(spans.length, 2);
  assert.ok(!JSON.stringify(b.calls).includes("super-secret"));
  assert.ok(!JSON.stringify(b.calls).includes("hidden"));
});

test("navigation breadcrumbs and spans template sensitive routes", async () => {
  const b = browser();
  await settle();
  for (const route of ["invite", "reset-password", "share"]) {
    b.api.onRouterTransitionStart(`/${route}/private-token?password=hidden#hidden`, "push");
    await settle();
  }
  const spans = b.calls.filter(([name]) => name === "span");
  const breadcrumbs = b.calls.filter(([name]) => name === "breadcrumb");
  assert.deepEqual(spans.map(([, value]) => value.name), ["/invite/[token]", "/reset-password/[token]", "/share/[token]"]);
  assert.deepEqual(breadcrumbs.map(([, value]) => value.data.to), spans.map(([, value]) => value.name));
  assert.ok(!JSON.stringify(b.calls).includes("private-token"));
  assert.ok(!JSON.stringify(b.calls).includes("hidden"));
});

test("client import/init/navigation failures are swallowed; no client emits nothing", async () => {
  for (const overrides of [
    { importError: true },
    { init() { throw new Error("init failed"); } },
    { getClient() { throw new Error("client failed"); } },
    { addBreadcrumb() { throw new Error("breadcrumb failed"); } },
    { startBrowserTracingNavigationSpan() { throw new Error("span failed"); } },
  ]) {
    const b = browser(undefined, overrides);
    assert.doesNotThrow(() => b.api.onRouterTransitionStart("/gallery", "push"));
    await settle(); // node:test treats an unhandled async rejection as a failure.
  }
  const b = browser(undefined, { getClient() { return undefined; } });
  b.api.onRouterTransitionStart("/gallery", "push");
  await settle();
  assert.equal(b.calls.filter(([name]) => name === "span" || name === "breadcrumb").length, 0);
});
