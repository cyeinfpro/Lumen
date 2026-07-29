import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const boardSource = source("./MaskBoard.tsx");

test("MaskBoard stays below the component size limit and delegates responsibilities", () => {
  assert.ok(boardSource.split("\n").length < 800);
  assert.match(boardSource, /useMaskBoardState/);
  assert.match(boardSource, /useMaskPointerInteraction/);
  assert.match(boardSource, /<MaskBoardCanvas/);
  assert.match(boardSource, /<MaskBoardToolbar/);
  assert.doesNotMatch(boardSource, /function MaskCanvasStage/);
  assert.doesNotMatch(boardSource, /function estimateLuminance/);
});

test("MaskBoard preserves its public type export path", () => {
  assert.match(
    boardSource,
    /export type \{ MaskBoardHandle, MaskExport \} from "\.\/mask-board\/types"/,
  );
  assert.match(
    boardSource,
    /export type \{ Stroke, Tool \} from "\.\/types"/,
  );
});
