import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

type ModuleExports = Record<string, unknown>;

class TestApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly payload: unknown;

  constructor(input: {
    code: string;
    message: string;
    status: number;
    payload?: unknown;
  }) {
    super(input.message);
    this.code = input.code;
    this.status = input.status;
    this.payload = input.payload;
  }
}

function compile(
  relativePath: string,
  overrides: Record<string, unknown>,
): ModuleExports {
  const url = new URL(relativePath, import.meta.url);
  const output = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: url.pathname,
  }).outputText;
  const compiledModule = { exports: {} as ModuleExports };
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

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}

function markDefinitiveRequestFailure<T extends object>(error: T): T {
  Object.defineProperty(error, "semanticIdempotencyDisposition", {
    configurable: true,
    value: "definitive-terminal-failure",
  });
  return error;
}

const AMBIGUOUS_FAILURE_CODES = new Set([
  "idempotency_replay_unavailable",
  "idempotency_terminal_persist_unknown",
]);

function errorRecord(error: unknown): Record<string, unknown> | null {
  return error !== null && typeof error === "object"
    ? (error as Record<string, unknown>)
    : null;
}

function ambiguousStatus(status: unknown): boolean {
  if (typeof status !== "number" || status <= 0) return true;
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function ambiguousFailure(error: unknown): boolean {
  const record = errorRecord(error);
  if (!record) return true;
  if (
    typeof record.code === "string" &&
    AMBIGUOUS_FAILURE_CODES.has(record.code)
  ) {
    return true;
  }
  if (
    record.semanticIdempotencyDisposition === "definitive-terminal-failure"
  ) {
    return false;
  }
  return ambiguousStatus(record.status);
}

function loadPromptEnhancement(responses: Array<Response | Error | object>) {
  const confirmed: string[] = [];
  const failures: Array<{ key: string; error: unknown }> = [];
  const calls: Array<{
    path: string;
    body: unknown;
    idempotencyKey: string | undefined;
  }> = [];
  let sequence = 0;
  let pendingKey: string | null = null;
  const semanticPostIdempotency = {
    async acquire() {
      pendingKey ??= `semantic-key-${++sequence}`;
      return {
        fingerprint: "fingerprint",
        key: pendingKey,
        expiresAt: Date.now() + 60_000,
        identityEpoch: 1,
      };
    },
    async confirm(lease: { key: string }) {
      confirmed.push(lease.key);
      if (pendingKey === lease.key) pendingKey = null;
    },
    async markSubmitted() {},
    async recordFailure(lease: { key: string }, error: unknown) {
      failures.push({ key: lease.key, error });
      if (!ambiguousFailure(error) && pendingKey === lease.key) {
        pendingKey = null;
      }
    },
  };
  const streamClient = {
    async postJson(
      path: string,
      body: unknown,
      _signal?: AbortSignal,
      idempotencyKey?: string,
    ): Promise<Response> {
      calls.push({ path, body, idempotencyKey });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      if (next && typeof next === "object" && !(next instanceof Response)) {
        throw next;
      }
      assert.ok(next instanceof Response);
      return next;
    },
  };
  const promptEnhancementApi = compile("./promptEnhancement.ts", {
    "./http": { ApiError: TestApiError },
    "./semanticIdempotency": {
      markDefinitiveRequestFailure,
      semanticPostIdempotency,
    },
    "./streamClient": { streamClient },
  }) as {
    streamPromptEnhancement(
      path: string,
      body: unknown,
      onDelta: (text: string) => void,
    ): Promise<void>;
  };
  return { calls, confirmed, failures, promptEnhancementApi };
}

test("prompt enhancement confirms its semantic key only after valid terminal completion", async () => {
  const harness = loadPromptEnhancement([
    streamResponse([
      'data: {"text":"better "}\n\n',
      'data: {"text":"prompt"}\n\n',
      "data: [DONE]\n\n",
    ]),
  ]);
  let output = "";

  await harness.promptEnhancementApi.streamPromptEnhancement(
    "/prompts/enhance",
    { text: "prompt" },
    (delta) => {
      output += delta;
    },
  );

  assert.equal(output, "better prompt");
  assert.equal(harness.calls[0]?.idempotencyKey, "semantic-key-1");
  assert.deepEqual(harness.confirmed, ["semantic-key-1"]);
  assert.deepEqual(harness.failures, []);
});

test("truncated and empty prompt streams retain the original semantic key", async () => {
  const harness = loadPromptEnhancement([
    streamResponse(['data: {"text":"partial"}\n\n']),
    streamResponse(["data: [DONE]\n\n"]),
  ]);

  await assert.rejects(
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/enhance",
      { text: "prompt" },
      () => undefined,
    ),
    /terminal completion/,
  );
  await assert.rejects(
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/enhance",
      { text: "prompt" },
      () => undefined,
    ),
    /empty response/,
  );

  assert.deepEqual(
    harness.calls.map((call) => call.idempotencyKey),
    ["semantic-key-1", "semantic-key-1"],
  );
  assert.deepEqual(harness.confirmed, []);
  assert.equal(harness.failures.length, 2);
  assert.ok(
    harness.failures.every((failure) => failure.key === "semantic-key-1"),
  );
});

test("network and 5xx failures keep the prompt enhancement semantic lease pending", async () => {
  const harness = loadPromptEnhancement([
    new TypeError("response lost"),
    { status: 503 },
  ]);

  await assert.rejects(
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/video/enhance",
      { text: "prompt" },
      () => undefined,
    ),
    /response lost/,
  );
  await assert.rejects(
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/video/enhance",
      { text: "prompt" },
      () => undefined,
    ),
  );

  assert.deepEqual(harness.confirmed, []);
  assert.equal(harness.failures.length, 2);
  assert.deepEqual(
    harness.calls.map((call) => call.idempotencyKey),
    ["semantic-key-1", "semantic-key-1"],
  );
});

test("server terminal SSE errors retire the failed key before retry", async () => {
  const harness = loadPromptEnhancement([
    streamResponse(['data: {"error":"billing_failed"}\n\n']),
    streamResponse(['data: {"text":"retry ok"}\n\ndata: [DONE]\n\n']),
  ]);

  await assert.rejects(
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/enhance",
      { text: "prompt" },
      () => undefined,
    ),
    /扣费结算失败/,
  );
  await harness.promptEnhancementApi.streamPromptEnhancement(
    "/prompts/enhance",
    { text: "prompt" },
    () => undefined,
  );

  assert.deepEqual(
    harness.calls.map((call) => call.idempotencyKey),
    ["semantic-key-1", "semantic-key-2"],
  );
  assert.equal(harness.failures.length, 1);
  assert.deepEqual(harness.confirmed, ["semantic-key-2"]);
});

test("terminal persistence uncertainty retains the SSE key without weakening internal errors", async () => {
  const harness = loadPromptEnhancement([
    streamResponse([
      'data: {"error":"idempotency_terminal_persist_unknown"}\n\n',
    ]),
    streamResponse(['data: {"error":"internal"}\n\n']),
    streamResponse(['data: {"text":"retry ok"}\n\ndata: [DONE]\n\n']),
  ]);
  const invoke = () =>
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/enhance",
      { text: "prompt" },
      () => undefined,
    );

  await assert.rejects(
    invoke(),
    (error) =>
      error instanceof TestApiError &&
      error.code === "idempotency_terminal_persist_unknown",
  );
  await assert.rejects(
    invoke(),
    (error) => error instanceof TestApiError && error.code === "internal",
  );
  await invoke();

  assert.deepEqual(
    harness.calls.map((call) => call.idempotencyKey),
    ["semantic-key-1", "semantic-key-1", "semantic-key-2"],
  );
  assert.deepEqual(
    harness.failures.map(({ error }) =>
      error instanceof TestApiError ? error.code : null,
    ),
    ["idempotency_terminal_persist_unknown", "internal"],
  );
  assert.deepEqual(harness.confirmed, ["semantic-key-2"]);
});

test("malformed SSE text and error fields remain ambiguous and emit no object text", async () => {
  const harness = loadPromptEnhancement([
    streamResponse(['data: {"text":{"value":"bad"}}\n\n']),
    streamResponse(['data: {"error":{"code":"billing_failed"}}\n\n']),
    streamResponse(['data: {"text":"valid"}\n\ndata: [DONE]\n\n']),
  ]);
  let output = "";
  const invoke = () =>
    harness.promptEnhancementApi.streamPromptEnhancement(
      "/prompts/enhance",
      { text: "prompt" },
      (delta) => {
        output += delta;
      },
    );

  await assert.rejects(invoke(), /Failed to parse enhancement response/);
  await assert.rejects(invoke(), /Failed to parse enhancement response/);
  await invoke();

  assert.equal(output, "valid");
  assert.deepEqual(
    harness.calls.map((call) => call.idempotencyKey),
    ["semantic-key-1", "semantic-key-1", "semantic-key-1"],
  );
  assert.equal(harness.failures.length, 2);
  assert.ok(
    harness.failures.every(
      ({ error }) =>
        error instanceof TestApiError &&
        error.code === "enhance_parse_error" &&
        !("semanticIdempotencyDisposition" in error),
    ),
  );
  assert.deepEqual(harness.confirmed, ["semantic-key-1"]);
});
