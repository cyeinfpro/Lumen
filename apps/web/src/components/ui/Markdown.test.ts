import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import type { HastElementNode } from "./markdownHighlight";

const {
  decodeHighlightedText,
  parseHighlightedHtmlToHast,
  rehypeHighlightLite,
} = await import(new URL("./markdownHighlight.ts", import.meta.url).href);

function readSource(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

function flattenHastText(node: { type: string; value?: string; children?: unknown[] }): string {
  if (node.type === "text") return node.value ?? "";
  if (!Array.isArray(node.children)) return "";
  return node.children
    .map((child) => flattenHastText(child as { type: string; value?: string; children?: unknown[] }))
    .join("");
}

test("Markdown and heavy engine components enforce dynamic import isolation", () => {
  const markdownSource = readSource("./Markdown.tsx");
  assert.doesNotMatch(markdownSource, /from\s+["']rehype-highlight["']/);
  assert.doesNotMatch(markdownSource, /from\s+["']highlight\.js["']/);
  assert.match(markdownSource, /import\(["']\.\/markdownHighlight["']\)/);
  assert.match(markdownSource, /export function hasFencedCodeBlock/);

  const highlightSource = readSource("./markdownHighlight.ts");
  assert.match(highlightSource, /from\s+["']highlight\.js\/lib\/core["']/);
  assert.doesNotMatch(highlightSource, /from\s+["']highlight\.js["']/);
  assert.doesNotMatch(highlightSource, /dangerouslySetInnerHTML/);

  const lazyInpaintSource = readSource("./inpaint/LazyInpaintModal.tsx");
  assert.match(lazyInpaintSource, /useInpaintStore/);
  assert.match(lazyInpaintSource, /if\s*\(\s*!open\s*\)\s*return\s+null/);

  const canvasWorkspaceSource = readSource("./canvas/CanvasWorkspace.tsx");
  assert.doesNotMatch(canvasWorkspaceSource, /from\s+["']@xyflow\/react["']/);
  assert.match(canvasWorkspaceSource, /import\(["']\.\/CanvasViewport["']\)/);
});

test("rehypeHighlightLite highlights known languages, aliases, and preserves escaped text safely", () => {
  const codeText = `const greeting: string = "<script>alert('xss') & ok</script>";\n`;
  const tree: HastElementNode = {
    type: "element",
    tagName: "div",
    children: [
      {
        type: "element",
        tagName: "pre",
        children: [
          {
            type: "element",
            tagName: "code",
            properties: { className: ["language-ts"] },
            children: [{ type: "text", value: codeText }],
          },
        ],
      },
    ],
  };

  rehypeHighlightLite({ detect: true })(tree);
  const pre = tree.children[0] as HastElementNode;
  const code = pre.children[0] as HastElementNode;
  assert.deepEqual(code.properties?.className, ["hljs", "language-ts"]);
  assert.ok(
    code.children.some(
      (child) =>
        child.type === "element" &&
        (child as HastElementNode).tagName === "span",
    ),
  );
  assert.equal(flattenHastText(code), codeText);
});

test("rehypeHighlightLite respects detect=false and gracefully ignores unknown languages", () => {
  const unknownLangTree: HastElementNode = {
    type: "element",
    tagName: "pre",
    children: [
      {
        type: "element",
        tagName: "code",
        properties: { className: ["language-unknown-dsl"] },
        children: [{ type: "text", value: "foo := bar" }],
      },
    ],
  };
  rehypeHighlightLite({ detect: true })(unknownLangTree);
  const unknownCode = unknownLangTree.children[0] as HastElementNode;
  assert.deepEqual(unknownCode.properties?.className, ["language-unknown-dsl"]);
  assert.equal(flattenHastText(unknownCode), "foo := bar");

  const untaggedTree: HastElementNode = {
    type: "element",
    tagName: "pre",
    children: [
      {
        type: "element",
        tagName: "code",
        properties: {},
        children: [{ type: "text", value: "def compute(x):\n    return x + 1\n" }],
      },
    ],
  };
  rehypeHighlightLite({ detect: false })(untaggedTree);
  const untaggedNoDetect = untaggedTree.children[0] as HastElementNode;
  assert.equal(untaggedNoDetect.children.length, 1);
  assert.equal(untaggedNoDetect.children[0]?.type, "text");

  rehypeHighlightLite({ detect: true })(untaggedTree);
  const untaggedDetected = untaggedTree.children[0] as HastElementNode;
  const classes = (untaggedDetected.properties?.className ?? []) as string[];
  assert.ok(classes.includes("hljs"));
  assert.ok(untaggedDetected.children.some((node) => node.type === "element"));
});

test("decodeHighlightedText and parseHighlightedHtmlToHast handle nested spans and entities", () => {
  assert.equal(
    decodeHighlightedText("&lt;div attr=&quot;1&quot;&gt;&#x27;&amp;&#39;&lt;/div&gt;"),
    `<div attr="1">'&'</div>`,
  );
  const nodes = parseHighlightedHtmlToHast(
    `<span class="hljs-keyword">export</span> <span class="hljs-title function_">fn</span>(&lt;T&gt;)`,
  );
  assert.equal(nodes.length, 4);
  assert.equal(flattenHastText({ type: "element", children: nodes }), "export fn(<T>)");
});
