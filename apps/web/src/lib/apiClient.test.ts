import { doesNotMatch, match, ok, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const source = readFileSync(new URL("./apiClient.ts", import.meta.url), "utf8");
const tasksSource = readFileSync(
  new URL("./api/tasks.ts", import.meta.url),
  "utf8",
);
const storyboardsSource = readFileSync(
  new URL("./api/storyboards.ts", import.meta.url),
  "utf8",
);
const workflowsSource = readFileSync(
  new URL("./api/workflows.ts", import.meta.url),
  "utf8",
);
const posterStylesSource = readFileSync(
  new URL("./api/posterStyles.ts", import.meta.url),
  "utf8",
);

test("redeemCode sends an idempotency key generated from crypto.randomUUID with fallback", () => {
  match(source, /function createIdempotencyKey\(\): string/);
  match(source, /crypto\.randomUUID\(\)/);
  match(source, /return uuid\(\);/);
  match(source, /headers: \{ "Idempotency-Key": createIdempotencyKey\(\) \}/);
});

test("exportMyData retries once after refreshing a stale csrf token", () => {
  match(source, /async function exportApiErrorFromResponse\(res: Response\)/);
  match(source, /if \(res\.status === 403\)/);
  match(source, /err\.code !== "csrf_failed"/);
  match(source, /refreshCsrfToken\(\)\.catch\(\(\) => null\)/);
  match(source, /res = await doFetch\(fresh\)/);
});

test("apiClient preserves poster style exports through the focused module", () => {
  match(source, /export \* from "\.\/api\/posterStyles";/);
  doesNotMatch(source, /export interface PosterStyleItem/);
  match(posterStylesSource, /export interface PosterStyleItem/);
  match(posterStylesSource, /export function listPosterStyles/);
});

test("poster style requests reuse the shared HTTP helper", () => {
  match(posterStylesSource, /import \{ apiFetch \} from "\.\/http";/);
  match(
    posterStylesSource,
    /return apiFetch<PosterStyleGenerateOut>\("\/poster-styles\/generate"/,
  );
  doesNotMatch(posterStylesSource, /\bfetch\s*\(/);
});

test("apiClient preserves task, storyboard, and workflow exports through focused modules", () => {
  match(source, /export \* from "\.\/api\/tasks";/);
  match(source, /export \* from "\.\/api\/storyboards";/);
  match(source, /export \* from "\.\/api\/workflows";/);

  doesNotMatch(source, /export interface BackendGeneration/);
  doesNotMatch(source, /export interface StoryboardRun/);
  doesNotMatch(source, /export interface WorkflowRun/);
  doesNotMatch(source, /export function listStoryboards/);
  doesNotMatch(source, /export function listWorkflows/);

  match(tasksSource, /export interface BackendGeneration/);
  match(tasksSource, /export interface BackendCompletion/);
  match(tasksSource, /export interface BackendImageMeta/);
  match(storyboardsSource, /export interface StoryboardRun/);
  match(storyboardsSource, /export function assembleStoryboard/);
  match(workflowsSource, /export interface WorkflowRun/);
  match(workflowsSource, /export function completeWorkflowDelivery/);
});

test("focused storyboard and workflow requests reuse the shared HTTP helper", () => {
  match(storyboardsSource, /import \{ apiFetch \} from "\.\/http";/);
  match(workflowsSource, /import \{ apiFetch \} from "\.\/http";/);
  match(
    storyboardsSource,
    /return apiFetch<StoryboardListResponse>\(`\/storyboards\$\{suffix\}`\)/,
  );
  match(
    workflowsSource,
    /return apiFetch<WorkflowRunListResponse>\(`\/workflows\$\{suffix\}`\)/,
  );
  doesNotMatch(storyboardsSource, /\bfetch\s*\(/);
  doesNotMatch(workflowsSource, /\bfetch\s*\(/);
});

test("apiClient facade stays below 3000 lines", () => {
  const lineCount = source.trimEnd().split("\n").length;
  ok(lineCount < 3000, `apiClient.ts is ${lineCount} lines`);
});

test("apiClient and focused modules compile with the project TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../tsconfig.json", import.meta.url),
  );
  const rootNames = [
    "./apiClient.ts",
    "./api/tasks.ts",
    "./api/storyboards.ts",
    "./api/workflows.ts",
  ].map((relativePath) =>
    fileURLToPath(new URL(relativePath, import.meta.url)),
  );
  const rootNameSet = new Set(rootNames);

  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(
    config.error,
    undefined,
    config.error
      ? ts.flattenDiagnosticMessageText(config.error.messageText, "\n")
      : undefined,
  );
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(
    parsed.errors.length,
    0,
    ts.formatDiagnostics(parsed.errors, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );

  const program = ts.createProgram({
    rootNames,
    options: { ...parsed.options, incremental: false, noEmit: true },
  });
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .filter(
      (diagnostic) =>
        diagnostic.file == null || rootNameSet.has(diagnostic.file.fileName),
    );
  strictEqual(
    diagnostics.length,
    0,
    ts.formatDiagnostics(diagnostics, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );
});
