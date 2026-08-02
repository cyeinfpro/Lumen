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

test("video generation create takes the idempotency key per call, not module state", () => {
  // 幂等键必须按提交操作由调用方显式传入:模块级全局会跨请求/并发/改参数重提
  // 共享同一 key,后端指纹不同返回 409。key 的复用/释放决策在
  // video-create-idempotency.ts(行为测试见 video-create-idempotency.test.ts)。
  const createCall = source.indexOf("export function createVideoGeneration");
  ok(createCall >= 0);
  match(source, /options\.idempotency_key \?\?\s*createIdempotencyKey\(\)/);
  // 模块级待定 key 已彻底移除,不得复活。
  doesNotMatch(source, /pendingVideoCreateIdempotencyKey/);
  doesNotMatch(source, /let pendingVideoCreate/);
});

test("cookie-changing auth flows notify other tabs before accepting an identity", () => {
  const loginResponse = source.indexOf("const loginResponse");
  const loginNotification = source.indexOf(
    "notifyAuthSessionChanged();",
    loginResponse,
  );
  const loginIdentity = source.indexOf("await getMe()", loginResponse);

  ok(loginResponse >= 0);
  ok(loginNotification > loginResponse);
  ok(loginIdentity > loginNotification);
  match(source, /auth\/signup[\s\S]*?notifyAuthSessionChanged/);
  match(source, /auth\/signup\/byok[\s\S]*?notifyAuthSessionChanged/);
  match(source, /export async function logout[\s\S]*?notifyAuthSessionChanged/);
  const logoutRequest = source.indexOf('apiFetchNoContent("/auth/logout"');
  const logoutNotification = source.indexOf(
    "notifyAuthSessionChanged();",
    logoutRequest,
  );
  ok(logoutNotification > logoutRequest);
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
    "./auth/sessionChangeBus.ts",
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
