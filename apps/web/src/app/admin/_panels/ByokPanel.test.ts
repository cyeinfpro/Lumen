import { deepStrictEqual, ok, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const modelUrl = new URL("./ByokPanel.model.ts", import.meta.url);
const {
  detectMode,
  EMPTY_SUPPLIER,
  retentionStateFor,
  supplierDraftToCreateBody,
  togglePurpose,
  validateBaseUrl,
} = (await import(modelUrl.href)) as typeof import("./ByokPanel.model");

const moduleUrls = [
  new URL("./ByokPanel.tsx", import.meta.url),
  modelUrl,
  new URL("./ByokPanel.settings.tsx", import.meta.url),
  new URL("./ByokPanel.shared.tsx", import.meta.url),
  new URL("./ByokPanel.suppliers.tsx", import.meta.url),
];

test("BYOK modes preserve the original preset toggle combinations", () => {
  const base = {
    fallback_to_admin_provider: false,
    validation_model: "gpt-5.4",
    validation_timeout_ms: 15000,
    pending_token_ttl_seconds: 900,
    retention_hide_enabled: true,
    retention_hide_days: 3,
    retention_delete_enabled: false,
    retention_delete_days: 7,
  };

  strictEqual(
    detectMode({
      ...base,
      mode_enabled: false,
      byok_signup_enabled: false,
      byok_signup_bypasses_allowlist: false,
    }),
    "off",
  );
  strictEqual(
    detectMode({
      ...base,
      mode_enabled: true,
      byok_signup_enabled: false,
      byok_signup_bypasses_allowlist: false,
    }),
    "bind_only",
  );
  strictEqual(
    detectMode({
      ...base,
      mode_enabled: true,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: false,
    }),
    "key_first",
  );
  strictEqual(
    detectMode({
      ...base,
      mode_enabled: true,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: true,
    }),
    "fully_open",
  );
  strictEqual(
    detectMode({
      ...base,
      mode_enabled: false,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: false,
    }),
    null,
  );
});

test("BYOK validation and draft mapping preserve existing behavior", () => {
  strictEqual(validateBaseUrl(""), "必填");
  strictEqual(validateBaseUrl("ftp://example.com"), "必须是 http(s)");
  strictEqual(
    validateBaseUrl("https://user:secret@example.com"),
    "URL 不能包含账号密码",
  );
  strictEqual(validateBaseUrl("https://api.example.com"), null);

  deepStrictEqual(togglePurpose(["chat"], "chat"), ["chat"]);
  deepStrictEqual(togglePurpose(["chat", "image"], "chat"), ["image"]);
  deepStrictEqual(togglePurpose(["chat"], "embedding"), [
    "chat",
    "embedding",
  ]);

  const body = supplierDraftToCreateBody({
    ...EMPTY_SUPPLIER,
    probe_key: "temporary-secret",
  });
  ok(!("probe_key" in body));
  strictEqual(body.public_signup_enabled, true);
  deepStrictEqual(body.purposes, ["chat", "image"]);
});

test("BYOK retention validation compares effective enabled windows", () => {
  const state = retentionStateFor(
    { retention_hide_days: 10, retention_delete_days: 5 },
    undefined,
    {
      mode_enabled: true,
      byok_signup_enabled: true,
      byok_signup_bypasses_allowlist: false,
      fallback_to_admin_provider: false,
      validation_model: "gpt-5.4",
      validation_timeout_ms: 15000,
      pending_token_ttl_seconds: 900,
      retention_hide_enabled: true,
      retention_hide_days: 10,
      retention_delete_enabled: true,
      retention_delete_days: 5,
    },
  );

  deepStrictEqual(state, { hideDays: 10, deleteDays: 5, invalid: true });
});

test("BYOK production modules stay within the requested line budget", () => {
  for (const url of moduleUrls) {
    const source = readFileSync(url, "utf8");
    const lineCount = source.trimEnd().split("\n").length;
    ok(lineCount <= 800, `${fileURLToPath(url)} is ${lineCount} lines`);
  }
});

test("BYOK panel modules compile under the web TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../../../tsconfig.json", import.meta.url),
  );
  const rootNames = moduleUrls.map((url) => fileURLToPath(url));
  const rootNameSet = new Set(rootNames);
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(config.error, undefined);
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(parsed.errors.length, 0);
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
