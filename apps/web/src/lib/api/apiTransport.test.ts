import {
  deepStrictEqual,
  equal,
  rejects,
  throws,
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

test("request budgets default queries and commands to one 30 second total deadline", () => {
  const budgets = compile("./requestBudget.ts") as {
    DEFAULT_API_TIMEOUT_MS: number;
    budgetFor(kind: string): { kind: string; totalMs: number };
  };
  equal(budgets.DEFAULT_API_TIMEOUT_MS, 30_000);
  deepStrictEqual(budgets.budgetFor("query"), {
    kind: "deadline",
    totalMs: 30_000,
  });
  deepStrictEqual(budgets.budgetFor("command"), {
    kind: "deadline",
    totalMs: 30_000,
  });
});

test("successful response parsing rejects malformed or non-JSON payloads", async () => {
  class TestApiError extends Error {
    code: string;
    status: number;
    payload: unknown;

    constructor(options: {
      code: string;
      message: string;
      status: number;
      payload?: unknown;
    }) {
      super(options.message);
      this.code = options.code;
      this.status = options.status;
      this.payload = options.payload;
    }
  }
  const responseModule = compile("./response.ts", {
    "./errors": { ApiError: TestApiError },
  }) as {
    applyResponseValidator<T>(
      response: Response,
      path: string,
      data: unknown,
      validate: (value: unknown) => T,
    ): T;
    readSuccessResponseData(response: Response): Promise<unknown>;
  };

  await rejects(
    responseModule.readSuccessResponseData(
      new Response("{", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
    (error: unknown) =>
      error instanceof TestApiError && error.code === "response_parse_error",
  );
  await rejects(
    responseModule.readSuccessResponseData(
      new Response("null", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
    (error: unknown) =>
      error instanceof TestApiError && error.code === "response_schema_error",
  );
  await rejects(
    responseModule.readSuccessResponseData(
      new Response("<html>ok</html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    ),
    (error: unknown) =>
      error instanceof TestApiError &&
      error.code === "response_content_type_error",
  );
  const validJsonWrongShape = await responseModule.readSuccessResponseData(
    new Response(JSON.stringify({ id: 42 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  throws(
    () =>
      responseModule.applyResponseValidator(
        new Response(null, { status: 200 }),
        "/auth/me",
        validJsonWrongShape,
        (value) => {
          if (
            !value ||
            typeof value !== "object" ||
            typeof (value as { id?: unknown }).id !== "string"
          ) {
            throw new TypeError("id must be a string");
          }
          return value;
        },
      ),
    (error: unknown) =>
      error instanceof TestApiError &&
      error.code === "response_schema_error" &&
      error.status === 200,
  );
  equal(
    await responseModule.readSuccessResponseData(
      new Response(null, { status: 204 }),
    ),
    undefined,
  );
});

test("caller abort reason wins over timeout and timeout is typed", async () => {
  class TimeoutApiError extends Error {
    code = "request_timeout";
  }
  const signalModule = compile("./requestSignal.ts", {
    "./errors": {
      timeoutError: () => new TimeoutApiError("timeout"),
    },
  }) as {
    createRequestSignal(
      signal: AbortSignal,
      budget: { kind: "deadline"; totalMs: number },
    ): {
      signal: AbortSignal;
      throwIfAborted(): void;
      cleanup(): void;
    };
  };
  const caller = new AbortController();
  const callerReason = { kind: "superseded" };
  const context = signalModule.createRequestSignal(caller.signal, {
    kind: "deadline",
    totalMs: 100,
  });
  caller.abort(callerReason);
  rejects(
    Promise.resolve().then(() => context.throwIfAborted()),
    (error) => error === callerReason,
  );
  context.cleanup();

  const timeout = signalModule.createRequestSignal(new AbortController().signal, {
    kind: "deadline",
    totalMs: 1,
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  rejects(
    Promise.resolve().then(() => timeout.throwIfAborted()),
    (error: unknown) =>
      error instanceof TimeoutApiError &&
      error.code === "request_timeout",
  );
  timeout.cleanup();
});

test("CSRF refresh is singleflight and invalidation discards stale responses", async () => {
  const documentDescriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "document",
  );
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: { cookie: "" },
  });
  let release!: (response: Response) => void;
  let calls = 0;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  const csrfModule = compile("./csrf.ts", {
    "@/lib/auth/authFailureCoordinator": {
      coordinateUnauthorized() {},
    },
    "./baseUrl": { API_BASE: "/api" },
    "./fetchExecutor": {
      executeFetch: async () => {
        calls += 1;
        return pending;
      },
    },
    "./requestBudget": {
      budgetFor: () => ({ kind: "deadline", totalMs: 30_000 }),
    },
    "./requestSignal": {
      createRequestSignal: () => ({
        signal: new AbortController().signal,
        throwIfAborted() {},
        cleanup() {},
      }),
    },
  }) as {
    DefaultCsrfService: new () => {
      refresh(): Promise<string | null>;
      invalidate(): void;
    };
  };
  try {
    const service = new csrfModule.DefaultCsrfService();
    const first = service.refresh();
    const second = service.refresh();
    equal(calls, 1);
    service.invalidate();
    release(
      new Response(JSON.stringify({ csrf_token: "stale" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    equal(await first, null);
    equal(await second, null);
  } finally {
    if (documentDescriptor) {
      Object.defineProperty(globalThis, "document", documentDescriptor);
    } else {
      delete (globalThis as Record<string, unknown>).document;
    }
  }
});

test("command client applies identity policy before the fake transport", async () => {
  const calls: string[] = [];
  class TestApiError extends Error {
    status: number;
    constructor(status: number) {
      super("blocked");
      this.status = status;
    }
  }
  const commandModule = compile("./commandClient.ts", {
    "@/lib/auth/identityPolicy": {
      identityWritePolicy: { assertAllowed() {} },
    },
    "@/lib/auth/authFailureCoordinator": {
      coordinateUnauthorized() {
        calls.push("unauthorized");
      },
    },
    "./errors": { ApiError: TestApiError },
    "./transport": {
      apiTransport: {
        async request(path: string) {
          calls.push(path);
          return { ok: true };
        },
      },
    },
  }) as {
    CommandApiClient: new (policy: {
      assertAllowed(method: string, path: string): void;
    }) => {
      request<T>(
        path: string,
        options: { method: "POST" },
      ): Promise<T | undefined>;
    };
  };
  const blocked = new commandModule.CommandApiClient({
    assertAllowed() {
      throw new TestApiError(401);
    },
  });
  throws(() => blocked.request("/admin/settings", { method: "POST" }));
  deepStrictEqual(calls, ["unauthorized"]);

  const allowed = new commandModule.CommandApiClient({
    assertAllowed(method, path) {
      calls.push(`${method}:${path}`);
    },
  });
  deepStrictEqual(
    await allowed.request("/conversations", { method: "POST" }),
    { ok: true },
  );
  deepStrictEqual(calls, [
    "unauthorized",
    "POST:/conversations",
    "/conversations",
  ]);
});

test("download and stream clients use the typed raw transport adapters", async () => {
  const calls: Array<{ path: string; init: Record<string, unknown> }> = [];
  const fakeTransport = {
    async requestRaw(
      path: string,
      init: Record<string, unknown>,
      readSuccess: (response: Response) => Promise<unknown>,
    ) {
      calls.push({ path, init });
      return readSuccess(
        new Response(new Blob(["zip"]), {
          status: 200,
          headers: { "content-type": "application/zip" },
        }),
      );
    },
  };
  const downloadModule = compile("./downloadClient.ts", {
    "./transport": { apiTransport: fakeTransport },
    "./requestBudget": {
      deadline: (totalMs: number) => ({ kind: "deadline", totalMs }),
    },
  }) as {
    downloadClient: {
      postBlob(path: string): Promise<Blob>;
    };
  };
  const streamModule = compile("./streamClient.ts", {
    "./baseUrl": { apiUrl: (path: string) => `/api${path}` },
    "./requestBudget": {
      deadline: (totalMs: number) => ({ kind: "deadline", totalMs }),
    },
    "./transport": { apiTransport: fakeTransport },
  }) as {
    streamClient: {
      url(path: string): string;
      postJson(
        path: string,
        body: unknown,
        signal?: AbortSignal,
        idempotencyKey?: string,
      ): Promise<Response>;
    };
  };

  const blob = await downloadModule.downloadClient.postBlob("/me/export");
  equal(await blob.text(), "zip");
  equal(streamModule.streamClient.url("/prompts/enhance"), "/api/prompts/enhance");
  await streamModule.streamClient.postJson(
    "/prompts/enhance",
    { text: "x" },
    undefined,
    "stream-key-1",
  );
  equal(
    new Headers(calls[1]?.init.headers as HeadersInit).get("Idempotency-Key"),
    "stream-key-1",
  );
  deepStrictEqual(
    calls.map(({ path, init }) => [
      path,
      init.method,
      init.requestClass,
      init.applyCsrf,
    ]),
    [
      ["/me/export", "POST", "long-operation", true],
      ["/prompts/enhance", "POST", "command", true],
    ],
  );
});
