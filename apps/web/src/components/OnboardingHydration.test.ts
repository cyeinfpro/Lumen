import { equal, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./Onboarding.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX,
    esModuleInterop: true,
  },
}).outputText;

type OnboardingProps = { onPick: (text: string, mode: string) => void; loading?: boolean };
function renderOnboarding(hydrated: boolean, loading: boolean): string {
  const moduleRecord = { exports: {} as { Onboarding: React.ComponentType<OnboardingProps> } };
  const container = ({ children }: { children: React.ReactNode }) => React.createElement("section", null, children);
  runInNewContext(compiled, {
    module: moduleRecord,
    exports: moduleRecord.exports,
    require(id: string) {
      if (id === "react") return {
        ...React,
        useSyncExternalStore(_subscribe: unknown, client: () => boolean, server: () => boolean) {
          return hydrated ? client() : server();
        },
      };
      if (id === "@/store/useChatStore") return {
        useChatStore: (selector: (state: { setComposerExpanded: () => void }) => unknown) =>
          selector({ setComposerExpanded() {} }),
      };
      if (id === "react/jsx-runtime") return require(id);
      if (id === "framer-motion") return { motion: { section: container }, useReducedMotion: () => false };
      if (id === "lucide-react") return { ArrowRight: () => null, ChevronDown: () => null };
      if (id === "next/image") return { __esModule: true, default: () => null };
      if (id === "@/lib/motion") return { DURATION: { normal: 0 }, EASE: { develop: [0, 0, 1, 1] } };
      if (id === "@/components/ui/primitives/Button") return {
        Button: ({ children, disabled }: { children: React.ReactNode; disabled: boolean }) =>
          React.createElement("button", { disabled }, children),
      };
      throw new Error(`Unexpected import: ${id}`);
    },
  });
  return renderToStaticMarkup(React.createElement(moduleRecord.exports.Onboarding, { loading, onPick() {} }));
}

for (const hydrated of [false, true]) {
  for (const loading of [false, true]) {
    test(`studio controls are inert until hydration and loading finish (${hydrated}, ${loading})`, () => {
      const html = renderOnboarding(hydrated, loading);
      const buttons = html.match(/<button\b[^>]*>/gu) ?? [];
      equal(buttons.length, 8, "start, four presets and three conversation starters must stay present");
      equal(buttons.filter((button) => /\bdisabled=""/u.test(button)).length, hydrated && !loading ? 0 : 8);
      match(html, /今天想创作什么？/u);
    });
  }
}

test("caption typography keeps semantic color utilities authoritative", () => {
  const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
  const caption = css.match(/(?<![\w(:])\.type-caption\s*\{([^}]+)\}/u);
  equal(Boolean(caption), true);
  equal(/(?:^|;)\s*color:/u.test(caption![1]), false);
  match(css, /:where\(\.type-caption\)\s*\{\s*color:\s*var\(--fg-2\)/u);
});
