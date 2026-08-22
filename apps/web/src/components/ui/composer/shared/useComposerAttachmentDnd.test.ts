import { equal, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("./useComposerAttachmentDnd.ts", import.meta.url),
  "utf8",
);

test("reference upload expands the composer after file processing settles", () => {
  const start = source.indexOf("const ingestFile = useCallback(");
  const end = source.indexOf("const ingestMany = useCallback(", start);
  ok(start >= 0 && end > start, "missing attachment ingestion block");
  const block = source.slice(start, end);

  const uploadAt = block.indexOf("await uploadAttachment(file");
  const finallyAt = block.indexOf("} finally {");
  const expandAt = block.indexOf("setExpanded(true)");
  ok(uploadAt >= 0 && finallyAt > uploadAt && expandAt > finallyAt);
  equal((block.match(/setExpanded\(true\)/g) ?? []).length, 1);
});

test("temporary composer unmounts do not abort accepted uploads", () => {
  equal(source.includes("ctl.abort()"), false);
  equal(source.includes("uploadControllers.clear()"), false);
});
