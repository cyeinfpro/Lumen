import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const here = path.dirname(fileURLToPath(import.meta.url));
const proxyPath = path.join(here, "proxy.ts");

function loadProxyModule() {
  const source = readFileSync(proxyPath, "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  });
  const mod = { exports: {} };
  const fakeNextServer = {
    NextResponse: {
      rewrite(url, init) {
        return { kind: "rewrite", url: String(url), init };
      },
      redirect(url) {
        return { kind: "redirect", url: String(url) };
      },
      next(init) {
        return { kind: "next", init };
      },
    },
  };
  const customRequire = (specifier) => {
    if (specifier === "next/server") return fakeNextServer;
    throw new Error(`Unexpected import: ${specifier}`);
  };
  new Function("require", "module", "exports", outputText)(
    customRequire,
    mod,
    mod.exports,
  );
  return mod.exports;
}

test("proxy matcher skips static font, service worker, and manifest assets", () => {
  const { config } = loadProxyModule();
  assert.ok(Array.isArray(config.matcher));
  const catchAll = config.matcher.find((entry) => entry.startsWith("/((?!"));
  assert.ok(catchAll, "expected negative-lookahead catch-all matcher");

  const routeRegex = new RegExp(`^${catchAll}$`);
  for (const excluded of [
    "/_next/static/chunks/main.js",
    "/_next/image",
    "/fonts/inter-var.woff2",
    "/favicon.ico",
    "/robots.txt",
    "/sw.js",
    "/manifest.webmanifest",
  ]) {
    assert.equal(
      routeRegex.test(excluded),
      false,
      `expected ${excluded} to bypass proxy catch-all matcher`,
    );
  }

  for (const included of ["/", "/agent", "/video", "/projects/canvas"]) {
    assert.equal(
      routeRegex.test(included),
      true,
      `expected ${included} to match proxy catch-all matcher`,
    );
  }
});

test("proxy preserves /api prefix-stripping and /events backend routing for local dev", () => {
  const previous = process.env.LUMEN_BACKEND_URL;
  process.env.LUMEN_BACKEND_URL = "http://127.0.0.1:8000";
  try {
    const { proxy } = loadProxyModule();
    const makeRequest = (pathname, search = "") => ({
      url: `http://localhost:3000${pathname}${search}`,
      headers: new Headers(),
      nextUrl: { pathname, search },
      cookies: { get: () => undefined },
    });

    const apiRewrite = proxy(makeRequest("/api/videos/v-1/binary", "?download=1"));
    assert.equal(apiRewrite.kind, "rewrite");
    assert.equal(apiRewrite.url, "http://127.0.0.1:8000/videos/v-1/binary?download=1");

    const eventsRewrite = proxy(makeRequest("/events", "?channels=user:u-1"));
    assert.equal(eventsRewrite.kind, "rewrite");
    assert.equal(eventsRewrite.url, "http://127.0.0.1:8000/events?channels=user:u-1");
  } finally {
    if (previous === undefined) {
      delete process.env.LUMEN_BACKEND_URL;
    } else {
      process.env.LUMEN_BACKEND_URL = previous;
    }
  }
});
