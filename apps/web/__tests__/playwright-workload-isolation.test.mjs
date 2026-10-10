import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = fileURLToPath(new URL("../", import.meta.url));
function loadConfig(name, cache = new Map()) {
  const filename = path.resolve(root, name + ".ts");
  if (cache.has(filename)) return cache.get(filename);
  const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const compiledModule = { exports: {} };
  const require = (id) => {
    if (id === "@playwright/test") return { defineConfig: (config) => config };
    assert.ok(id.startsWith("./"), "Unexpected configuration dependency: " + id);
    return { default: loadConfig(id.slice(2), cache), __esModule: true };
  };
  new Function("require", "module", "exports", "process", compiled)(require, compiledModule, compiledModule.exports, {
    env: { LUMEN_QA_CHROMIUM: "/existing/chromium", LUMEN_QA_WEBKIT: "/existing/webkit" },
  });
  cache.set(filename, compiledModule.exports.default);
  return compiledModule.exports.default;
}

test("default browser gate excludes only explicitly dedicated scale and touch workloads", () => {
  const config = loadConfig("playwright.config");
  assert.deepEqual(config.testIgnore, ["**/canvas-scale-performance.spec.ts", "**/canvas-touch-preview.spec.ts"]);
  assert.equal(config.testMatch, undefined);
  assert.equal(config.projects.length, 7);
  for (const project of config.projects) {
    assert.deepEqual(project.testIgnore, [...config.testIgnore, "**/agent-live.spec.ts"]);
  }
  assert.ok(config.projects.some((project) => project.use.hasTouch === false));
});

test("dedicated scale config opts back in with the same two engines and observation budget", () => {
  const config = loadConfig("playwright.canvas-scale.config");
  assert.deepEqual(config.testIgnore, []);
  assert.equal(config.testMatch, "**/canvas-scale-performance.spec.ts");
  assert.deepEqual(config.projects.map((project) => project.name), ["ux-desktop-light", "ux-webkit-reduced"]);
  assert.equal(config.timeout, 360_000);
  assert.equal(config.retries, 0);
  assert.equal(config.workers, 1);
});

test("dedicated touch config retains native-touch WebKit and does not inherit default exclusions", () => {
  const config = loadConfig("playwright.canvas-touch.config");
  assert.deepEqual(config.testIgnore, []);
  assert.equal(config.testMatch, "**/canvas-touch-preview.spec.ts");
  assert.equal(config.projects.length, 1);
  assert.equal(config.projects[0].use.browserName, "webkit");
  assert.equal(config.projects[0].use.hasTouch, true);
});
