import {
  doesNotMatch,
  match,
  ok,
  strictEqual,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const source = readFileSync(new URL("./apiClient.ts", import.meta.url), "utf8");
const http = readFileSync(new URL("./api/http.ts", import.meta.url), "utf8");
const navigation = readFileSync(
  new URL("./auth/navigation.ts", import.meta.url),
  "utf8",
);

test("API compatibility facade delegates to typed clients with a standard default deadline", () => {
  ok(http.trimEnd().split("\n").length < 150);
  match(http, /queryClient\.get/);
  match(http, /commandClient\.request/);
  match(http, /uploadClient\.send/);
  match(http, /DEFAULT_API_TIMEOUT_MS/);
  doesNotMatch(http, /timeoutMs = null/);
  doesNotMatch(http, /globalThis\.fetch|window\.fetch\s*=/);
});

test("download and streaming prompt paths no longer own raw fetch calls", () => {
  const promptSource = readFileSync(
    new URL("./api/promptEnhancement.ts", import.meta.url),
    "utf8",
  );
  doesNotMatch(source, /\bfetch\s*\(/);
  doesNotMatch(promptSource, /\bfetch\s*\(/);
  match(source, /downloadClient\.postBlob\("\/me\/export"\)/);
  match(promptSource, /streamClient\.postJson\(path, body, signal\)/);
});

test("safe login navigation is centralized and preserves next with replace", () => {
  match(navigation, /export function safeAuthNextPath/);
  match(navigation, /window\.location\.replace\(currentLoginPath\(\)\)/);
  match(navigation, /encodeURIComponent\(next\)/);
  doesNotMatch(navigation, /location\.assign/);
});

test("apiClient preserves public endpoint exports", () => {
  for (const moduleName of [
    "tasks",
    "storyboards",
    "workflows",
    "posterWorkflows",
    "admin",
    "system",
    "billing",
    "memory",
    "conversations",
    "systemPrompts",
    "images",
    "account",
    "posterStyles",
  ]) {
    match(source, new RegExp(`export \\* from "\\./api/${moduleName}"`));
  }
});

test("API facade and new modules compile with the project TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../", import.meta.url));
  const configPath = fileURLToPath(new URL("../../tsconfig.json", import.meta.url));
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(config.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(parsed.errors.length, 0);
  const rootNames = [
    "./apiClient.ts",
    "./api/http.ts",
    "./api/transport.ts",
    "./api/queryClient.ts",
    "./api/commandClient.ts",
    "./api/downloadClient.ts",
    "./api/uploadClient.ts",
    "./api/longOperationClient.ts",
    "./api/streamClient.ts",
    "./api/csrf.ts",
    "./auth/navigation.ts",
    "./auth/authFailureCoordinator.ts",
    "./auth/identityPolicy.ts",
  ].map((relativePath) => fileURLToPath(new URL(relativePath, import.meta.url)));
  const program = ts.createProgram({
    rootNames,
    options: { ...parsed.options, incremental: false, noEmit: true },
  });
  const diagnostics = ts.getPreEmitDiagnostics(program);
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
