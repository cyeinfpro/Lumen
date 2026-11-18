import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

export interface HastTextNode {
  type: "text";
  value: string;
  children?: undefined;
}

export interface HastElementNode {
  type: "element";
  tagName: string;
  properties?: Record<string, unknown>;
  children: HastChildNode[];
}

export type HastChildNode = HastTextNode | HastElementNode | { type: string; children?: HastChildNode[]; value?: string };

export interface RehypeHighlightLiteOptions {
  detect?: boolean;
}

let languagesRegistered = false;

function ensureCommonLanguagesRegistered(): void {
  if (languagesRegistered) return;
  languagesRegistered = true;
  const entries = [
    ["bash", bash, ["sh", "zsh"]],
    ["c", c, ["h"]],
    ["cpp", cpp, ["cc", "cxx", "hpp", "c++"]],
    ["css", css, []],
    ["diff", diff, ["patch"]],
    ["dockerfile", dockerfile, ["docker"]],
    ["go", go, ["golang"]],
    ["ini", ini, ["toml", "conf"]],
    ["java", java, []],
    ["javascript", javascript, ["js", "jsx", "mjs", "cjs"]],
    ["json", json, ["jsonc", "jsonl"]],
    ["markdown", markdown, ["md"]],
    ["plaintext", plaintext, ["text", "txt"]],
    ["python", python, ["py"]],
    ["rust", rust, ["rs"]],
    ["shell", shell, ["console", "shellsession"]],
    ["sql", sql, ["postgresql", "postgres", "sqlite"]],
    ["typescript", typescript, ["ts", "tsx", "mts", "cts"]],
    ["xml", xml, ["html", "xhtml", "svg", "vue"]],
    ["yaml", yaml, ["yml"]],
  ] as const;

  for (const [name, grammar, aliases] of entries) {
    hljs.registerLanguage(name, grammar);
    if (aliases.length > 0) {
      hljs.registerAliases([...aliases], { languageName: name });
    }
  }
}

const NAMED_HTML_ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": "\"",
  "&#x27;": "'",
  "&#39;": "'",
};

function decodeHtmlEntity(entity: string): string {
  const named = NAMED_HTML_ENTITIES[entity];
  if (named) return named;
  if (entity.startsWith("&#x") || entity.startsWith("&#X")) {
    const code = Number.parseInt(entity.slice(3, -1), 16);
    return Number.isFinite(code) ? String.fromCodePoint(code) : entity;
  }
  if (entity.startsWith("&#")) {
    const code = Number.parseInt(entity.slice(2, -1), 10);
    return Number.isFinite(code) ? String.fromCodePoint(code) : entity;
  }
  return entity;
}

export function decodeHighlightedText(escaped: string): string {
  if (!escaped.includes("&")) return escaped;
  return escaped.replace(/&(?:#x[0-9a-fA-F]+|#\d+|[a-zA-Z0-9]+);/g, decodeHtmlEntity);
}

const HLJS_SPAN_TOKEN_RE = /<span class="([^"]*)">|<\/span>/g;

export function parseHighlightedHtmlToHast(html: string): HastChildNode[] {
  const rootChildren: HastChildNode[] = [];
  const stack: HastChildNode[][] = [rootChildren];
  let cursor = 0;
  HLJS_SPAN_TOKEN_RE.lastIndex = 0;

  for (let match = HLJS_SPAN_TOKEN_RE.exec(html); match !== null; match = HLJS_SPAN_TOKEN_RE.exec(html)) {
    if (match.index > cursor) {
      const text = decodeHighlightedText(html.slice(cursor, match.index));
      if (text) stack[stack.length - 1]!.push({ type: "text", value: text });
    }
    const classAttr = match[1];
    if (classAttr !== undefined) {
      const children: HastChildNode[] = [];
      const className = classAttr.trim().split(/\s+/).filter(Boolean);
      stack[stack.length - 1]!.push({
        type: "element",
        tagName: "span",
        properties: { className },
        children,
      });
      stack.push(children);
    } else if (stack.length > 1) {
      stack.pop();
    }
    cursor = match.index + match[0].length;
  }

  if (cursor < html.length) {
    const trailing = decodeHighlightedText(html.slice(cursor));
    if (trailing) stack[stack.length - 1]!.push({ type: "text", value: trailing });
  }

  return rootChildren;
}

function readHastText(node: HastChildNode): string {
  if (node.type === "text") return typeof node.value === "string" ? node.value : "";
  if (!Array.isArray(node.children)) return "";
  return node.children.map(readHastText).join("");
}

function normalizeClassList(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map((item) => String(item));
  if (typeof raw === "string") return raw.trim().split(/\s+/).filter(Boolean);
  return [];
}

function extractLanguageFromClasses(classes: readonly string[]): {
  skip: boolean;
  language: string | null;
} {
  for (const cls of classes) {
    if (cls === "no-highlight" || cls === "nohighlight") {
      return { skip: true, language: null };
    }
    const match = /^(?:language|lang)-([\w+-]+)$/.exec(cls);
    if (match) return { skip: false, language: match[1]! };
  }
  return { skip: false, language: null };
}

function highlightCodeElement(codeNode: HastElementNode, detect: boolean): void {
  ensureCommonLanguagesRegistered();
  const properties = codeNode.properties ?? {};
  const classes = normalizeClassList(properties.className);
  const { skip, language } = extractLanguageFromClasses(classes);
  if (skip) return;

  const source = readHastText(codeNode);
  if (!source) return;

  if (language) {
    if (!hljs.getLanguage(language)) return;
    const highlighted = hljs.highlight(source, {
      language,
      ignoreIllegals: true,
    });
    const nextClasses = classes.includes("hljs") ? classes : ["hljs", ...classes];
    codeNode.properties = { ...properties, className: nextClasses };
    codeNode.children = parseHighlightedHtmlToHast(highlighted.value);
    return;
  }

  if (!detect) return;
  const auto = hljs.highlightAuto(source);
  if (!auto.language) return;
  const langClass = `language-${auto.language}`;
  const nextClasses = [...new Set(["hljs", ...classes, langClass])];
  codeNode.properties = { ...properties, className: nextClasses };
  codeNode.children = parseHighlightedHtmlToHast(auto.value);
}

function walkHastPreCode(node: HastChildNode, detect: boolean): void {
  if (!Array.isArray(node.children)) return;
  if (node.type === "element" && (node as HastElementNode).tagName === "pre") {
    for (const child of node.children) {
      if (child.type === "element" && (child as HastElementNode).tagName === "code") {
        highlightCodeElement(child as HastElementNode, detect);
      }
    }
    return;
  }
  for (const child of node.children) {
    walkHastPreCode(child, detect);
  }
}

export function rehypeHighlightLite(options?: RehypeHighlightLiteOptions) {
  const detect = options?.detect ?? false;
  return (tree: HastChildNode) => {
    walkHastPreCode(tree, detect);
  };
}
