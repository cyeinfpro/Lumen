import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import postcss from "postcss";
import { compileThemeCss } from "./helpers/theme-css.mjs";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const candidate = "[@media(orientation:landscape)_and_(max-height:480px)]:max-h-[clamp(44px,calc(100dvh_-_264px),144px)]";

test("Agent textarea reserves notification space only in short landscape without shrinking touch controls", () => {
  const source = readFileSync(resolve(webRoot, "src/features/agent/ui/AgentComposer.tsx"), "utf8");
  assert.ok(source.includes(candidate));
  assert.match(source, /max-h-36 min-h-11[^"]*overflow-y-auto/u);
  const selector = "." + candidate.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  const declarations = [];
  postcss.parse(compileThemeCss(webRoot)).walkRules((rule) => {
    if (rule.selector !== selector) return;
    rule.walkDecls("max-height", (declaration) => {
      const media = [];
      for (let parent = declaration.parent; parent; parent = parent.parent) {
        if (parent.type === "atrule" && parent.name === "media") media.push(parent.params);
      }
      declarations.push({ value: declaration.value.replace(/\s+/gu, ""), media });
    });
  });
  assert.equal(declarations.length, 1, "the real Tailwind build emits the responsive limit");
  assert.match(declarations[0].media.join(" "), /orientation:\s*landscape/u);
  assert.match(declarations[0].media.join(" "), /max-height:\s*480px/u);
  assert.equal(declarations[0].value, "clamp(44px,100dvh-264px,144px)");
});
