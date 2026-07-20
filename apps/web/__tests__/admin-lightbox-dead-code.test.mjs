import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(webRoot, relativePath), "utf8");
}

test("video provider drafts cannot enable Veo or save the Omni placeholder", () => {
  const source = read("src/app/admin/_panels/VideoProvidersPanel.tsx");

  assert.match(
    source,
    /function videoProviderKindCanBeEnabled[\s\S]*?return kind !== "veo";/,
  );
  assert.match(
    source,
    /enabled: normalizeVideoProviderEnabled\(item\.kind, item\.enabled\)/,
  );
  assert.match(
    source,
    /function veoPresetPatch[\s\S]*?kind: "veo",[\s\S]*?enabled: false/,
  );
  assert.match(
    source,
    /disabled=\{!videoProviderKindCanBeEnabled\(draft\.kind\)\}/,
  );
  assert.match(
    source,
    /enabled: normalizeVideoProviderEnabled\(draft\.kind, draft\.enabled\)/,
  );
  assert.match(
    source,
    /function isOmniFlashPlaceholderBaseUrl[\s\S]*?"api\.example\.com"/,
  );
  assert.match(
    source,
    /isOmniFlashPlaceholderBaseUrl\(draft\.kind, (?:draft\.base_url|baseUrl)\)/,
  );
});

test("admin panels contain no proxy draft state or unreachable update group", () => {
  const providers = read("src/app/admin/_panels/ProvidersPanel.tsx");
  const settings = read("src/app/admin/_panels/SettingsPanel.tsx");

  assert.doesNotMatch(providers, /\b(proxyDrafts|ProxyDraft|toProxyDraft)\b/);
  assert.match(providers, /proxies=\{serverProxies\}/);

  assert.doesNotMatch(settings, /\|\s*"update"/);
  assert.doesNotMatch(settings, /\b(?:group|id): "update"/);
  assert.match(settings, /"update\.use_proxy_pool"/);
  assert.match(settings, /"update\.proxy_name"/);
});

test("desktop lightbox scopes transient and async state to the active image", () => {
  const source = read(
    "src/components/ui/lightbox/DesktopLightboxController.tsx",
  );

  assert.match(
    source,
    /const imageStateKey = `\$\{lightbox\.imageId \?\? ""\}\\n/,
  );
  assert.match(
    source,
    /activeImageStateKeyRef\.current !== imageStateKey/,
  );
  assert.match(source, /const sourceImageKey = imageStateKey;/);
  assert.match(
    source,
    /activeImageStateKeyRef\.current !== sourceImageKey/,
  );
  assert.match(source, /const downloadSeqRef = useRef\(0\);/);
  assert.match(source, /const shareSeqRef = useRef\(0\);/);
  assert.match(
    source,
    /activeImageStateKeyRef\.current === operationKey[\s\S]*?operationSeq/,
  );
});

test("queries exports all have at least one external named reference", () => {
  const queriesPath = path.join(webRoot, "src/lib/queries.ts");
  const queriesSource = fs.readFileSync(queriesPath, "utf8");
  const queriesFile = ts.createSourceFile(
    queriesPath,
    queriesSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const exportedNames = [];

  for (const statement of queriesFile.statements) {
    const modifiers = ts.canHaveModifiers(statement)
      ? ts.getModifiers(statement)
      : undefined;
    if (!modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
      continue;
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name)) {
          exportedNames.push(declaration.name.text);
        }
      }
    } else if ("name" in statement && statement.name && ts.isIdentifier(statement.name)) {
      exportedNames.push(statement.name.text);
    }
  }

  const referencedNames = new Set();
  const sourceFiles = [];
  const extensions = new Set([
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
  ]);

  function walk(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name === ".next") continue;
      const absolutePath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        walk(absolutePath);
      } else if (extensions.has(path.extname(entry.name))) {
        sourceFiles.push(absolutePath);
      }
    }
  }

  walk(webRoot);

  for (const sourcePath of sourceFiles) {
    if (sourcePath === queriesPath) continue;
    const source = fs.readFileSync(sourcePath, "utf8");
    const sourceFile = ts.createSourceFile(
      sourcePath,
      source,
      ts.ScriptTarget.Latest,
      true,
      sourcePath.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );

    for (const statement of sourceFile.statements) {
      if (!ts.isImportDeclaration(statement) && !ts.isExportDeclaration(statement)) {
        continue;
      }
      const specifier =
        statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)
          ? statement.moduleSpecifier.text
          : "";
      const importsQueries =
        specifier === "@/lib/queries" ||
        specifier === "./queries" ||
        specifier.endsWith("/lib/queries");
      if (!importsQueries) continue;

      if (
        ts.isImportDeclaration(statement) &&
        statement.importClause?.namedBindings &&
        ts.isNamedImports(statement.importClause.namedBindings)
      ) {
        for (const element of statement.importClause.namedBindings.elements) {
          referencedNames.add((element.propertyName ?? element.name).text);
        }
      }
      if (
        ts.isExportDeclaration(statement) &&
        statement.exportClause &&
        ts.isNamedExports(statement.exportClause)
      ) {
        for (const element of statement.exportClause.elements) {
          referencedNames.add((element.propertyName ?? element.name).text);
        }
      }
    }
  }

  const unreferenced = exportedNames.filter((name) => !referencedNames.has(name));
  assert.deepEqual(unreferenced, []);

  const removedHooks = [
    "useMyUsageQuery",
    "useAdminUsersQuery",
    "useMySharesQuery",
    "usePublicShareQuery",
    "useRevokeShareMutation",
    "useListMessagesQuery",
    "useDeleteStoryboardMutation",
    "usePatchStoryboardAssetMutation",
    "useCreatePosterStyleMutation",
  ];
  for (const name of removedHooks) {
    assert.equal(exportedNames.includes(name), false, `${name} must stay deleted`);
  }
});
