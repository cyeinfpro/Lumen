#!/usr/bin/env node

import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";


const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(scriptDir, "..");
const defaultSrcRoot = path.join(appRoot, "src");
const SOURCE_EXTENSIONS = [
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".mts",
  ".mjs",
  ".cts",
  ".cjs",
];
const LOWER_LAYERS = new Set(["hooks", "lib", "store"]);
const UI_LAYERS = new Set(["app", "components"]);


function toPosix(value) {
  return value.split(path.sep).join("/");
}


function isProductionSource(filePath) {
  const normalized = toPosix(filePath);
  return (
    !/(?:^|\/)__tests__(?:\/|$)/.test(normalized) &&
    !/\.(?:test|spec)\.[cm]?[jt]sx?$/.test(normalized) &&
    !/\.d\.[cm]?ts$/.test(normalized)
  );
}


function sourceKind(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (filePath.endsWith(".js") || filePath.endsWith(".mjs")) {
    return ts.ScriptKind.JS;
  }
  return ts.ScriptKind.TS;
}


function listSourceFiles(srcRoot) {
  const files = [];
  const walk = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const fullPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        walk(fullPath);
      } else if (
        SOURCE_EXTENSIONS.some((extension) => entry.name.endsWith(extension)) &&
        isProductionSource(fullPath)
      ) {
        files.push(fullPath);
      }
    }
  };
  walk(srcRoot);
  return files.sort();
}


function loadPackageImports(srcRoot) {
  const packagePath = path.join(path.dirname(srcRoot), "package.json");
  if (!existsSync(packagePath)) return {};
  const payload = JSON.parse(readFileSync(packagePath, "utf8"));
  return payload.imports && typeof payload.imports === "object"
    ? payload.imports
    : {};
}


function candidateFiles(basePath) {
  const candidates = [basePath];
  if (!SOURCE_EXTENSIONS.some((extension) => basePath.endsWith(extension))) {
    for (const extension of SOURCE_EXTENSIONS) {
      candidates.push(`${basePath}${extension}`);
    }
    for (const extension of SOURCE_EXTENSIONS) {
      candidates.push(path.join(basePath, `index${extension}`));
    }
  }
  return candidates;
}


function resolveInternalImport(
  sourcePath,
  specifier,
  srcRoot,
  packageImports,
) {
  let basePath = null;
  if (specifier.startsWith("@/")) {
    basePath = path.join(srcRoot, specifier.slice(2));
  } else if (specifier.startsWith(".")) {
    basePath = path.resolve(path.dirname(sourcePath), specifier);
  } else if (specifier.startsWith("#")) {
    const mapped = packageImports[specifier];
    if (typeof mapped === "string" && mapped.startsWith("./")) {
      basePath = path.resolve(path.dirname(srcRoot), mapped);
    }
  }
  if (basePath === null) return null;
  for (const candidate of candidateFiles(basePath)) {
    if (!existsSync(candidate) || !statSync(candidate).isFile()) continue;
    const relative = path.relative(srcRoot, candidate);
    if (relative.startsWith("..") || path.isAbsolute(relative)) return null;
    return toPosix(relative);
  }
  return null;
}


function importSpecifiers(sourceFile) {
  const specifiers = [];
  const visit = (node) => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      ts.isStringLiteralLike(node.moduleSpecifier)
    ) {
      specifiers.push(node.moduleSpecifier.text);
    } else if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments.length === 1 &&
      ts.isStringLiteralLike(node.arguments[0])
    ) {
      specifiers.push(node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return specifiers;
}


function layerOf(relativePath) {
  return relativePath.split("/", 1)[0];
}


function boundaryViolation(source, target) {
  const sourceLayer = layerOf(source);
  const targetLayer = layerOf(target);
  if (LOWER_LAYERS.has(sourceLayer) && UI_LAYERS.has(targetLayer)) {
    return "lower-layer-imports-ui";
  }
  if (sourceLayer === "components" && targetLayer === "app") {
    return "component-imports-page";
  }
  return null;
}


function stronglyConnectedComponents(edges) {
  let index = 0;
  const stack = [];
  const onStack = new Set();
  const indices = new Map();
  const lowLinks = new Map();
  const components = [];

  const visit = (node) => {
    index += 1;
    indices.set(node, index);
    lowLinks.set(node, index);
    stack.push(node);
    onStack.add(node);
    for (const target of edges.get(node) ?? []) {
      if (!indices.has(target)) {
        visit(target);
        lowLinks.set(
          node,
          Math.min(lowLinks.get(node), lowLinks.get(target)),
        );
      } else if (onStack.has(target)) {
        lowLinks.set(
          node,
          Math.min(lowLinks.get(node), indices.get(target)),
        );
      }
    }
    if (lowLinks.get(node) !== indices.get(node)) return;
    const component = [];
    while (stack.length > 0) {
      const current = stack.pop();
      onStack.delete(current);
      component.push(current);
      if (current === node) break;
    }
    if (component.length > 1) components.push(component.sort());
  };

  for (const node of [...edges.keys()].sort()) {
    if (!indices.has(node)) visit(node);
  }
  return components.sort((left, right) => left[0].localeCompare(right[0]));
}


export function collectArchitectureFindings({
  srcRoot = defaultSrcRoot,
} = {}) {
  const normalizedRoot = path.resolve(srcRoot);
  const packageImports = loadPackageImports(normalizedRoot);
  const files = listSourceFiles(normalizedRoot);
  const edges = new Map();
  const violations = [];

  for (const filePath of files) {
    const source = toPosix(path.relative(normalizedRoot, filePath));
    const sourceFile = ts.createSourceFile(
      filePath,
      readFileSync(filePath, "utf8"),
      ts.ScriptTarget.Latest,
      true,
      sourceKind(filePath),
    );
    const targets = new Set();
    for (const specifier of importSpecifiers(sourceFile)) {
      const target = resolveInternalImport(
        filePath,
        specifier,
        normalizedRoot,
        packageImports,
      );
      if (target === null || target === source) continue;
      targets.add(target);
      const rule = boundaryViolation(source, target);
      if (rule !== null) {
        violations.push({ rule, source, target });
      }
    }
    edges.set(source, targets);
  }

  return {
    cycles: stronglyConnectedComponents(edges),
    edgeCount: [...edges.values()].reduce(
      (total, targets) => total + targets.size,
      0,
    ),
    fileCount: files.length,
    violations: violations.sort((left, right) =>
      `${left.rule}|${left.source}|${left.target}`.localeCompare(
        `${right.rule}|${right.source}|${right.target}`,
      ),
    ),
  };
}


function main() {
  const findings = collectArchitectureFindings();
  const errors = [
    ...findings.violations.map(
      ({ rule, source, target }) => `${rule}: ${source} -> ${target}`,
    ),
    ...findings.cycles.map(
      (component) => `dependency cycle: ${component.join(" -> ")}`,
    ),
  ];
  if (errors.length > 0) {
    console.error("Frontend architecture check failed:");
    for (const error of errors) console.error(`- ${error}`);
    process.exit(1);
  }
  console.log(
    `Frontend architecture passed: ${findings.fileCount} files, ` +
      `${findings.edgeCount} internal edges, 0 forbidden edges, 0 cycles.`,
  );
}


if (
  process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
) {
  main();
}
