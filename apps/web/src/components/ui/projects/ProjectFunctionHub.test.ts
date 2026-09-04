import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  ok,
} from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(
  new URL("./ProjectFunctionHub.tsx", import.meta.url),
  "utf8",
);
const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
const sourceFile = ts.createSourceFile(
  "ProjectFunctionHub.tsx",
  source,
  ts.ScriptTarget.ES2022,
  true,
  ts.ScriptKind.TSX,
);

function standaloneFunction<T>(name: string): T {
  const declaration = sourceFile.statements.find(
    (statement): statement is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(statement) && statement.name?.text === name,
  );
  ok(declaration, `missing ${name}`);
  const output = ts.transpileModule(
    `${declaration.getText(sourceFile)}\nmodule.exports.${name} = ${name};`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = { exports: {} as Record<string, unknown> };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports[name] as T;
}

test("feature and history links resolve only to implemented pages", () => {
  const hrefs = Array.from(
    source.matchAll(/(?:primaryHref|secondaryHref):\s*"([^"]+)"/g),
    (entry) => entry[1],
  ).sort();

  deepEqual(hrefs, [
    "/poster-styles",
    "/projects/apparel-model-showcase",
    "/projects/apparel-model-showcase/new",
    "/projects/canvas",
    "/projects/canvas/new",
    "/projects/poster-design/new",
    "/projects/storyboard",
  ]);
  for (const href of hrefs) {
    ok(
      existsSync(resolve(webRoot, "src/app", href.slice(1), "page.tsx")),
      `missing page for ${href}`,
    );
  }

  match(
    source,
    /href="\/projects\/apparel-model-showcase"[\s\S]*?服饰历史/,
  );
  doesNotMatch(source, /全部历史|href:\s*"#|href=\{?"#/);
});

test("recent project links are type-specific and unknown types stay inert", () => {
  const projectHref = standaloneFunction<
    (item: { id: string; type: string }) => string | null
  >("projectHref");

  equal(
    projectHref({ id: "apparel/1", type: "apparel_model_showcase" }),
    "/projects/apparel%2F1",
  );
  equal(
    projectHref({ id: "poster/1", type: "poster_design" }),
    "/projects/poster%2F1",
  );
  equal(
    projectHref({ id: "board/1", type: "storyboard" }),
    "/projects/storyboard/board%2F1",
  );
  equal(projectHref({ id: "unknown/1", type: "unknown" }), null);
  match(source, /href \? \([\s\S]*?<Link\s+href=\{href\}/);
  match(source, /href \? \([\s\S]*?: \([\s\S]*?暂不支持/);
});

test("project hub has one accessible page heading at each breakpoint", () => {
  equal((source.match(/<h1\b/g) ?? []).length, 2);
  match(source, /<ProjectMobileTopBar title="创作工作流"/);
  match(source, /<h1 className="sr-only md:hidden">创作工作流<\/h1>/);
  match(
    source,
    /<div className="hidden md:block">[\s\S]*?<header className="page-header">[\s\S]*?<h1 className="type-page-title text-\[var\(--fg-0\)\]">/,
  );
  doesNotMatch(source, /<header className="page-header[^\"]*hidden/);
});

test("recent cards render honest API-backed preview and state fields", () => {
  match(source, /const previewSrc = productThumbSrc\(item\)/);
  match(source, /data-preview-state=\{previewSrc \? "available" : "empty"\}/);
  match(source, /暂无预览/);
  match(source, /data-project-status=\{item\.status\}/);
  match(source, /item\.type === "storyboard" \? STORYBOARD_STATUS_LABEL : STATUS_LABEL/);
  match(source, /data-project-progress[\s\S]*?\{stageLabel\}/);
  match(source, /<ProjectProgressRing value=\{item\.completion_percent\} \/>/);
  match(source, /role="progressbar"/);
  match(source, /aria-valuenow=\{value\}/);
  match(source, /data-project-progress-ring/);
  match(source, /conic-gradient\(var\(--accent\)/);
  match(source, /\{value\}%/);
  match(source, /item\.current_step/);
  match(source, /STORYBOARD_STAGES\.map\(\(stage\) => \[stage\.id, stage\.label\]\)/);
  match(source, /item\.next_action/);
  match(source, /item\.output_count > 0/);
  match(source, /<time dateTime=\{item\.updated_at\}/);
  match(source, /formatRelativeTime\(item\.updated_at\)/);
  doesNotMatch(source, /progress_pct|Math\.round/);
});

test("workflow and recent-project cards share the motion-safe V2 surface", () => {
  equal((source.match(/surface-card-v2/g) ?? []).length, 2);
  doesNotMatch(source, /hover:-translate-y/);
  match(source, /group-hover:scale-\[1\.02\] motion-reduce:transform-none/);
});

test("recent loading, error, empty, and status tones remain explicit", () => {
  match(source, /loading \? \([\s\S]*?加载中/);
  match(source, /error \? \([\s\S]*?最近项目加载失败/);
  match(source, /items\.length === 0 \? \([\s\S]*?暂无最近项目/);

  const statusTone = standaloneFunction<(status: string) => string>(
    "projectStatusTone",
  );
  match(statusTone("completed"), /success/);
  match(statusTone("failed"), /danger/);
  match(statusTone("running"), /accent/);
  match(statusTone("running"), /fg-0/);
  match(statusTone("in_progress"), /accent/);
  match(statusTone("needs_review"), /warning/);
  match(statusTone("draft"), /border-subtle/);
});
