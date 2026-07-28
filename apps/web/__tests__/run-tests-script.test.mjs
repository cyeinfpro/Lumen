import assert from "node:assert/strict";
import {
  mkdtempSync,
  mkdirSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  discoverTestFiles,
  selectTestFiles,
} from "../scripts/run-tests.mjs";


function makeWebRoot() {
  const root = mkdtempSync(path.join(os.tmpdir(), "lumen-web-tests-"));
  mkdirSync(path.join(root, "src/lib"), { recursive: true });
  mkdirSync(path.join(root, "__tests__"), { recursive: true });
  writeFileSync(path.join(root, "src/lib/runtime.test.ts"), "", "utf8");
  writeFileSync(path.join(root, "__tests__/shell.spec.mjs"), "", "utf8");
  writeFileSync(path.join(root, "src/lib/runtime.ts"), "", "utf8");
  return root;
}


test("no arguments preserve full test discovery", () => {
  const webRoot = makeWebRoot();

  assert.deepEqual(selectTestFiles([], webRoot), discoverTestFiles(webRoot));
});


test("explicit arguments select only normalized test files", () => {
  const webRoot = makeWebRoot();

  assert.deepEqual(
    selectTestFiles(
      ["src/lib/runtime.test.ts", "__tests__/shell.spec.mjs"],
      webRoot,
    ),
    ["src/lib/runtime.test.ts", "__tests__/shell.spec.mjs"],
  );
});


test("explicit selection rejects traversal, outside paths, and non-tests", () => {
  const webRoot = makeWebRoot();
  const outside = path.join(path.dirname(webRoot), "outside.test.ts");
  writeFileSync(outside, "", "utf8");

  assert.throws(
    () => selectTestFiles(["../outside.test.ts"], webRoot),
    /must not contain '\.\.'/,
  );
  assert.throws(
    () => selectTestFiles([outside], webRoot),
    /inside the Web root/,
  );
  assert.throws(
    () => selectTestFiles(["src/lib/runtime.ts"], webRoot),
    /test\/spec file/,
  );
});


test("explicit selection rejects symlinks escaping the Web root", () => {
  const webRoot = makeWebRoot();
  const outside = path.join(path.dirname(webRoot), "symlink-target.test.ts");
  writeFileSync(outside, "", "utf8");
  symlinkSync(outside, path.join(webRoot, "src/lib/linked.test.ts"));

  assert.throws(
    () => selectTestFiles(["src/lib/linked.test.ts"], webRoot),
    /inside the Web root/,
  );
});
