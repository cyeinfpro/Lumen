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
const defaultBaselinePath = path.join(
  scriptDir,
  "architecture-baseline.json",
);
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
const BROWSER_GLOBALS = new Set(["document", "window"]);
const REALTIME_CONSTRUCTORS = new Set([
  "BroadcastChannel",
  "BrowserEventSourceTransport",
  "EventSource",
  "RealtimeRuntime",
  "WebSocket",
]);
const SERVER_ENTRY_FILE = /^(?:default|error|layout|loading|not-found|page|route|template)\.[cm]?[jt]sx?$/;


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


function hasDirective(sourceFile, directive) {
  for (const statement of sourceFile.statements) {
    if (
      !ts.isExpressionStatement(statement) ||
      !ts.isStringLiteral(statement.expression)
    ) {
      return false;
    }
    if (statement.expression.text === directive) return true;
  }
  return false;
}


function isIdentifierReference(node) {
  const parent = node.parent;
  if (parent === undefined) return true;
  if (
    (ts.isPropertyAccessExpression(parent) && parent.name === node) ||
    (ts.isPropertyAssignment(parent) && parent.name === node) ||
    (ts.isMethodDeclaration(parent) && parent.name === node) ||
    (ts.isPropertyDeclaration(parent) && parent.name === node) ||
    (ts.isPropertySignature(parent) && parent.name === node) ||
    (ts.isMethodSignature(parent) && parent.name === node) ||
    (ts.isBindingElement(parent) && parent.name === node) ||
    (ts.isVariableDeclaration(parent) && parent.name === node) ||
    (ts.isParameter(parent) && parent.name === node) ||
    (ts.isFunctionDeclaration(parent) && parent.name === node) ||
    (ts.isClassDeclaration(parent) && parent.name === node) ||
    (ts.isInterfaceDeclaration(parent) && parent.name === node) ||
    (ts.isTypeAliasDeclaration(parent) && parent.name === node) ||
    ts.isImportClause(parent) ||
    ts.isImportSpecifier(parent) ||
    ts.isNamespaceImport(parent) ||
    ts.isExportSpecifier(parent)
  ) {
    return false;
  }
  return true;
}


function addBindingNames(name, names) {
  if (ts.isIdentifier(name)) {
    names.add(name.text);
    return;
  }
  for (const element of name.elements) {
    if (ts.isBindingElement(element)) addBindingNames(element.name, names);
  }
}


function locallyDeclaredNames(sourceFile) {
  const names = new Set();
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) ||
      ts.isParameter(node) ||
      ts.isBindingElement(node)
    ) {
      addBindingNames(node.name, names);
    } else if (
      (ts.isFunctionDeclaration(node) ||
        ts.isClassDeclaration(node) ||
        ts.isInterfaceDeclaration(node) ||
        ts.isTypeAliasDeclaration(node) ||
        ts.isEnumDeclaration(node)) &&
      node.name
    ) {
      names.add(node.name.text);
    } else if (ts.isImportClause(node) && node.name) {
      names.add(node.name.text);
    } else if (ts.isImportSpecifier(node) || ts.isNamespaceImport(node)) {
      names.add(node.name.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return names;
}


function globalMemberName(expression) {
  if (ts.isIdentifier(expression)) return expression.text;
  if (
    ts.isPropertyAccessExpression(expression) &&
    ts.isIdentifier(expression.expression) &&
    (expression.expression.text === "globalThis" ||
      expression.expression.text === "window")
  ) {
    return expression.name.text;
  }
  return null;
}


function sourceFacts(sourceFile) {
  const browserGlobals = new Set();
  const declaredNames = locallyDeclaredNames(sourceFile);
  const realtimeCreations = new Set();
  let callsFetch = false;

  const visit = (node) => {
    if (
      ts.isIdentifier(node) &&
      BROWSER_GLOBALS.has(node.text) &&
      !declaredNames.has(node.text) &&
      isIdentifierReference(node)
    ) {
      browserGlobals.add(node.text);
    }
    if (
      ts.isPropertyAccessExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "globalThis" &&
      BROWSER_GLOBALS.has(node.name.text)
    ) {
      browserGlobals.add(node.name.text);
    }
    if (
      ts.isCallExpression(node) &&
      globalMemberName(node.expression) === "fetch" &&
      !declaredNames.has("fetch")
    ) {
      callsFetch = true;
    }
    if (ts.isNewExpression(node)) {
      const constructorName = globalMemberName(node.expression);
      if (REALTIME_CONSTRUCTORS.has(constructorName)) {
        realtimeCreations.add(constructorName);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);

  return {
    browserGlobals: [...browserGlobals].sort(),
    callsFetch,
    realtimeCreations: [...realtimeCreations].sort(),
    useClient: hasDirective(sourceFile, "use client"),
    useServer: hasDirective(sourceFile, "use server"),
  };
}


function layerOf(relativePath) {
  return relativePath.split("/", 1)[0];
}


function featureOf(relativePath) {
  const parts = relativePath.split("/");
  return parts[0] === "features" && parts.length >= 3 ? parts[1] : null;
}


function isFeaturePublicEntry(relativePath) {
  const parts = relativePath.split("/");
  return (
    parts[0] === "features" &&
    parts.length === 3 &&
    /^index\.[cm]?[jt]sx?$/.test(parts[2])
  );
}


function isUiModule(relativePath) {
  return (
    relativePath.startsWith("app/") ||
    relativePath.startsWith("components/") ||
    relativePath.startsWith("shared/ui/") ||
    /^features\/[^/]+\/ui\//.test(relativePath)
  );
}


function isPresentationalUi(relativePath) {
  return (
    relativePath.startsWith("shared/ui/") ||
    relativePath.startsWith("components/ui/primitives/") ||
    /^features\/[^/]+\/ui\//.test(relativePath)
  );
}


function isApiOwnershipModule(relativePath) {
  return (
    relativePath === "lib/apiClient.ts" ||
    relativePath === "lib/queries.ts" ||
    relativePath.startsWith("lib/api/") ||
    relativePath.startsWith("lib/queries/") ||
    relativePath.startsWith("shared/api/") ||
    /^features\/[^/]+\/api\//.test(relativePath)
  );
}


function storeOwner(relativePath) {
  const feature = featureOf(relativePath);
  if (
    feature !== null &&
    relativePath.startsWith(`features/${feature}/store/`)
  ) {
    return `feature:${feature}`;
  }
  const parts = relativePath.split("/");
  if (parts[0] !== "store" || parts.length < 2) return null;
  if (parts.length >= 3) return `store:${parts[1]}`;
  const basename = parts[1].replace(/\.[^.]+$/, "");
  const hookMatch = /^use([A-Z][A-Za-z0-9]*)Store$/.exec(basename);
  if (hookMatch) {
    const owner = hookMatch[1];
    return `store:${owner[0].toLowerCase()}${owner.slice(1)}`;
  }
  const prefix = /^[a-z]+/.exec(basename)?.[0] ?? basename;
  return `store:${prefix}`;
}


function directBoundaryViolations(source, target) {
  const violations = [];
  const sourceLayer = layerOf(source);
  const targetLayer = layerOf(target);
  const sourceStore = storeOwner(source);
  const targetStore = storeOwner(target);

  if (sourceStore !== null && isUiModule(target)) {
    violations.push({
      rule: "store-imports-ui",
      source,
      target,
    });
  } else if (LOWER_LAYERS.has(sourceLayer) && UI_LAYERS.has(targetLayer)) {
    violations.push({
      rule: "lower-layer-imports-ui",
      source,
      target,
    });
  }
  if (sourceLayer === "components" && targetLayer === "app") {
    violations.push({
      rule: "component-imports-page",
      source,
      target,
    });
  }
  if (
    sourceStore !== null &&
    targetStore !== null &&
    sourceStore !== targetStore
  ) {
    violations.push({
      rule: "store-to-store-import",
      source,
      target,
    });
  }

  const sourceFeature = featureOf(source);
  const targetFeature = featureOf(target);
  if (
    sourceFeature !== null &&
    targetFeature !== null &&
    sourceFeature !== targetFeature &&
    !isFeaturePublicEntry(target)
  ) {
    violations.push({
      rule: "feature-deep-import",
      source,
      target,
    });
  }
  return violations;
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


function comparePaths(left, right) {
  if (left.length !== right.length) return left.length - right.length;
  return left.join("\0").localeCompare(right.join("\0"));
}


function shortestCycle(edges, component) {
  const allowed = new Set(component);
  let best = null;
  for (const start of [...component].sort()) {
    const queue = [[start]];
    const shortestDistance = new Map([[start, 0]]);
    while (queue.length > 0) {
      const currentPath = queue.shift();
      const current = currentPath.at(-1);
      for (const target of [...(edges.get(current) ?? [])].sort()) {
        if (!allowed.has(target)) continue;
        const candidate = [...currentPath, target];
        if (target === start && candidate.length > 2) {
          if (best === null || comparePaths(candidate, best) < 0) {
            best = candidate;
          }
          continue;
        }
        const distance = currentPath.length;
        if (
          shortestDistance.has(target) &&
          shortestDistance.get(target) <= distance
        ) {
          continue;
        }
        shortestDistance.set(target, distance);
        queue.push(candidate);
      }
    }
  }
  return best;
}


function cyclePaths(edges) {
  return stronglyConnectedComponents(edges)
    .map((component) => shortestCycle(edges, component))
    .filter((cycle) => cycle !== null)
    .sort(comparePaths);
}


function featureGraph(edges) {
  const graph = new Map();
  for (const [source, targets] of edges) {
    const sourceFeature = featureOf(source);
    if (sourceFeature === null) continue;
    if (!graph.has(sourceFeature)) graph.set(sourceFeature, new Set());
    for (const target of targets) {
      const targetFeature = featureOf(target);
      if (targetFeature === null || targetFeature === sourceFeature) continue;
      graph.get(sourceFeature).add(targetFeature);
      if (!graph.has(targetFeature)) graph.set(targetFeature, new Set());
    }
  }
  return graph;
}


function isServerEntry(relativePath, facts) {
  if (facts.useClient) return false;
  if (facts.useServer) return true;
  return (
    relativePath.startsWith("app/") &&
    SERVER_ENTRY_FILE.test(path.posix.basename(relativePath))
  );
}


function shortestServerBrowserViolations(edges, factsByFile) {
  const bestByTarget = new Map();
  const roots = [...factsByFile.entries()]
    .filter(([relativePath, facts]) => isServerEntry(relativePath, facts))
    .map(([relativePath]) => relativePath)
    .sort();

  for (const root of roots) {
    const queue = [[root]];
    const visited = new Set([root]);
    while (queue.length > 0) {
      const currentPath = queue.shift();
      const current = currentPath.at(-1);
      const currentFacts = factsByFile.get(current);
      if (currentPath.length > 1 && currentFacts?.useClient) continue;
      if (currentFacts?.browserGlobals.length > 0) {
        const previous = bestByTarget.get(current);
        if (
          previous === undefined ||
          comparePaths(currentPath, previous) < 0
        ) {
          bestByTarget.set(current, currentPath);
        }
      }
      for (const target of [...(edges.get(current) ?? [])].sort()) {
        if (visited.has(target)) continue;
        visited.add(target);
        queue.push([...currentPath, target]);
      }
    }
  }

  return [...bestByTarget.entries()]
    .map(([target, violationPath]) => ({
      detail: factsByFile.get(target).browserGlobals.join(","),
      path: violationPath,
      rule: "server-chain-imports-browser-global",
      source: violationPath[0],
      target,
    }))
    .sort((left, right) => comparePaths(left.path, right.path));
}


function violationSortKey(violation) {
  return [
    violation.rule,
    violation.source,
    violation.target,
    violation.path?.join(" -> ") ?? "",
    violation.detail ?? "",
  ].join("|");
}


export function collectArchitectureFindings({
  srcRoot = defaultSrcRoot,
} = {}) {
  const normalizedRoot = path.resolve(srcRoot);
  const packageImports = loadPackageImports(normalizedRoot);
  const files = listSourceFiles(normalizedRoot);
  const edges = new Map();
  const factsByFile = new Map();
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
    const facts = sourceFacts(sourceFile);
    const targets = new Set();
    factsByFile.set(source, facts);

    for (const specifier of importSpecifiers(sourceFile)) {
      const target = resolveInternalImport(
        filePath,
        specifier,
        normalizedRoot,
        packageImports,
      );
      if (target === null || target === source) continue;
      if (targets.has(target)) continue;
      targets.add(target);
      violations.push(...directBoundaryViolations(source, target));
      if (isPresentationalUi(source) && isApiOwnershipModule(target)) {
        violations.push({
          rule: "presentational-ui-imports-api",
          source,
          target,
        });
      }
    }
    if (isPresentationalUi(source) && facts.callsFetch) {
      violations.push({
        rule: "presentational-ui-calls-fetch",
        source,
        target: "<global:fetch>",
      });
    }
    if (!source.startsWith("shared/realtime/")) {
      for (const constructorName of facts.realtimeCreations) {
        violations.push({
          detail: constructorName,
          rule: "realtime-runtime-outside-owner",
          source,
          target: `<runtime:${constructorName}>`,
        });
      }
    }
    edges.set(source, targets);
  }

  violations.push(
    ...shortestServerBrowserViolations(edges, factsByFile),
  );
  const fileCycles = stronglyConnectedComponents(edges);
  const fileCyclePaths = cyclePaths(edges);
  const features = featureGraph(edges);
  const featureCycles = cyclePaths(features);

  return {
    cyclePaths: fileCyclePaths,
    cycles: fileCycles,
    edgeCount: [...edges.values()].reduce(
      (total, targets) => total + targets.size,
      0,
    ),
    featureCycles,
    featureEdgeCount: [...features.values()].reduce(
      (total, targets) => total + targets.size,
      0,
    ),
    featureCount: features.size,
    fileCount: files.length,
    violations: violations.sort((left, right) =>
      violationSortKey(left).localeCompare(violationSortKey(right)),
    ),
  };
}


function formatViolation(violation) {
  const violationPath = violation.path ?? [
    violation.source,
    violation.target,
  ];
  const detail = violation.detail ? ` [${violation.detail}]` : "";
  return `${violation.rule}: ${violationPath.join(" -> ")}${detail}`;
}


export function architectureFindingFingerprints(findings) {
  return [
    ...findings.violations.map(formatViolation),
    ...findings.cyclePaths.map(
      (cycle) => `dependency-cycle: ${cycle.join(" -> ")}`,
    ),
    ...findings.featureCycles.map(
      (cycle) => `feature-cycle: ${cycle.join(" -> ")}`,
    ),
  ].sort();
}


function loadArchitectureBaseline(baselinePath) {
  if (!existsSync(baselinePath)) return null;
  const payload = JSON.parse(readFileSync(baselinePath, "utf8"));
  if (
    !payload ||
    !Array.isArray(payload.findings) ||
    payload.findings.some((finding) => typeof finding !== "string")
  ) {
    throw new Error(
      `${baselinePath} must contain a string array named "findings"`,
    );
  }
  return [...new Set(payload.findings)].sort();
}


function main() {
  const findings = collectArchitectureFindings();
  const findingFingerprints = architectureFindingFingerprints(findings);
  const baseline = loadArchitectureBaseline(defaultBaselinePath);
  const baselineSet = new Set(baseline ?? []);
  const findingSet = new Set(findingFingerprints);
  const unbaselinedFindings = findingFingerprints.filter(
    (finding) => !baselineSet.has(finding),
  );
  const staleBaselineFindings = (baseline ?? []).filter(
    (finding) => !findingSet.has(finding),
  );
  if (process.argv.includes("--json")) {
    console.log(
      JSON.stringify(
        {
          ...findings,
          baselinePath: toPosix(
            path.relative(appRoot, defaultBaselinePath),
          ),
          findingFingerprints,
          staleBaselineFindings,
          unbaselinedFindings,
        },
        null,
        2,
      ),
    );
    if (
      unbaselinedFindings.length > 0 ||
      staleBaselineFindings.length > 0
    ) {
      process.exitCode = 1;
    }
    return;
  }
  if (
    unbaselinedFindings.length > 0 ||
    staleBaselineFindings.length > 0
  ) {
    console.error("Frontend architecture check failed:");
    for (const finding of unbaselinedFindings) {
      console.error(`- unbaselined: ${finding}`);
    }
    for (const finding of staleBaselineFindings) {
      console.error(`- stale baseline: ${finding}`);
    }
    process.exit(1);
  }
  console.log(
    `Frontend architecture passed: ${findings.fileCount} files, ` +
      `${findings.edgeCount} internal edges, ` +
      `${findings.featureCount} features, ` +
      `${findings.featureEdgeCount} cross-feature edges, ` +
      `${findingFingerprints.length} baselined findings.`,
  );
}


if (
  process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
) {
  main();
}
