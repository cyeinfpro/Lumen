import {
  doesNotMatch,
  equal,
  match,
  ok,
} from "node:assert/strict";
import {
  existsSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const srcRoot = fileURLToPath(new URL("../", import.meta.url));

function source(relativePath: string): string {
  return readFileSync(path.join(srcRoot, relativePath), "utf8");
}

function productionSources(root: string): string[] {
  const files: string[] = [];
  const visit = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const target = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(target);
      } else if (
        /\.[cm]?[jt]sx?$/.test(entry.name) &&
        !/\.(?:test|spec)\.[cm]?[jt]sx?$/.test(entry.name)
      ) {
        files.push(target);
      }
    }
  };
  visit(root);
  return files;
}

test("realtime, generation, and assets expose real public feature entries", () => {
  const expected = {
    assets: [
      "./api/queries",
      "./model/prewarmScheduler",
      "./containers/DesktopAssetStream",
    ],
    generation: ["./model/generationState", "./model/lifecycleEvents"],
    realtime: ["./model/useSSE", "./model/useLumenRealtime"],
  } as const;
  for (const [feature, exports] of Object.entries(expected)) {
    const index = source(`features/${feature}/index.ts`);
    for (const target of exports) match(index, new RegExp(target.replace("/", "\\/")));
  }
});

test("legacy shims are deleted after feature migration", () => {
  for (const legacyPath of [
    "components/ui/shell/DesktopStream.tsx",
    "components/ui/shell/MobileStream.tsx",
    "components/ui/stream",
    "lib/imagePreload.ts",
    "lib/queries/stream.ts",
    "lib/sse",
    "lib/useSSE.ts",
    "store/chat/generationSlice.ts",
    "store/chatGenerationEvents.ts",
  ]) {
    equal(existsSync(path.join(srcRoot, legacyPath)), false, legacyPath);
  }
});

test("composition roots consume feature public entries", () => {
  for (const page of ["app/assets/page.tsx", "app/stream/page.tsx"]) {
    const pageSource = source(page);
    match(pageSource, /@\/features\/assets/);
    doesNotMatch(pageSource, /apiFetch|useStreamFeedQuery|GenerationMasonry/);
  }
  match(source("components/SSEProvider.tsx"), /@\/features\/realtime/);
  match(source("store/useChatStore.ts"), /@\/features\/generation/);
});

test("features never deep-import another feature", () => {
  const featureRoot = path.join(srcRoot, "features");
  for (const file of productionSources(featureRoot)) {
    const relative = path.relative(featureRoot, file).split(path.sep).join("/");
    const owner = relative.split("/")[0];
    const fileSource = readFileSync(file, "utf8");
    for (const imported of fileSource.matchAll(
      /@\/features\/([^/"']+)(\/[^"']+)?/g,
    )) {
      ok(
        imported[1] === owner || imported[2] === undefined,
        `${relative} deep-imports ${imported[0]}`,
      );
    }
  }
});

test("shared realtime is the only runtime registry owner", () => {
  const hook = source("features/realtime/model/useSSE.ts");
  const registry = source("shared/realtime/runtimeRegistry.ts");
  doesNotMatch(hook, /new Map/);
  match(registry, /const runtimes = new Map<string, RealtimeRuntime>\(\)/);
  match(hook, /acquireRealtimeRuntime/);
  match(hook, /releaseRealtimeRuntime/);
});
