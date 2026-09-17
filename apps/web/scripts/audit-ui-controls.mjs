#!/usr/bin/env node

// Static inventory, not an accessibility verdict or proof that a control was clicked.
// Usage: node scripts/audit-ui-controls.mjs [output.json]
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = join(root, "src");
const files = [];
function walk(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) walk(path);
    else if (entry.isFile() && /\.tsx$/.test(path) && !/\.(test|spec)\.tsx$/.test(path)) files.push(path);
  }
}
walk(sourceRoot);
files.sort();

const knownControls = new Set([
  "button", "a", "input", "textarea", "select", "summary", "Button", "IconButton",
  "MediaControlButton", "MobileIconButton", "Pressable", "Link", "Input", "Textarea",
  "Select", "Slider", "Switch", "Chip",
]);
const records = [];
const routes = [];
function attributeText(attribute, source) {
  if (!attribute?.initializer) return attribute ? "true" : null;
  if (ts.isStringLiteral(attribute.initializer)) return attribute.initializer.text;
  return attribute.initializer.getText(source);
}
function textContent(node) {
  if (ts.isJsxText(node)) return node.text.replace(/\s+/g, " ").trim();
  if (ts.isJsxExpression(node)) {
    if (node.expression && ts.isStringLiteralLike(node.expression)) return node.expression.text;
    return node.expression ? `{${node.expression.getText().replace(/\s+/g, " ").slice(0, 120)}}` : "";
  }
  if (ts.isJsxElement(node) || ts.isJsxFragment(node)) return node.children.map(textContent).filter(Boolean).join(" ");
  return "";
}

for (const path of files) {
  const file = relative(root, path).split("\\").join("/");
  const source = ts.createSourceFile(file, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  if (/\/page\.tsx$/.test(file) && file.startsWith("src/app/")) {
    const route = "/" + file.slice("src/app/".length).replace(/(?:\/)?page\.tsx$/, "").split("/").filter((part) => !/^\(.*\)$/.test(part)).join("/");
    routes.push({ route, file, status: "inventory-only; runtime state coverage recorded separately" });
  }
  const labelledIds = new Set();
  function collectLabels(node) {
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(source) === "label") {
      const attribute = node.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(source) === "htmlFor");
      const value = attributeText(attribute, source);
      if (value) labelledIds.add(value);
    }
    ts.forEachChild(node, collectLabels);
  }
  collectLabels(source);
  function visit(node) {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(source);
      const attrs = new Map(node.attributes.properties.filter(ts.isJsxAttribute).map((attr) => [attr.name.getText(source), attributeText(attr, source)]));
      const role = attrs.get("role");
      if (knownControls.has(tag) || attrs.has("onClick") || ["button", "link", "tab", "menuitem", "checkbox", "radio"].includes(role)) {
        const location = source.getLineAndCharacterOfPosition(node.getStart(source));
        const element = ts.isJsxOpeningElement(node) ? node.parent : node;
        const text = textContent(element).slice(0, 200);
        let wrappedLabel = false;
        let ancestor = element.parent;
        const interactiveAncestors = [];
        while (ancestor) {
          if (ts.isJsxElement(ancestor)) {
            const parentTag = ancestor.openingElement.tagName.getText(source);
            if (parentTag === "label") wrappedLabel = true;
            if (["a", "button", "Link", "Button", "IconButton", "Pressable"].includes(parentTag)) interactiveAncestors.push(parentTag);
          }
          ancestor = ancestor.parent;
        }
        const spread = node.attributes.properties.some(ts.isJsxSpreadAttribute);
        const name = attrs.get("aria-label") ?? attrs.get("aria-labelledby") ?? attrs.get("label") ?? attrs.get("title") ?? text;
        const flags = [];
        if (tag === "button" && !attrs.has("type") && !spread) flags.push("native-button-implicit-type");
        if (["input", "textarea", "select", "Input", "Textarea", "Select", "Slider", "Switch"].includes(tag)
          && !["hidden", "submit", "button", "reset"].includes(attrs.get("type"))
          && !name && !wrappedLabel && !labelledIds.has(attrs.get("id")) && !spread) flags.push("field-name-needs-review");
        if (["button", "Button", "IconButton", "MobileIconButton", "a", "Link"].includes(tag) && !name && !spread) flags.push("control-name-needs-review");
        if (interactiveAncestors.length && knownControls.has(tag)) flags.push("nested-interactive-needs-review");
        if (["div", "span", "section", "article"].includes(tag) && attrs.has("onClick") && !attrs.has("role")) flags.push("clickable-container-needs-review");
        records.push({
          id: `${file}:${location.line + 1}:${location.character + 1}`, file,
          line: location.line + 1, tag, name: name || null,
          type: attrs.get("type") ?? null, role: role ?? null,
          href: attrs.get("href") ?? null,
          disabled: attrs.get("disabled") ?? attrs.get("aria-disabled") ?? null,
          busy: attrs.get("loading") ?? attrs.get("aria-busy") ?? null,
          handler: attrs.get("onClick") ?? attrs.get("onChange") ?? null,
          flags, status: "not-runtime-verified",
        });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
const flagCounts = {};
for (const record of records) for (const flag of record.flags) flagCounts[flag] = (flagCounts[flag] ?? 0) + 1;
const report = {
  schemaVersion: 1,
  scope: "All production src/**/*.tsx declarations. Dynamic lists, conditional states, composed labels and prop spreading require runtime review. Flags are candidates, never automatic failures. Source declarations are not rendered button counts.",
  summary: { sourceFiles: files.length, routes: routes.length, controlDeclarations: records.length, flaggedDeclarations: records.filter((record) => record.flags.length).length, flagCounts },
  routes, controls: records,
};
if (process.argv[2]) {
  const destination = resolve(process.cwd(), process.argv[2]);
  mkdirSync(dirname(destination), { recursive: true });
  writeFileSync(destination, JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ ...report.summary, output: destination }, null, 2));
} else {
  console.log(JSON.stringify(report, null, 2));
}
