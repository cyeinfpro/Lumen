import { deepEqual, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import * as ts from "typescript";

const source = readFileSync(new URL("./useSSE.ts", import.meta.url), "utf8");

function loadBackoffBaseDelay(): (attempt: number) => number {
  const start = source.indexOf("export function getSSEBackoffBaseDelay");
  const end = source.indexOf("function initialStatus", start);
  const output = ts.transpileModule(
    `${source.slice(start, end)}
module.exports.getSSEBackoffBaseDelay = getSSEBackoffBaseDelay;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as {
      getSSEBackoffBaseDelay: (attempt: number) => number;
    },
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports.getSSEBackoffBaseDelay;
}

test("SSE retry delay grows exponentially and remains capped", () => {
  const getSSEBackoffBaseDelay = loadBackoffBaseDelay();
  deepEqual(
    [0, 1, 2, 5, 20].map(getSSEBackoffBaseDelay),
    [1_000, 2_000, 4_000, 30_000, 30_000],
  );
});

test("SSE defaults to infinite retry and exposes immediate reconnect", () => {
  match(source, /DEFAULT_MAX_RETRY_COUNT = Number\.POSITIVE_INFINITY/);
  match(source, /reconnectNow\(\): void/);
  match(source, /return \{ status, reconnect \};/);
});
