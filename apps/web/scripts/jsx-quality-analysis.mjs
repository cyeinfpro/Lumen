import ts from "typescript";

const SAFE_HIT_AREA_COMPONENTS = new Set([
  "Pressable",
  "MobileIconButton",
]);

function scriptKind(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (filePath.endsWith(".js")) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

function parseSource(filePath, source) {
  const sourceFile = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(filePath),
  );
  if (sourceFile.parseDiagnostics.length > 0) {
    const diagnostic = sourceFile.parseDiagnostics[0];
    const message = ts.flattenDiagnosticMessageText(
      diagnostic.messageText,
      "\n",
    );
    throw new Error(`Unable to parse ${filePath}: ${message}`);
  }
  return sourceFile;
}

function openingElement(node) {
  if (ts.isJsxElement(node)) return node.openingElement;
  if (ts.isJsxSelfClosingElement(node)) return node;
  return null;
}

function jsxAttribute(opening, name, sourceFile) {
  return opening.attributes.properties.find(
    (property) =>
      ts.isJsxAttribute(property) &&
      property.name.getText(sourceFile) === name,
  );
}

function bindingInitializers(sourceFile) {
  const bindings = new Map();
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer
    ) {
      const values = bindings.get(node.name.text) ?? [];
      values.push(node.initializer);
      bindings.set(node.name.text, values);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return bindings;
}

function propertyNameText(name) {
  if (
    ts.isIdentifier(name) ||
    ts.isStringLiteral(name) ||
    ts.isNoSubstitutionTemplateLiteral(name)
  ) {
    return name.text;
  }
  return null;
}

function collectStaticFragments(
  node,
  bindings,
  output,
  conditional = false,
  resolving = new Set(),
) {
  if (!node) return;
  if (ts.isJsxAttribute(node)) {
    collectStaticFragments(
      node.initializer,
      bindings,
      output,
      conditional,
      resolving,
    );
    return;
  }
  if (ts.isJsxExpression(node)) {
    collectStaticFragments(
      node.expression,
      bindings,
      output,
      conditional,
      resolving,
    );
    return;
  }
  if (ts.isStringLiteralLike(node)) {
    output.push({ value: node.text, conditional });
    return;
  }
  if (ts.isTemplateExpression(node)) {
    output.push({ value: node.head.text, conditional });
    for (const span of node.templateSpans) {
      collectStaticFragments(
        span.expression,
        bindings,
        output,
        true,
        resolving,
      );
      output.push({ value: span.literal.text, conditional });
    }
    return;
  }
  if (ts.isIdentifier(node)) {
    const initializers = bindings.get(node.text);
    if (
      initializers?.length === 1 &&
      !resolving.has(node.text)
    ) {
      const nextResolving = new Set(resolving);
      nextResolving.add(node.text);
      collectStaticFragments(
        initializers[0],
        bindings,
        output,
        conditional,
        nextResolving,
      );
    }
    return;
  }
  if (ts.isConditionalExpression(node)) {
    collectStaticFragments(
      node.whenTrue,
      bindings,
      output,
      true,
      resolving,
    );
    collectStaticFragments(
      node.whenFalse,
      bindings,
      output,
      true,
      resolving,
    );
    return;
  }
  if (ts.isBinaryExpression(node)) {
    const isConcatenation =
      node.operatorToken.kind === ts.SyntaxKind.PlusToken;
    collectStaticFragments(
      node.left,
      bindings,
      output,
      conditional || !isConcatenation,
      resolving,
    );
    collectStaticFragments(
      node.right,
      bindings,
      output,
      conditional || !isConcatenation,
      resolving,
    );
    return;
  }
  if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression)
    ) {
      collectStaticFragments(
        node.expression.expression,
        bindings,
        output,
        conditional,
        resolving,
      );
    }
    for (const argument of node.arguments ?? []) {
      collectStaticFragments(
        argument,
        bindings,
        output,
        conditional,
        resolving,
      );
    }
    return;
  }
  if (ts.isArrayLiteralExpression(node)) {
    for (const element of node.elements) {
      collectStaticFragments(
        element,
        bindings,
        output,
        conditional,
        resolving,
      );
    }
    return;
  }
  if (ts.isObjectLiteralExpression(node)) {
    for (const property of node.properties) {
      if (ts.isPropertyAssignment(property)) {
        const name = propertyNameText(property.name);
        if (name) output.push({ value: name, conditional: true });
        collectStaticFragments(
          property.initializer,
          bindings,
          output,
          true,
          resolving,
        );
      } else if (ts.isShorthandPropertyAssignment(property)) {
        collectStaticFragments(
          property.name,
          bindings,
          output,
          true,
          resolving,
        );
      } else if (ts.isSpreadAssignment(property)) {
        collectStaticFragments(
          property.expression,
          bindings,
          output,
          true,
          resolving,
        );
      }
    }
    return;
  }
  if (
    ts.isParenthesizedExpression(node) ||
    ts.isAsExpression(node) ||
    ts.isTypeAssertionExpression(node) ||
    ts.isNonNullExpression(node) ||
    ts.isSatisfiesExpression(node)
  ) {
    collectStaticFragments(
      node.expression,
      bindings,
      output,
      conditional,
      resolving,
    );
    return;
  }
  ts.forEachChild(node, (child) =>
    collectStaticFragments(
      child,
      bindings,
      output,
      true,
      resolving,
    ),
  );
}

function attributeFragments(opening, name, sourceFile, bindings) {
  const attribute = jsxAttribute(opening, name, sourceFile);
  const fragments = [];
  collectStaticFragments(attribute, bindings, fragments);
  return fragments;
}

function classTokens(opening, sourceFile, bindings) {
  return attributeFragments(
    opening,
    "className",
    sourceFile,
    bindings,
  ).flatMap(({ value, conditional }) =>
    value
      .split(/\s+/)
      .filter(Boolean)
      .map((token) => ({ token, conditional })),
  );
}

function importedSafeComponentNames(sourceFile) {
  const names = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) {
      continue;
    }
    const clause = statement.importClause;
    if (
      clause.name &&
      SAFE_HIT_AREA_COMPONENTS.has(clause.name.text)
    ) {
      names.add(clause.name.text);
    }
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const element of clause.namedBindings.elements) {
        const importedName = (element.propertyName ?? element.name).text;
        if (SAFE_HIT_AREA_COMPONENTS.has(importedName)) {
          names.add(element.name.text);
        }
      }
    }
  }
  return names;
}

function mobileVariantApplies(token) {
  const parts = token.split(":");
  const variants = parts.slice(0, -1);
  return !variants.some((variant) =>
    /^(?:sm|md|lg|xl|2xl|min-\[)/.test(variant),
  );
}

function tailwindPixels(utility, prefix) {
  const scale = utility.match(new RegExp(`^${prefix}-(\\d+(?:\\.\\d+)?)$`));
  if (scale) return Number.parseFloat(scale[1]) * 4;
  const px = utility.match(
    new RegExp(`^${prefix}-\\[(\\d+(?:\\.\\d+)?)px\\]$`),
  );
  if (px) return Number.parseFloat(px[1]);
  const rem = utility.match(
    new RegExp(`^${prefix}-\\[(\\d+(?:\\.\\d+)?)rem\\]$`),
  );
  return rem ? Number.parseFloat(rem[1]) * 16 : null;
}

function utilityPart(token) {
  return token.split(":").at(-1) ?? token;
}

function hasSmallMobileHeight(tokens) {
  return tokens.some(({ token }) => {
    if (!mobileVariantApplies(token)) return false;
    const pixels = tailwindPixels(utilityPart(token), "h");
    return pixels !== null && pixels < 44;
  });
}

function hasGuaranteedMobileMinHeight(tokens) {
  return tokens.some(({ token, conditional }) => {
    if (conditional || !mobileVariantApplies(token)) return false;
    const pixels = tailwindPixels(utilityPart(token), "min-h");
    return pixels !== null && pixels >= 44;
  });
}

function hasHitAreaAllow(source, sourceFile, opening) {
  const lines = source.split(/\r?\n/);
  const start =
    sourceFile.getLineAndCharacterOfPosition(opening.getStart(sourceFile)).line;
  const end =
    sourceFile.getLineAndCharacterOfPosition(opening.end).line;
  return /@hit-area-ok/.test(
    lines.slice(Math.max(0, start - 1), end + 1).join("\n"),
  );
}

export function auditHitAreaSource(filePath, source) {
  const sourceFile = parseSource(filePath, source);
  const bindings = bindingInitializers(sourceFile);
  const safeComponents = importedSafeComponentNames(sourceFile);
  const findings = [];

  const visit = (node) => {
    const opening = openingElement(node);
    if (opening) {
      const tag = opening.tagName.getText(sourceFile);
      const roles = attributeFragments(
        opening,
        "role",
        sourceFile,
        bindings,
      ).map(({ value }) => value.trim());
      const interactive =
        tag === "button" ||
        tag === "a" ||
        roles.includes("button");
      if (interactive && !safeComponents.has(tag)) {
        const tokens = classTokens(opening, sourceFile, bindings);
        if (
          hasSmallMobileHeight(tokens) &&
          !hasGuaranteedMobileMinHeight(tokens) &&
          !hasHitAreaAllow(source, sourceFile, opening)
        ) {
          findings.push({
            line:
              sourceFile.getLineAndCharacterOfPosition(
                opening.getStart(sourceFile),
              ).line + 1,
            tag,
            snippet: opening.getText(sourceFile).slice(0, 220),
          });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return findings;
}

function isMobileDialogMediaException(filePath) {
  return /(?:lightbox|maskcanvas|inpaint|imagepreviewmodal|share\/\[token\]\/sharecontentclient)/i.test(
    filePath,
  );
}

function tokenSet(opening, sourceFile, bindings) {
  return new Set(
    classTokens(opening, sourceFile, bindings).map(({ token }) => token),
  );
}

function hasDialogPanelDescendant(
  root,
  sourceFile,
  bindings,
) {
  let found = false;
  const visit = (node) => {
    if (found) return;
    const opening = openingElement(node);
    if (opening) {
      const tokens = tokenSet(opening, sourceFile, bindings);
      if (tokens.has("mobile-dialog-shell")) return;
      if (
        tokens.has("mobile-dialog-panel") ||
        tokens.has("mobile-dialog-sheet")
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(root, visit);
  return found;
}

function looksLikeFixedModal(opening, sourceFile, bindings) {
  const tokenEntries = classTokens(opening, sourceFile, bindings);
  const tokens = new Set(tokenEntries.map(({ token }) => token));
  const unconditional = new Set(
    tokenEntries
      .filter(({ conditional }) => !conditional)
      .map(({ token }) => token),
  );
  if (!unconditional.has("fixed") || !unconditional.has("inset-0")) {
    return false;
  }
  const hasZIndex = [...unconditional].some((token) => /^z-/.test(token));
  if (!hasZIndex) return false;
  const tag = opening.tagName.getText(sourceFile);
  const interactive =
    ["section", "form", "aside"].includes(tag) ||
    [...tokens].some(
      (token) =>
        token === "flex" ||
        token === "grid" ||
        /^(?:items-|justify-|p-\d|px-|py-|sm:items|md:items)/.test(
          token,
        ),
    );
  if (!interactive) return false;
  const classes = [...tokens].join(" ");
  const standaloneScrim =
    /\bbg-black/.test(classes) &&
    ![...tokens].some(
      (token) =>
        token === "flex" ||
        token === "grid" ||
        /^(?:items-|justify-|p-\d|px-|py-)/.test(token),
    );
  return !standaloneScrim;
}

export function findMobileDialogIssues(filePath, source) {
  if (isMobileDialogMediaException(filePath)) return [];
  const sourceFile = parseSource(filePath, source);
  const bindings = bindingInitializers(sourceFile);
  const issues = [];

  const visit = (node) => {
    const opening = openingElement(node);
    if (opening) {
      const tokens = tokenSet(opening, sourceFile, bindings);
      const line =
        sourceFile.getLineAndCharacterOfPosition(
          opening.getStart(sourceFile),
        ).line + 1;
      if (tokens.has("mobile-dialog-shell")) {
        if (!hasDialogPanelDescendant(node, sourceFile, bindings)) {
          issues.push({
            rule: "mobile-dialog-panel",
            line,
            message:
              "mobile-dialog-shell must be paired with mobile-dialog-panel or mobile-dialog-sheet in the same dialog subtree.",
            snippet: opening.getText(sourceFile),
          });
        }
      } else if (looksLikeFixedModal(opening, sourceFile, bindings)) {
        issues.push({
          rule: "mobile-dialog-shell",
          line,
          message:
            "Fixed modal wrapper must use mobile-dialog-shell or be an allowlisted media/lightbox surface.",
          snippet: opening.getText(sourceFile),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return issues;
}
