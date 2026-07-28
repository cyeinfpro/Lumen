import {
  equal,
  notEqual,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

function compile(
  relativePath: string,
  overrides: Record<string, unknown> = {},
) {
  const url = new URL(relativePath, import.meta.url);
  const source = readFileSync(url, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: url.pathname,
  }).outputText;
  const compiledModule = { exports: {} as Record<string, unknown> };
  new Function("require", "module", "exports", output)(
    (id: string) => {
      if (id in overrides) return overrides[id];
      throw new Error(`missing test dependency: ${id}`);
    },
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

test("retry mode rejects one-shot ReadableStream bodies", () => {
  const policy = compile("./retryPolicy.ts", {
    "./requestSignal": {
      abortReason: () => new DOMException("aborted", "AbortError"),
    },
  }) as {
    retryModeFor(
      method: string,
      headers: Headers,
      body?: BodyInit | null,
    ): string;
  };
  const headers = new Headers({ "Idempotency-Key": "request-1" });

  equal(
    policy.retryModeFor(
      "POST",
      headers,
      new ReadableStream<Uint8Array>(),
    ),
    "none",
  );
  equal(policy.retryModeFor("POST", headers, JSON.stringify({ ok: true })), "idempotent");
});

test("fetch retries rebuild requests and cancel the discarded response body", async () => {
  let cancelled = false;
  let factoryCalls = 0;
  const requests: RequestInit[] = [];
  const executor = compile("./fetchExecutor.ts", {
    "./retryPolicy": {
      retryDelayMs: () => 0,
      shouldRetryStatus: (status: number) => status === 503,
      waitForRetry: async () => undefined,
    },
  }) as {
    executeFetch(
      url: string,
      requestFactory: () => RequestInit,
      options: {
        retryMode: string;
        maxRetries: number;
        fetchImpl: typeof fetch;
      },
    ): Promise<Response>;
  };
  const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
    requests.push(init ?? {});
    if (requests.length === 1) {
      return new Response(
        new ReadableStream<Uint8Array>({
          cancel() {
            cancelled = true;
          },
        }),
        { status: 503 },
      );
    }
    return new Response("ok", { status: 200 });
  }) as typeof fetch;

  const response = await executor.executeFetch(
    "https://example.test/api",
    () => {
      factoryCalls += 1;
      return {
        method: "POST",
        body: new Blob([`attempt-${factoryCalls}`]),
      };
    },
    {
      retryMode: "idempotent",
      maxRetries: 1,
      fetchImpl,
    },
  );

  equal(response.status, 200);
  equal(factoryCalls, 2);
  equal(requests.length, 2);
  notEqual(requests[0], requests[1]);
  equal(cancelled, true);
});
