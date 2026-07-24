import {
  deepStrictEqual,
  doesNotMatch,
  match,
  ok,
  rejects,
  strictEqual,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const source = readFileSync(new URL("./apiClient.ts", import.meta.url), "utf8");
const httpSource = readFileSync(
  new URL("./api/http.ts", import.meta.url),
  "utf8",
);
const loginSource = readFileSync(
  new URL("../app/login/page.tsx", import.meta.url),
  "utf8",
);
const tasksSource = readFileSync(
  new URL("./api/tasks.ts", import.meta.url),
  "utf8",
);
const storyboardsSource = readFileSync(
  new URL("./api/storyboards.ts", import.meta.url),
  "utf8",
);
const workflowsSource = readFileSync(
  new URL("./api/workflows.ts", import.meta.url),
  "utf8",
);
const posterStylesSource = readFileSync(
  new URL("./api/posterStyles.ts", import.meta.url),
  "utf8",
);
const posterWorkflowsSource = readFileSync(
  new URL("./api/posterWorkflows.ts", import.meta.url),
  "utf8",
);
const videoAssetsSource = readFileSync(
  new URL("./api/videoAssets.ts", import.meta.url),
  "utf8",
);
const conversationsSource = readFileSync(
  new URL("./api/conversations.ts", import.meta.url),
  "utf8",
);
const systemPromptsSource = readFileSync(
  new URL("./api/systemPrompts.ts", import.meta.url),
  "utf8",
);
const imagesSource = readFileSync(
  new URL("./api/images.ts", import.meta.url),
  "utf8",
);
const accountSource = readFileSync(
  new URL("./api/account.ts", import.meta.url),
  "utf8",
);
const typesSource = readFileSync(new URL("./types.ts", import.meta.url), "utf8");
const videoAssetTypesSource = readFileSync(
  new URL("./videoAssetTypes.ts", import.meta.url),
  "utf8",
);

type TestApiError = Error & { code: string; status: number };

function loadCommonJsModule(
  moduleSource: string,
  fileName: string,
  overrides: Record<string, unknown> = {},
) {
  const output = ts.transpileModule(moduleSource, {
    compilerOptions: {
      isolatedModules: true,
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName,
  }).outputText;
  const compiledModule = { exports: {} as Record<string, unknown> };
  const requireModule = (id: string) => {
    if (id in overrides) return overrides[id];
    return new Proxy(
      { __esModule: true },
      {
        get(target, key) {
          if (key === "__esModule") return true;
          if (!(key in target)) {
            (target as Record<PropertyKey, unknown>)[key] = () => undefined;
          }
          return (target as Record<PropertyKey, unknown>)[key];
        },
      },
    );
  };
  new Function("require", "module", "exports", output)(
    requireModule,
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

function loadHttpModule(
  clearPrivateCanvasPersistence: () => Promise<void> = async () => undefined,
) {
  return loadCommonJsModule(httpSource, "http.ts", {
    "@/lib/auth/publicPaths": {
      isPublicPath(pathname: string) {
        return (
          pathname === "/login" ||
          pathname === "/signup" ||
          pathname.startsWith("/reset-password") ||
          pathname.startsWith("/invite/")
        );
      },
    },
    "#canvas-persistence": {
      activatePrivateCanvasPersistence: async () => undefined,
      clearPrivateCanvasPersistence,
    },
  }) as {
    ApiError: typeof Error & {
      new (opts: {
        code: string;
        message: string;
        status: number;
        payload?: unknown;
      }): TestApiError;
    };
    apiFetch<T>(
      path: string,
      init?: RequestInit & { timeoutMs?: number | null },
    ): Promise<T>;
    ensureCsrfToken(): Promise<string | null>;
    refreshCsrfToken(): Promise<string | null>;
    handle401(): void;
    invalidateSessionClientState(): Promise<void>;
    resumeSessionClientState(userId: string): Promise<void>;
    safeAuthNextPath(raw: string, origin?: string): string;
  };
}

function setGlobalProperty(name: string, value: unknown): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  });
  return () => {
    if (descriptor) {
      Object.defineProperty(globalThis, name, descriptor);
    } else {
      delete (globalThis as Record<string, unknown>)[name];
    }
  };
}

test("apiFetch maps timeout separately and preserves any caller abort reason without retry", async () => {
  match(httpSource, /export const DEFAULT_API_TIMEOUT_MS = 30_000/);
  match(httpSource, /timeoutMs = null/);
  match(httpSource, /type RequestAbortSource = "caller" \| "timeout" \| null/);

  const http = loadHttpModule();
  const caller = new AbortController();
  const callerReason = { kind: "superseded", requestId: 7 };
  let fetches = 0;
  let receivedSignal: AbortSignal | null | undefined;
  const restoreFetch = setGlobalProperty(
    "fetch",
    (_url: string, init: RequestInit) => {
      fetches += 1;
      return new Promise<Response>((_resolve, reject) => {
        receivedSignal = init.signal;
        if (init.signal?.aborted) {
          reject(init.signal.reason);
          return;
        }
        init.signal?.addEventListener(
          "abort",
          () => reject(init.signal?.reason),
          { once: true },
        );
      });
    },
  );
  try {
    const request = http.apiFetch("/slow", {
      signal: caller.signal,
      timeoutMs: 1_000,
    });
    await new Promise<void>((resolve) => setImmediate(resolve));
    ok(receivedSignal);
    caller.abort(callerReason);
    await rejects(request, (err: unknown) => err === callerReason);
    strictEqual(receivedSignal === caller.signal, false);
    strictEqual(receivedSignal?.aborted, true);
    strictEqual(fetches, 1);

    await rejects(
      http.apiFetch("/timeout", { timeoutMs: 5 }),
      (err: unknown) => {
        if (!(err instanceof http.ApiError)) return false;
        const apiError = err as TestApiError;
        return apiError.code === "request_timeout" && apiError.status === 0;
      },
    );
    strictEqual(fetches, 2);
  } finally {
    restoreFetch();
  }
});

test("apiFetch does not impose a global timeout unless the caller opts in", async () => {
  let receivedSignal: AbortSignal | null | undefined;
  const restoreFetch = setGlobalProperty(
    "fetch",
    async (_url: string, init: RequestInit) => {
      receivedSignal = init.signal;
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  );
  try {
    const http = loadHttpModule();
    deepStrictEqual(await http.apiFetch("/long-operation"), { ok: true });
    strictEqual(receivedSignal, undefined);
  } finally {
    restoreFetch();
  }
});

test("csrf refresh is singleflight across concurrent write preparation", async () => {
  const restoreDocument = setGlobalProperty("document", { cookie: "" });
  let csrfFetches = 0;
  let releaseResponse!: (response: Response) => void;
  const responsePromise = new Promise<Response>((resolve) => {
    releaseResponse = resolve;
  });
  const restoreFetch = setGlobalProperty("fetch", async () => {
    csrfFetches += 1;
    return responsePromise;
  });
  try {
    const http = loadHttpModule();
    const requests = [
      http.ensureCsrfToken(),
      http.ensureCsrfToken(),
      http.ensureCsrfToken(),
    ];
    strictEqual(csrfFetches, 1);
    releaseResponse(
      new Response(JSON.stringify({ csrf_token: "csrf-1" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    deepStrictEqual(await Promise.all(requests), [
      "csrf-1",
      "csrf-1",
      "csrf-1",
    ]);
    strictEqual(csrfFetches, 1);
  } finally {
    restoreFetch();
    restoreDocument();
  }
});

test("failed csrf refresh clears the flight so the next request can recover", async () => {
  const restoreDocument = setGlobalProperty("document", { cookie: "" });
  let csrfFetches = 0;
  const restoreFetch = setGlobalProperty("fetch", async () => {
    csrfFetches += 1;
    if (csrfFetches === 1) return new Response(null, { status: 503 });
    return new Response(JSON.stringify({ csrf_token: "csrf-recovered" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  try {
    const http = loadHttpModule();
    strictEqual(await http.ensureCsrfToken(), null);
    strictEqual(await http.ensureCsrfToken(), "csrf-recovered");
    strictEqual(csrfFetches, 2);
  } finally {
    restoreFetch();
    restoreDocument();
  }
});

test("logout invalidates an in-flight csrf refresh even when fetch ignores abort", async () => {
  const restoreDocument = setGlobalProperty("document", { cookie: "" });
  let releaseStale!: (response: Response) => void;
  const staleResponse = new Promise<Response>((resolve) => {
    releaseStale = resolve;
  });
  let csrfFetches = 0;
  const restoreFetch = setGlobalProperty("fetch", async () => {
    csrfFetches += 1;
    if (csrfFetches === 1) return staleResponse;
    return new Response(JSON.stringify({ csrf_token: "csrf-new" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  try {
    const http = loadHttpModule();
    const staleRefresh = http.refreshCsrfToken();
    const apiClient = loadCommonJsModule(source, "apiClient.ts", {
      "./api/http": {
        API_BASE: "/api",
        ApiError: http.ApiError,
        apiFetch: async () => undefined,
        apiFetchNoContent: async () => undefined,
        ensureCsrfToken: http.ensureCsrfToken,
        handle401: http.handle401,
        invalidateSessionClientState: http.invalidateSessionClientState,
        refreshCsrfToken: http.refreshCsrfToken,
        resumeSessionClientState: http.resumeSessionClientState,
        safeAuthNextPath: () => "/",
      },
    }) as {
      logout(): Promise<void>;
    };
    await apiClient.logout();
    releaseStale(
      new Response(JSON.stringify({ csrf_token: "csrf-stale" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    strictEqual(await staleRefresh, null);
    strictEqual(await http.ensureCsrfToken(), "csrf-new");
    strictEqual(csrfFetches, 2);
  } finally {
    restoreFetch();
    restoreDocument();
  }
});

test("401 clears private client state and replaces history with a safe next path", async () => {
  let privateCleanupCalls = 0;
  let replacedWith = "";
  const restoreWindow = setGlobalProperty("window", {
    location: {
      origin: "https://lumen.example",
      pathname: "/projects/private",
      search: "?view=board",
      hash: "#node-1",
      replace(path: string) {
        replacedWith = path;
      },
    },
  });
  try {
    const http = loadHttpModule(async () => {
      privateCleanupCalls += 1;
    });
    strictEqual(
      http.safeAuthNextPath(
        "https://evil.example/steal",
        "https://lumen.example",
      ),
      "/",
    );
    strictEqual(
      http.safeAuthNextPath(
        "/projects/private?view=board#node-1",
        "https://lumen.example",
      ),
      "/projects/private?view=board#node-1",
    );
    http.handle401();
    await new Promise<void>((resolve) => setImmediate(resolve));
    strictEqual(privateCleanupCalls, 1);
    strictEqual(
      replacedWith,
      "/login?next=%2Fprojects%2Fprivate%3Fview%3Dboard%23node-1",
    );
    doesNotMatch(httpSource, /useChatStore|router\.replace|location\.assign/);
    match(httpSource, /document teardown 会清空 React Query、Zustand/);
  } finally {
    restoreWindow();
  }
});

test("login uses backend cookie signals for HTTP diagnosis and otherwise stays generic", async () => {
  const http = loadHttpModule();
  const calls: string[] = [];
  const resumedUsers: string[] = [];
  const verifiedUser = { id: "user-1", email: "user@example.com" };
  const apiClient = loadCommonJsModule(source, "apiClient.ts", {
    "./api/http": {
      API_BASE: "/api",
      ApiError: http.ApiError,
      apiFetch: async (path: string) => {
        calls.push(path);
        return path === "/auth/me" ? verifiedUser : { id: "bootstrap-user" };
      },
      apiFetchNoContent: async () => undefined,
      ensureCsrfToken: async () => null,
      handle401: () => undefined,
      invalidateSessionClientState: async () => undefined,
      refreshCsrfToken: async () => null,
      resumeSessionClientState: async (userId: string) => {
        resumedUsers.push(userId);
      },
      safeAuthNextPath: () => "/",
    },
  }) as {
    login(email: string, password: string): Promise<{ id: string }>;
  };
  deepStrictEqual(
    await apiClient.login("user@example.com", "secret"),
    verifiedUser,
  );
  deepStrictEqual(calls, ["/auth/login", "/auth/me"]);
  deepStrictEqual(resumedUsers, ["user-1"]);

  const restoreWindow = setGlobalProperty("window", {
    location: { protocol: "http:" },
  });
  try {
    const failedClient = loadCommonJsModule(source, "apiClient.ts", {
      "./api/http": {
        API_BASE: "/api",
        ApiError: http.ApiError,
        apiFetch: async (path: string) => {
          if (path === "/auth/me") {
            throw new http.ApiError({
              code: "unauthorized",
              message: "unauthorized",
              status: 401,
              payload: { session_cookie_secure: true },
            });
          }
          return {
            id: "bootstrap-user",
            session_cookie_secure: true,
          };
        },
        apiFetchNoContent: async () => undefined,
        ensureCsrfToken: async () => null,
        handle401: () => undefined,
        invalidateSessionClientState: async () => undefined,
        refreshCsrfToken: async () => null,
        resumeSessionClientState: async () => undefined,
        safeAuthNextPath: () => "/",
      },
    }) as {
      login(email: string, password: string): Promise<{ id: string }>;
    };
    await rejects(
      failedClient.login("user@example.com", "secret"),
      (err: unknown) => {
        if (!(err instanceof http.ApiError)) return false;
        const apiError = err as TestApiError;
        return (
          apiError.code === "secure_cookie_requires_https" &&
          /HTTP/.test(apiError.message) &&
          /Secure/.test(apiError.message) &&
          /HTTPS/.test(apiError.message)
        );
      },
    );

    const genericClient = loadCommonJsModule(source, "apiClient.ts", {
      "./api/http": {
        API_BASE: "/api",
        ApiError: http.ApiError,
        apiFetch: async (path: string) => {
          if (path === "/auth/me") {
            throw new http.ApiError({
              code: "unauthorized",
              message: "unauthorized",
              status: 401,
            });
          }
          return { id: "bootstrap-user" };
        },
        apiFetchNoContent: async () => undefined,
        ensureCsrfToken: async () => null,
        handle401: () => undefined,
        invalidateSessionClientState: async () => undefined,
        refreshCsrfToken: async () => null,
        resumeSessionClientState: async () => undefined,
        safeAuthNextPath: () => "/",
      },
    }) as {
      login(email: string, password: string): Promise<{ id: string }>;
    };
    await rejects(
      genericClient.login("user@example.com", "secret"),
      (err: unknown) =>
        err instanceof http.ApiError &&
        (err as TestApiError).code === "session_unverified",
    );
  } finally {
    restoreWindow();
  }

  match(loginSource, /const next = safeAuthNextPath\(rawNext\)/);
  match(loginSource, /router\.replace\(next\)/);
  match(loginSource, /err\.code === "session_unverified"/);
  doesNotMatch(source, /const insecureHttp =[\s\S]*location\.protocol/);
  doesNotMatch(loginSource, /function safeNextPath/);
});

test("getMe is a pure identity request without persistence side effects", async () => {
  const http = loadHttpModule();
  const resumedUsers: string[] = [];
  const apiClient = loadCommonJsModule(source, "apiClient.ts", {
    "./api/http": {
      API_BASE: "/api",
      ApiError: http.ApiError,
      apiFetch: async () => ({ id: "user-stale" }),
      apiFetchNoContent: async () => undefined,
      ensureCsrfToken: async () => null,
      handle401: () => undefined,
      invalidateSessionClientState: async () => undefined,
      refreshCsrfToken: async () => null,
      resumeSessionClientState: async (userId: string) => {
        resumedUsers.push(userId);
      },
      safeAuthNextPath: () => "/",
    },
  }) as {
    getMe(): Promise<{ id: string }>;
  };

  deepStrictEqual(await apiClient.getMe(), { id: "user-stale" });
  deepStrictEqual(resumedUsers, []);
});

test("redeemCode sends an idempotency key generated from crypto.randomUUID with fallback", () => {
  match(source, /function createIdempotencyKey\(\): string/);
  match(source, /crypto\.randomUUID\(\)/);
  match(source, /return uuid\(\);/);
  match(source, /headers: \{ "Idempotency-Key": createIdempotencyKey\(\) \}/);
});

test("exportMyData retries once after refreshing a stale csrf token", () => {
  match(source, /async function exportApiErrorFromResponse\(res: Response\)/);
  match(source, /if \(res\.status === 403\)/);
  match(source, /err\.code !== "csrf_failed"/);
  match(source, /refreshCsrfToken\(\)\.catch\(\(\) => null\)/);
  match(source, /res = await doFetch\(fresh\)/);
});

test("apiClient preserves poster style exports through the focused module", () => {
  match(source, /export \* from "\.\/api\/posterStyles";/);
  doesNotMatch(source, /export interface PosterStyleItem/);
  match(posterStylesSource, /export interface PosterStyleItem/);
  match(posterStylesSource, /export function listPosterStyles/);
});

test("poster style requests reuse the shared HTTP helper", () => {
  match(posterStylesSource, /import \{ apiFetch \} from "\.\/http";/);
  match(
    posterStylesSource,
    /return apiFetch<PosterStyleGenerateOut>\("\/poster-styles\/generate"/,
  );
  doesNotMatch(posterStylesSource, /\bfetch\s*\(/);
});

test("apiClient preserves task, storyboard, and workflow exports through focused modules", () => {
  match(source, /export \* from "\.\/api\/tasks";/);
  match(source, /export \* from "\.\/api\/storyboards";/);
  match(source, /export \* from "\.\/api\/workflows";/);

  doesNotMatch(source, /export interface BackendGeneration/);
  doesNotMatch(source, /export interface StoryboardRun/);
  doesNotMatch(source, /export interface WorkflowRun/);
  doesNotMatch(source, /export function listStoryboards/);
  doesNotMatch(source, /export function listWorkflows/);

  match(tasksSource, /export interface BackendGeneration/);
  match(tasksSource, /export interface BackendCompletion/);
  match(tasksSource, /export interface BackendImageMeta/);
  match(storyboardsSource, /export interface StoryboardRun/);
  match(storyboardsSource, /export function assembleStoryboard/);
  match(workflowsSource, /export interface WorkflowRun/);
  match(workflowsSource, /export function completeWorkflowDelivery/);
});

test("apiClient preserves video asset and poster workflow ABI through focused modules", () => {
  match(source, /from "\.\/api\/videoAssets";/);
  match(source, /export \* from "\.\/api\/posterWorkflows";/);
  match(videoAssetsSource, /export const DEFAULT_VIDEO_ASSET_QUOTAS/);
  match(videoAssetsSource, /export function listVideoAssetGroups/);
  match(videoAssetsSource, /export function retryVideoAssetOperation/);
  match(posterWorkflowsSource, /export interface PosterDesignWorkflowCreateIn/);
  match(posterWorkflowsSource, /export function createPosterDesignWorkflow/);
  match(typesSource, /from "\.\/videoAssetTypes";/);
  match(videoAssetTypesSource, /export interface VideoAssetOperationOut/);
});

test("apiClient preserves conversation, prompt, and image exports through focused modules", () => {
  match(source, /export \* from "\.\/api\/conversations";/);
  match(source, /export \* from "\.\/api\/systemPrompts";/);
  match(source, /export \* from "\.\/api\/images";/);
  match(source, /export \* from "\.\/api\/account";/);

  doesNotMatch(source, /export interface ConversationSummary/);
  doesNotMatch(source, /export interface SystemPrompt/);
  doesNotMatch(source, /export interface UploadedImage/);

  match(conversationsSource, /export interface ConversationSummary/);
  match(conversationsSource, /export function listMessages/);
  match(systemPromptsSource, /export function listSystemPrompts/);
  match(imagesSource, /export function uploadImage/);
  match(imagesSource, /export function imageVariantUrl/);
  match(accountSource, /export function listMySessions/);
  match(accountSource, /export function deleteMyAccount/);
});

test("focused storyboard and workflow requests reuse the shared HTTP helper", () => {
  match(storyboardsSource, /import \{ apiFetch \} from "\.\/http";/);
  match(workflowsSource, /import \{ apiFetch \} from "\.\/http";/);
  match(
    storyboardsSource,
    /return apiFetch<StoryboardListResponse>\(`\/storyboards\$\{suffix\}`\)/,
  );
  match(
    workflowsSource,
    /return apiFetch<WorkflowRunListResponse>\(`\/workflows\$\{suffix\}`\)/,
  );
  doesNotMatch(storyboardsSource, /\bfetch\s*\(/);
  doesNotMatch(workflowsSource, /\bfetch\s*\(/);
});

test("API and type facades stay within architecture budgets", () => {
  const lines = (value: string) => value.trimEnd().split("\n").length;

  ok(lines(source) <= 2563, `apiClient.ts is ${lines(source)} lines`);
  ok(lines(typesSource) <= 1500, `types.ts is ${lines(typesSource)} lines`);
  ok(
    lines(videoAssetsSource) <= 1500,
    `videoAssets.ts is ${lines(videoAssetsSource)} lines`,
  );
  ok(
    lines(posterWorkflowsSource) <= 1500,
    `posterWorkflows.ts is ${lines(posterWorkflowsSource)} lines`,
  );
  ok(
    lines(videoAssetTypesSource) <= 1500,
    `videoAssetTypes.ts is ${lines(videoAssetTypesSource)} lines`,
  );
});

test("apiClient and focused modules compile with the project TypeScript config", () => {
  const webRoot = fileURLToPath(new URL("../../", import.meta.url));
  const configPath = fileURLToPath(
    new URL("../../tsconfig.json", import.meta.url),
  );
  const rootNames = [
    "./apiClient.ts",
    "./api/http.ts",
    "./api/tasks.ts",
    "./api/storyboards.ts",
    "./api/workflows.ts",
    "./api/posterWorkflows.ts",
    "./api/videoAssets.ts",
    "./api/admin.ts",
    "./api/system.ts",
    "./api/billing.ts",
    "./api/memory.ts",
    "./api/conversations.ts",
    "./api/systemPrompts.ts",
    "./api/images.ts",
    "./api/account.ts",
    "./videoAssetTypes.ts",
    "../app/login/page.tsx",
  ].map((relativePath) =>
    fileURLToPath(new URL(relativePath, import.meta.url)),
  );
  const rootNameSet = new Set(rootNames);

  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  strictEqual(
    config.error,
    undefined,
    config.error
      ? ts.flattenDiagnosticMessageText(config.error.messageText, "\n")
      : undefined,
  );
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, webRoot);
  strictEqual(
    parsed.errors.length,
    0,
    ts.formatDiagnostics(parsed.errors, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );

  const program = ts.createProgram({
    rootNames,
    options: { ...parsed.options, incremental: false, noEmit: true },
  });
  const diagnostics = ts
    .getPreEmitDiagnostics(program)
    .filter(
      (diagnostic) =>
        diagnostic.file == null || rootNameSet.has(diagnostic.file.fileName),
    );
  strictEqual(
    diagnostics.length,
    0,
    ts.formatDiagnostics(diagnostics, {
      getCanonicalFileName: (fileName) => fileName,
      getCurrentDirectory: () => webRoot,
      getNewLine: () => "\n",
    }),
  );
});
