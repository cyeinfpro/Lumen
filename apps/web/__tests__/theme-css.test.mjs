import { deepEqual, equal, ok } from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import postcss from "postcss";
import { compileThemeCss } from "./helpers/theme-css.mjs";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(webRoot, "../..");
const cssPath = resolve(webRoot, "src/app/globals.css");

function selectors(css) {
  const result = new Set();
  postcss.parse(css).walkRules((rule) => result.add(rule.selector));
  return result;
}

function utilitySelector(className) {
  return `.${className.replace(/[^a-zA-Z0-9_-]/g, "\\$&")}`;
}

test("Tailwind compiles real application classes identically from repo and web cwd", () => {
  const fromWeb = compileThemeCss(webRoot);
  const fromRepo = compileThemeCss(webRoot, repoRoot);
  const rules = selectors(fromWeb);
  equal(fromRepo, fromWeb, "CSS bytes and selectors must not depend on cwd");
  for (const [path, candidate] of [
    ["src/app/layout.tsx", "min-h-[100dvh]"],
    ["src/features/agent/ui/AgentComposer.tsx", "z-[var(--z-composer)]"],
    ["src/components/ui/primitives/Button.tsx", "text-[var(--link-fg)]"],
  ]) {
    ok(readFileSync(resolve(webRoot, path), "utf8").includes(candidate), path);
    ok(rules.has(utilitySelector(candidate)), `${path}: missing ${candidate}`);
  }
  ok(rules.has(".hljs"), "external highlight.js CSS import is preserved");
  console.info(`Theme CSS: ${Buffer.byteLength(fromWeb)} bytes, ${rules.size} selectors, identical in both cwd configurations`);
});

test("explicit source covers all four src roots but excludes config and cross-package noise", () => {
  const fixture = mkdtempSync(resolve(tmpdir(), "lumen-theme-source-"));
  try {
    const fixtureWeb = resolve(fixture, "apps/web");
    const fixtureCss = resolve(fixtureWeb, "src/app/globals.css");
    mkdirSync(dirname(fixtureCss), { recursive: true });
    symlinkSync(resolve(webRoot, "node_modules"), resolve(fixtureWeb, "node_modules"), "dir");
    copyFileSync(cssPath, fixtureCss);
    copyFileSync(resolve(webRoot, "src/app/markdown.css"), resolve(fixtureWeb, "src/app/markdown.css"));
    const candidates = ["h-[317px]", "w-[319px]", "min-h-[323px]", "max-w-[331px]"];
    for (const [index, root] of ["app", "features", "shared", "components"].entries()) {
      const path = resolve(fixtureWeb, "src", root, "source-probe.tsx");
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, `export const Probe = () => <div className="${candidates[index]}" />;`);
    }
    writeFileSync(resolve(fixtureWeb, "eslint.config.mjs"), 'export default "w-[997px]";');
    mkdirSync(resolve(fixture, "packages/noise"), { recursive: true });
    writeFileSync(resolve(fixture, "packages/noise/index.tsx"), 'export const Noise = () => <div className="h-[991px]" />;');
    const fromWeb = compileThemeCss(webRoot, fixtureWeb, fixtureCss);
    const fromRepo = compileThemeCss(webRoot, fixture, fixtureCss);
    equal(fromRepo, fromWeb);
    const rules = selectors(fromWeb);
    for (const candidate of candidates) ok(rules.has(utilitySelector(candidate)), candidate);
    for (const candidate of ["w-[997px]", "h-[991px]"]) {
      equal(rules.has(utilitySelector(candidate)), false, `out-of-scope class ${candidate}`);
    }
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
});

function themeTokens(theme, systemLight = false) {
  const tokens = new Map();
  postcss.parse(readFileSync(cssPath, "utf8")).walkRules((rule) => {
    const isSystem = rule.parent.type === "atrule" && rule.parent.name === "media";
    if (isSystem && !(systemLight && rule.parent.params === "(prefers-color-scheme: light)")) return;
    const applies = isSystem
      ? !["theme-dark", "dark"].includes(theme)
      : rule.selectors.some((selector) => selector === ":root" || selector === `.${theme}`);
    if (applies) rule.walkDecls(/^--/, (decl) => tokens.set(decl.prop, decl.value));
  });
  return tokens;
}

function resolveToken(tokens, name) {
  const value = tokens.get(name);
  ok(value, `missing ${name}`);
  return value.replace(/var\((--[\w-]+)\)/g, (_, alias) => resolveToken(tokens, alias));
}

function luminance(hex) {
  ok(/^#[0-9a-f]{6}$/i.test(hex), `expected opaque RGB: ${hex}`);
  const [r, g, b] = hex.slice(1).match(/../g).map((part) => {
    const value = parseInt(part, 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return r * 0.2126 + g * 0.7152 + b * 0.0722;
}

function contrast(fg, bg) {
  const values = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

test("link text passes AA on every main surface in dark, light, and system themes", () => {
  let minimum = Infinity;
  for (const theme of ["", "theme-dark", "dark", "theme-light"]) {
    for (const systemLight of [false, true]) {
      const tokens = themeTokens(theme, systemLight);
      for (const surface of ["--bg-0", "--bg-1", "--bg-2", "--bg-3", "--surface-canvas", "--surface-chrome", "--surface-panel", "--surface-raised", "--surface-overlay"]) {
        const bg = resolveToken(tokens, surface);
        const ratio = contrast(resolveToken(tokens, "--link-fg"), bg);
        minimum = Math.min(minimum, ratio);
        ok(ratio >= 4.5, `${theme || "system"}/${systemLight}/${surface}: ${ratio}`);
        ok(contrast(resolveToken(tokens, "--fg-muted-aa"), bg) >= 4.5, `${surface}: muted text`);
        ok(contrast(resolveToken(tokens, "--focus-outline"), bg) >= 3, `${surface}: focus`);
      }
      for (const bg of ["--button-primary-bg", "--button-primary-bg-hover"]) {
        ok(contrast(resolveToken(tokens, "--accent-on"), resolveToken(tokens, bg)) >= 4.5);
      }
      ok(resolveToken(tokens, "--link-fg") !== resolveToken(tokens, "--info"));
      ok(resolveToken(tokens, "--link-fg") !== resolveToken(tokens, "--accent"));
    }
  }
  console.info(`Minimum link contrast: ${minimum.toFixed(2)}:1`);
  for (const theme of ["theme-dark", "dark", "theme-light"]) {
    deepEqual(themeTokens(theme, false), themeTokens(theme, true), `${theme} ignores system preference`);
  }
});
