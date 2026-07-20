import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

import {
  fetchJsonWithTimeout,
  pageIdentityErrors,
} from "../scripts/cdp-page-validation.mjs";
import { collectArchitectureFindings } from "../scripts/check-architecture.mjs";
import { getGitChangeScope } from "../scripts/git-change-scope.mjs";
import {
  auditHitAreaSource,
  findMobileDialogIssues,
} from "../scripts/jsx-quality-analysis.mjs";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(testDir, "..");

function read(relativePath) {
  return readFileSync(path.join(webRoot, relativePath), "utf8");
}

function git(cwd, args) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

test("hit-area audit cannot be bypassed by imports, role=button, or dynamic classes", () => {
  const findings = auditHitAreaSource(
    "fixture.tsx",
    `
      import { Pressable } from "./Pressable";
      const compactRole = ["h-9", active && "text-accent"].join(" ");
      export function Fixture() {
        return (
          <>
            <Pressable className="h-8" />
            <button className={cn("h-8", active && "text-accent")}>Raw</button>
            <div role="button" className={compactRole}>Role</div>
            <button className="h-8 min-h-11">Safe</button>
          </>
        );
      }
    `,
  );

  assert.deepEqual(
    findings.map(({ tag }) => tag),
    ["button", "div"],
  );
});

test("mobile dialog panels must pair with their own shell subtree", () => {
  const issues = findMobileDialogIssues(
    "fixture.tsx",
    `
      export function Fixture() {
        return (
          <>
            <div className="mobile-dialog-shell fixed inset-0 z-50 flex">
              <section className="mobile-dialog-panel" />
            </div>
            <div className="mobile-dialog-shell fixed inset-0 z-50 flex">
              <section />
            </div>
            <div className="fixed inset-0 z-50 flex">
              <section />
            </div>
          </>
        );
      }
    `,
  );

  assert.deepEqual(
    issues.map(({ rule }) => rule),
    ["mobile-dialog-panel", "mobile-dialog-shell"],
  );
});

test("CDP page identity rejects redirects, wrong pages, errors, and blanks", () => {
  const valid = {
    finalUrl: "http://localhost:3000/projects",
    responseStatus: 200,
    redirectCount: 0,
    bodyTextLength: 12,
    meaningfulElementCount: 1,
    errorMarker: false,
  };
  assert.deepEqual(
    pageIdentityErrors("http://localhost:3000/projects", valid),
    [],
  );
  assert.match(
    pageIdentityErrors("http://localhost:3000/projects", {
      ...valid,
      finalUrl: "http://localhost:3000/login",
    }).join("\n"),
    /wrong final URL/,
  );
  assert.match(
    pageIdentityErrors("http://localhost:3000/projects", {
      ...valid,
      redirectCount: 1,
    }).join("\n"),
    /redirect/,
  );
  assert.match(
    pageIdentityErrors("http://localhost:3000/projects", {
      ...valid,
      responseStatus: 500,
    }).join("\n"),
    /document status/,
  );
  assert.match(
    pageIdentityErrors("http://localhost:3000/projects", {
      ...valid,
      bodyTextLength: 0,
      meaningfulElementCount: 0,
    }).join("\n"),
    /blank page/,
  );
  assert.match(
    pageIdentityErrors("http://localhost:3000/projects", {
      ...valid,
      errorMarker: true,
    }).join("\n"),
    /error page marker/,
  );
  assert.deepEqual(
    pageIdentityErrors(
      "http://localhost:3000/missing-mobile-route",
      {
        ...valid,
        finalUrl: "http://localhost:3000/missing-mobile-route",
        responseStatus: 404,
      },
      { expectedStatuses: [404] },
    ),
    [],
  );
});

test("CDP HTTP JSON requests have a hard timeout", async () => {
  let receivedSignal = false;
  const fetchImpl = (_url, init) =>
    new Promise((_resolve, reject) => {
      receivedSignal = init.signal instanceof AbortSignal;
      const guard = setTimeout(
        () => reject(new Error("request did not abort")),
        1_000,
      );
      init.signal.addEventListener(
        "abort",
        () => {
          clearTimeout(guard);
          reject(init.signal.reason);
        },
        { once: true },
      );
    });

  await assert.rejects(
    fetchJsonWithTimeout(
      "http://127.0.0.1:9/json/version",
      {},
      { timeoutMs: 20, fetchImpl },
    ),
    /timed out/,
  );
  assert.equal(receivedSignal, true);
});

test("clean shallow clones fail closed when no comparison base is available", () => {
  const tempRoot = mkdtempSync(
    path.join(os.tmpdir(), "lumen-web-gate-"),
  );
  const origin = path.join(tempRoot, "origin");
  const shallow = path.join(tempRoot, "shallow");
  try {
    git(tempRoot, ["init", "-b", "main", origin]);
    git(origin, ["config", "user.email", "codex@example.com"]);
    git(origin, ["config", "user.name", "Codex"]);
    writeFileSync(path.join(origin, "fixture.txt"), "one\n");
    git(origin, ["add", "fixture.txt"]);
    git(origin, ["commit", "-m", "first"]);
    writeFileSync(path.join(origin, "fixture.txt"), "two\n");
    git(origin, ["commit", "-am", "second"]);
    git(tempRoot, [
      "clone",
      "--depth",
      "1",
      pathToFileURL(origin).href,
      shallow,
    ]);
    assert.equal(
      git(shallow, ["rev-parse", "--is-shallow-repository"]),
      "true",
    );
    assert.throws(
      () => getGitChangeScope({ startDir: shallow, env: {} }),
      /Unable to determine a Git comparison base/,
    );
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

function isTestFile(filePath) {
  return (
    /(?:^|[/\\])__tests__(?:[/\\])/.test(filePath) ||
    /\.(?:test|spec)\.[cm]?[jt]sx?$/.test(filePath)
  );
}

function importsQueries(specifier) {
  return (
    specifier === "@/lib/queries" ||
    specifier === "./queries" ||
    specifier.endsWith("/lib/queries")
  );
}

function queryExportUsage(program, queriesPath) {
  const checker = program.getTypeChecker();
  const queriesFile = program.getSourceFile(queriesPath);
  assert.ok(queriesFile, `missing ${queriesPath}`);
  const moduleSymbol = checker.getSymbolAtLocation(queriesFile);
  assert.ok(moduleSymbol, `missing module symbol for ${queriesPath}`);
  const exportedNames = checker
    .getExportsOfModule(moduleSymbol)
    .map((symbol) => symbol.name)
    .filter((name) => name !== "default");
  const referencedNames = new Set();

  for (const sourceFile of program.getSourceFiles()) {
    if (
      sourceFile.fileName === queriesPath ||
      sourceFile.isDeclarationFile ||
      isTestFile(sourceFile.fileName)
    ) {
      continue;
    }
    for (const statement of sourceFile.statements) {
      if (
        ts.isImportDeclaration(statement) &&
        ts.isStringLiteral(statement.moduleSpecifier) &&
        importsQueries(statement.moduleSpecifier.text) &&
        statement.importClause?.namedBindings &&
        ts.isNamedImports(statement.importClause.namedBindings)
      ) {
        for (const element of statement.importClause.namedBindings.elements) {
          const localSymbol = checker.getSymbolAtLocation(element.name);
          let used = false;
          const visit = (node) => {
            if (used) return;
            if (
              ts.isIdentifier(node) &&
              node !== element.name &&
              checker.getSymbolAtLocation(node) === localSymbol
            ) {
              used = true;
              return;
            }
            ts.forEachChild(node, visit);
          };
          visit(sourceFile);
          if (used) {
            referencedNames.add(
              (element.propertyName ?? element.name).text,
            );
          }
        }
      }
      if (
        ts.isExportDeclaration(statement) &&
        statement.moduleSpecifier &&
        ts.isStringLiteral(statement.moduleSpecifier) &&
        importsQueries(statement.moduleSpecifier.text) &&
        statement.exportClause &&
        ts.isNamedExports(statement.exportClause)
      ) {
        for (const element of statement.exportClause.elements) {
          referencedNames.add(
            (element.propertyName ?? element.name).text,
          );
        }
      }
    }
  }
  return {
    exportedNames,
    referencedNames,
    unusedNames: exportedNames.filter(
      (name) => !referencedNames.has(name),
    ),
  };
}

test("dead-code analysis ignores unused imports and test-only imports", () => {
  const tempRoot = mkdtempSync(
    path.join(os.tmpdir(), "lumen-dead-code-"),
  );
  try {
    const queriesPath = path.join(tempRoot, "queries.ts");
    const productionPath = path.join(tempRoot, "production.ts");
    const unusedImportPath = path.join(tempRoot, "unused-import.ts");
    const testPath = path.join(tempRoot, "feature.test.ts");
    writeFileSync(
      queriesPath,
      [
        "export const used = 1;",
        "export const unusedImportOnly = 2;",
        "export const testOnly = 3;",
        "",
      ].join("\n"),
    );
    writeFileSync(
      productionPath,
      'import { used } from "./queries";\nconsole.log(used);\n',
    );
    writeFileSync(
      unusedImportPath,
      'import { unusedImportOnly } from "./queries";\nconsole.log("noop");\n',
    );
    writeFileSync(
      testPath,
      'import { testOnly } from "./queries";\nconsole.log(testOnly);\n',
    );
    const program = ts.createProgram({
      rootNames: [
        queriesPath,
        productionPath,
        unusedImportPath,
        testPath,
      ],
      options: {
        module: ts.ModuleKind.ESNext,
        moduleResolution: ts.ModuleResolutionKind.Bundler,
        target: ts.ScriptTarget.ES2022,
      },
    });
    const usage = queryExportUsage(program, queriesPath);
    assert.deepEqual([...usage.referencedNames], ["used"]);
    assert.deepEqual(
      usage.unusedNames.sort(),
      ["testOnly", "unusedImportOnly"],
    );
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("architecture gate rejects layer inversion and dependency cycles", () => {
  const tempRoot = mkdtempSync(
    path.join(os.tmpdir(), "lumen-web-architecture-"),
  );
  try {
    const srcRoot = path.join(tempRoot, "src");
    const files = {
      "app/page.ts": "export const page = true;\n",
      "components/widget.ts":
        'import { page } from "@/app/page";\nexport const widget = page;\n',
      "lib/domain.ts":
        'import { widget } from "@/components/widget";\nexport const domain = widget;\n',
      "store/first.ts":
        'import { second } from "./second";\nexport const first = second;\n',
      "store/second.ts":
        'import { first } from "./first";\nexport const second = first;\n',
    };
    for (const [relativePath, source] of Object.entries(files)) {
      const target = path.join(srcRoot, relativePath);
      const directory = path.dirname(target);
      execFileSync("mkdir", ["-p", directory]);
      writeFileSync(target, source);
    }

    const findings = collectArchitectureFindings({ srcRoot });

    assert.deepEqual(findings.violations, [
      {
        rule: "component-imports-page",
        source: "components/widget.ts",
        target: "app/page.ts",
      },
      {
        rule: "lower-layer-imports-ui",
        source: "lib/domain.ts",
        target: "components/widget.ts",
      },
    ]);
    assert.deepEqual(findings.cycles, [
      ["store/first.ts", "store/second.ts"],
    ]);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("production queries exports all have a real production reference", () => {
  const configPath = ts.findConfigFile(
    webRoot,
    ts.sys.fileExists,
    "tsconfig.json",
  );
  assert.ok(configPath, "missing tsconfig.json");
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  const parsed = ts.parseJsonConfigFileContent(
    config.config,
    ts.sys,
    webRoot,
  );
  const program = ts.createProgram({
    rootNames: parsed.fileNames,
    options: parsed.options,
  });
  const queriesPath = path.join(webRoot, "src", "lib", "queries.ts");
  const usage = queryExportUsage(program, queriesPath);
  assert.deepEqual(usage.unusedNames.sort(), []);
});

test("gate wiring keeps full Git history and the complete layout vocabulary", () => {
  const complexity = read("scripts/check-complexity.mjs");
  const architecture = read("scripts/check-architecture.mjs");
  const governance = read("scripts/check-ui-governance.mjs");
  const layout = read("scripts/check-layout-contract.mjs");
  const packageJson = read("package.json");
  const fullTsconfig = JSON.parse(read("tsconfig.full.json"));
  const design = read("DESIGN.md");
  const ci = read("../../.github/workflows/ci.yml");

  assert.match(complexity, /getGitChangeScope/);
  assert.match(architecture, /lower-layer-imports-ui/);
  assert.match(governance, /getGitChangeScope/);
  assert.match(packageJson, /check:architecture/);
  assert.match(packageJson, /tsc -p tsconfig\.full\.json/);
  assert.equal(fullTsconfig.compilerOptions.skipLibCheck, false);
  assert.ok(fullTsconfig.exclude.includes(".next/dev/types"));
  assert.match(layout, /"--content-form": "720px"/);
  assert.match(layout, /"--content-settings": "1080px"/);
  assert.match(design, /## 4\. 排版：14 档 type-\* class/);
  assert.match(ci, /fetch-depth:\s*0/);
  assert.match(ci, /web-gate-hardening\.test\.mjs/);
  assert.match(ci, /node scripts\/audit-hit-area\.mjs/);
});
