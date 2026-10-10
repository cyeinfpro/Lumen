import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function previewHarness(output) {
  let cursor = 0;
  const state = [];
  const runtime = {
    Fragment: "fragment",
    jsx: (type, props, key) => ({ type, props: props ?? {}, key }),
    jsxs: (type, props, key) => ({ type, props: props ?? {}, key }),
  };
  const mocks = {
    "react/jsx-runtime": runtime,
    react: { useState(initial) {
      const slot = cursor++;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], (value) => { state[slot] = typeof value === "function" ? value(state[slot]) : value; }];
    } },
    "@/lib/apiClient": {
      imageVariantUrl: (id) => "/display/" + id, imageBinaryUrl: (id) => "/image/" + id, videoBinaryUrl: (id) => "/video/" + id,
    },
    "@/lib/utils": { cn: (...classes) => classes.filter(Boolean).join(" ") },
  };
  const code = ts.transpileModule(readFileSync(new URL("../src/components/ui/canvas/nodes/CanvasNodesPreview.tsx", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const compiledModule = { exports: {} };
  new Function("require", "module", "exports", code)((id) => mocks[id] ?? new Proxy({}, { get: () => () => null }), compiledModule, compiledModule.exports);
  const wrapper = () => compiledModule.exports.OutputPreview({ output, alt: "test" });
  function expand(value) {
    if (Array.isArray(value)) return value.map(expand);
    if (!value || typeof value !== "object") return value;
    if (typeof value.type === "function") return expand(value.type(value.props));
    return { ...value, props: { ...value.props, children: expand(value.props.children) } };
  }
  return { wrapper, render() { cursor = 0; return expand(wrapper()); } };
}
function all(value, predicate) {
  if (Array.isArray(value)) return value.flatMap((v) => all(v, predicate));
  if (!value || typeof value !== "object") return [];
  return [...(predicate(value) ? [value] : []), ...all(value.props?.children, predicate)];
}
const byType = (tree, type) => all(tree, (item) => item.type === type);
const stateOf = (tree) => all(tree, (item) => "data-canvas-preview-state" in item.props)[0].props["data-canvas-preview-state"];
const click = (element) => element.props.onClick({ stopPropagation() {} });

test("Canvas video thumbnails render a poster image and no media player", () => {
  const harness = previewHarness({ type: "video", video_id: "v", poster_url: "/poster.png" });
  const tree = harness.render();
  assert.equal(byType(tree, "video").length, 0);
  assert.equal(byType(tree, "img")[0].props.src, "/poster.png");
  assert.equal(byType(tree, "img")[0].props.loading, "lazy");
  assert.equal(stateOf(tree), "loading");
});
test("Canvas missing poster remains playable without loading video", () => {
  const harness = previewHarness({ type: "video", video_id: "v" });
  const tree = harness.render();
  assert.equal(stateOf(tree), "unavailable");
  assert.equal(byType(tree, "img").length, 0);
  assert.equal(byType(tree, "video").length, 0);
  assert.ok(byType(tree, "button").some((button) => button.props["aria-label"] === "播放test"));
});
test("Canvas output without playable media does not promise a working play action", () => {
  const tree = previewHarness({ type: "video" }).render();
  const button = byType(tree, "button").find((item) => item.props["aria-label"] === "播放test");
  assert.equal(button.props.disabled, true);
  assert.equal(button.props.title, "暂无可用预览");
  assert.equal(byType(tree, "video").length, 0);
});

test("Canvas failed preview retries locally and transitions to ready on image load", () => {
  const harness = previewHarness({ type: "image", url: "/image.png" });
  let tree = harness.render();
  byType(tree, "img")[0].props.onError();
  tree = harness.render();
  assert.equal(stateOf(tree), "failed");
  const retry = byType(tree, "button").find((button) => button.props["aria-label"] === "重试预览");
  assert.ok(retry);
  click(retry);
  tree = harness.render();
  assert.equal(stateOf(tree), "loading");
  assert.match(byType(tree, "img")[0].key, /:1$/);
  byType(tree, "img")[0].props.onLoad({ currentTarget: { naturalWidth: 640, naturalHeight: 360 } });
  assert.equal(stateOf(harness.render()), "ready");
});
test("Canvas image source fallback preserves display and original ordering", () => {
  const harness = previewHarness({ type: "image", image_id: "i", preview_url: "/preview", url: "/original" });
  for (const source of ["/preview", "/display/i", "/original", "/image/i"]) {
    const tree = harness.render();
    assert.equal(byType(tree, "img")[0].props.src, source);
    byType(tree, "img")[0].props.onError();
  }
  assert.equal(stateOf(harness.render()), "failed");
});
test("Canvas replacement source changes component identity to reset failures and playback", () => {
  const first = previewHarness({ type: "video", video_id: "one", poster_url: "/one" });
  const second = previewHarness({ type: "video", video_id: "two", poster_url: "/two" });
  assert.notEqual(first.wrapper().key, second.wrapper().key);
  assert.equal(first.wrapper().key, first.wrapper().key);
});

test("Canvas pending preparation stays distinct from network failure without fetching a poster", () => {
  for (const preparation_state of ["pending", "preparing"]) {
    const tree = previewHarness({ type: "video", video_id: "v", poster_url: "/poster", preparation_state }).render();
    assert.equal(stateOf(tree), "processing");
    assert.equal(byType(tree, "img").length, 0);
    assert.equal(byType(tree, "video").length, 0);
    assert.equal(byType(tree, "button").find((item) => item.props["aria-label"] === "播放test").props.disabled, true);
    assert.equal(byType(tree, "button").some((item) => item.props["aria-label"] === "重试预览"), false);
  }
});
test("Canvas preparation failure does not offer a misleading local preparation retry", () => {
  const tree = previewHarness({ type: "video", video_id: "v", preparation_state: "failed" }).render();
  assert.equal(stateOf(tree), "preparation_failed");
  assert.equal(byType(tree, "button").some((item) => item.props["aria-label"] === "重试预览"), false);
});
test("Canvas same asset id with replaced content resets preview identity", () => {
  const output = { type: "image", image_id: "i", url: "/same" };
  assert.notEqual(previewHarness({ ...output, source_sha256: "a" }).wrapper().key,
    previewHarness({ ...output, source_sha256: "b" }).wrapper().key);
});
test("Canvas small image preview starts with the supplied thumbnail", () => {
  const tree = previewHarness({ type: "image", image_id: "i", thumbnail_url: "/thumb", preview_url: "/preview" }).render();
  assert.equal(byType(tree, "img")[0].props.src, "/thumb");
});
