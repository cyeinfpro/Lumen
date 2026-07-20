import { match, ok, strictEqual } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const panelSource = readFileSync(
  new URL("./VideoProvidersPanel.tsx", import.meta.url),
  "utf8",
);
const domainSource = readFileSync(
  new URL("./videoProviderPanelDomain.ts", import.meta.url),
  "utf8",
);
const rulesSource = readFileSync(
  new URL("./videoProviderAssetRules.ts", import.meta.url),
  "utf8",
);
const source = `${panelSource}\n${domainSource}\n${rulesSource}`;
const queriesSource = [
  readFileSync(new URL("../../../lib/queries.ts", import.meta.url), "utf8"),
  readFileSync(
    new URL("../../../lib/queries/admin.ts", import.meta.url),
    "utf8",
  ),
].join("\n");
const rulesUrl = new URL("./videoProviderAssetRules.ts", import.meta.url);
const { evaluateVolcanoAssetCredentials } = (await import(
  rulesUrl.href
)) as typeof import("./videoProviderAssetRules");

test("volcano asset drafts use backend-compatible defaults", () => {
  match(source, /VOLCANO_DEFAULT_PROJECT_NAME = "default"/);
  match(source, /VOLCANO_DEFAULT_REGION = "cn-beijing"/);
  match(source, /function inferVolcanoRegion\(baseUrl: string\)/);
  match(
    source,
    /\^ark\\\.\(\[a-z0-9\]\+\(\?:-\[a-z0-9\]\+\)\*\)\\\.volces\\\.com\$/,
  );
  match(source, /resolvedVolcanoRegion\(item\.base_url, item\.region\)/);
  match(
    source,
    /project_name: item\.project_name\?\.trim\(\) \|\| VOLCANO_DEFAULT_PROJECT_NAME/,
  );
  match(
    source,
    /region: resolvedVolcanoRegion\(draft\.base_url, draft\.region\)/,
  );
  match(source, /draft\.region === previousInferredRegion/);
  match(
    source,
    /!previousInferredRegion &&\s*draft\.region === VOLCANO_DEFAULT_REGION/,
  );
});

test("volcano asset fields and saved readiness are visible only for volcano", () => {
  match(source, /draft\.kind === "volcano"/);
  match(source, /label="Access Key ID"/);
  match(source, /label="Secret Access Key"/);
  match(source, /label="ProjectName"/);
  match(source, /label="Region"/);
  match(source, /access_key_id_hint/);
  match(source, /secret_access_key_hint/);
  match(source, /asset_management_ready/);
});

test("provider rename preserves API and conditional asset-secret protection", () => {
  match(
    source,
    /draft\.kind === "volcano" \? volcanoDraftInput\(draft\) : null/,
  );
  match(source, /access_key_id: draft\.access_key_id\.trim\(\)/);
  match(source, /volcanoFields\?\.access_key_id/);
  match(source, /secret_access_key: draft\.secret_access_key\.trim\(\)/);
  match(source, /volcanoFields\?\.secret_access_key/);
  match(source, /Access Key ID 与 Secret Access Key 必须同时填写/);
  match(source, /将成对更新火山资产 Access Key ID 与 Secret Access Key/);
  match(
    source,
    /供应商重命名后需重新填写火山资产 Access Key ID 与 Secret Access Key/,
  );
  match(source, /供应商重命名后必须重新填写 API Key/);
  match(
    source,
    /draftWasRenamed\(draft\)[\s\S]*?!draft\.api_key\.trim\(\)[\s\S]*?severity: "error"/,
  );
  match(source, /evaluateVolcanoAssetCredentials\(\{/);
  match(source, /storedAccessKeyIdHint: stored\?\.access_key_id_hint/);
  match(source, /storedSecretAccessKeyHint: stored\?\.secret_access_key_hint/);
  match(source, /assetManagementReady: stored\?\.asset_management_ready/);
  match(source, /assetCredentialsRequireReplacement/);
  match(source, /label: "保存前需重填"/);
});

test("generation-only volcano rename does not invent an asset credential requirement", () => {
  const generationOnly = evaluateVolcanoAssetCredentials({
    renamed: true,
    accessKeyId: "",
    secretAccessKey: "",
  });
  const storedAssetConfig = evaluateVolcanoAssetCredentials({
    renamed: true,
    accessKeyId: "",
    secretAccessKey: "",
    storedAccessKeyIdHint: "ak-***",
  });
  const partialNewConfig = evaluateVolcanoAssetCredentials({
    renamed: false,
    accessKeyId: "ak-new",
    secretAccessKey: "",
  });
  const completeReplacement = evaluateVolcanoAssetCredentials({
    renamed: true,
    accessKeyId: "ak-new",
    secretAccessKey: "sk-new",
    assetManagementReady: true,
  });

  strictEqual(generationOnly.replacementRequired, false);
  strictEqual(generationOnly.error, null);
  strictEqual(storedAssetConfig.replacementRequired, true);
  strictEqual(storedAssetConfig.error, "rename_replacement");
  strictEqual(partialNewConfig.error, "incomplete");
  strictEqual(completeReplacement.error, null);
});

test("unchanged stored asset credentials remain protected when fields stay blank", () => {
  const result = evaluateVolcanoAssetCredentials({
    renamed: false,
    accessKeyId: "",
    secretAccessKey: "",
    storedAccessKeyIdHint: "ak-***",
    storedSecretAccessKeyHint: "sk-***",
    assetManagementReady: true,
  });

  strictEqual(result.replacementRequired, false);
  strictEqual(result.error, null);
  match(source, /key: renamed \? "" : item\.api_key_hint/);
  match(source, /accessKeyId: renamed \? "" : item\.access_key_id_hint/);
  match(source, /secretAccessKey: renamed \? "" : item\.secret_access_key_hint/);
});

test("video provider mutation commits returned data before invalidation", () => {
  match(
    queriesSource,
    /onSuccess:[\s\S]*?qc\.setQueryData\(qk\.videoProviders\(\), data\)[\s\S]*?qc\.invalidateQueries\(\{ queryKey: qk\.videoProviders\(\) \}\)/,
  );
});

test("provider secrets use isolated password-manager fields", () => {
  match(source, /name=\{`video-provider-\$\{draft\._key\}-api-key`\}/);
  match(
    source,
    /name=\{`video-provider-\$\{draft\._key\}-access-key-id`\}/,
  );
  match(
    source,
    /name=\{`video-provider-\$\{draft\._key\}-secret-access-key`\}/,
  );
  match(source, /autoComplete="new-password"/);
  match(source, /autoComplete=\{autoComplete\}/);
});

test("video provider panel files stay within architecture budgets", () => {
  const lines = (value: string) => value.trimEnd().split("\n").length;

  ok(lines(panelSource) <= 1995, `panel is ${lines(panelSource)} lines`);
  ok(lines(domainSource) <= 1500, `domain is ${lines(domainSource)} lines`);
  ok(lines(rulesSource) <= 1500, `rules are ${lines(rulesSource)} lines`);
});

test("video provider panel modules compile with the project TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../../../tsconfig.json", import.meta.url),
  );
  const rootNames = [
    "./VideoProvidersPanel.tsx",
    "./videoProviderPanelDomain.ts",
    "./videoProviderAssetRules.ts",
  ].map((relativePath) =>
    fileURLToPath(new URL(relativePath, import.meta.url)),
  );
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
