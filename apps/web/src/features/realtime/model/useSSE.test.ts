import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  ok,
  rejects,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import * as ts from "typescript";

const source = readFileSync(new URL("./useSSE.ts", import.meta.url), "utf8");
const subscriptionSource = readFileSync(
  new URL("./sseSubscription.ts", import.meta.url),
  "utf8",
);
const lumenSource = readFileSync(
  new URL("./useLumenRealtime.ts", import.meta.url),
  "utf8",
);
const runtimeSource = readFileSync(
  new URL("./runtime.ts", import.meta.url),
  "utf8",
);

type SSESubscriptionModule = typeof import("./sseSubscription");
type SSECallbackInvocation = Parameters<
  SSESubscriptionModule["dispatchSSECallbackForScope"]
>[4];

type LumenSnapshotHelpers = {
  shouldSkipRecentSnapshot: (
    recent: {
      userScope: string;
      userId: string | null;
      identityEpoch: number;
      syncedAt: number;
    },
    identity: {
      userScope: string;
      userId: string | null;
      identityEpoch: number;
    },
    now?: number,
  ) => boolean;
};

function loadSubscriptionHelpers(): SSESubscriptionModule {
  const output = ts.transpileModule(
    subscriptionSource,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as SSESubscriptionModule,
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports;
}

function loadLumenSnapshotHelpers(): LumenSnapshotHelpers {
  const start = lumenSource.indexOf("const RECENT_SNAPSHOT_WINDOW_MS");
  const end = lumenSource.indexOf("function staleSnapshotError", start);
  const output = ts.transpileModule(
    `${lumenSource.slice(start, end)}
module.exports.shouldSkipRecentSnapshot = shouldSkipRecentSnapshot;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as LumenSnapshotHelpers,
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports;
}

const helpers = loadSubscriptionHelpers();
const lumenSnapshotHelpers = loadLumenSnapshotHelpers();

test("SSE retry delay grows exponentially and remains capped", () => {
  deepEqual(
    [0, 1, 2, 5, 20].map(helpers.getSSEBackoffBaseDelay),
    [1_000, 2_000, 4_000, 30_000, 30_000],
  );
});

test("SSE scope fence drops user A events across the B commit and cleanup window", () => {
  const delivered: string[] = [];
  const handlers = {
    "completion.image": () => delivered.push("user-b"),
  };
  let activeUserScope = "user:user-b";
  const validateScope = (scopeIdentity: string) =>
    scopeIdentity === activeUserScope;

  equal(
    helpers.isSSEScopeCurrent(
      "user:user-a",
      "user:user-a",
      validateScope,
    ),
    false,
  );
  equal(
    helpers.dispatchSSEEventForScope(
      "user:user-a",
      "user:user-b",
      validateScope,
      handlers,
      "completion.image",
      {},
      "cursor-a",
    ),
    false,
  );
  deepEqual(delivered, []);

  activeUserScope = "user:user-b";
  equal(
    helpers.dispatchSSEEventForScope(
      "user:user-b",
      "user:user-b",
      validateScope,
      handlers,
      "completion.image",
      {},
      "cursor-b",
    ),
    true,
  );
  deepEqual(delivered, ["user-b"]);
  match(source, /const subscribedScope = scopeIdentity;/);
  match(source, /emitScopedCallback/);
  match(source, /createSSESubscriber\(\{/);
  match(lumenSource, /scopeIdentity: userScope,/);
  match(
    lumenSource,
    /useChatStore\.getState\(\)\.currentUserId === userId/,
  );
});

test("SSE callback dispatch keeps every callback behind the same scope fence", () => {
  const calls: string[] = [];
  const bindings = {
    handlers: {
      "completion.image": (_data: unknown, id: string) =>
        calls.push(`event:${id}`),
    },
    onOpen: () => calls.push("open"),
    onError: () => calls.push("error"),
    onControl: () => calls.push("control"),
    onAuthInvalidated: () => calls.push("auth"),
    setStatus: (status: "connecting" | "open" | "closed" | "error") =>
      calls.push(`status:${status}`),
  };
  const context = {
    connectionGeneration: 1,
    userScope: "user:user-b",
    isCurrent: () => true,
  };
  const invocations: SSECallbackInvocation[] = [
    {
      kind: "event",
      name: "completion.image",
      data: {},
      id: "cursor-b",
    },
    { kind: "open", event: new Event("open"), context },
    { kind: "error", event: new Event("error") },
    {
      kind: "control",
      event: { kind: "control", type: "heartbeat", version: 1 },
    },
    { kind: "auth-invalidated" },
    { kind: "status", status: "open" },
  ];

  for (const invocation of invocations) {
    equal(
      helpers.dispatchSSECallbackForScope(
        "user:user-a",
        "user:user-b",
        undefined,
        bindings,
        invocation,
      ),
      false,
    );
  }
  deepEqual(calls, []);

  for (const invocation of invocations) {
    equal(
      helpers.dispatchSSECallbackForScope(
        "user:user-b",
        "user:user-b",
        undefined,
        bindings,
        invocation,
      ),
      true,
    );
  }
  deepEqual(calls, [
    "event:cursor-b",
    "open",
    "error",
    "control",
    "auth",
    "status:open",
  ]);
});

test("SSE snapshot recovery rejects stale ownership before invoking the adapter", async () => {
  let recoveries = 0;
  const recoverSnapshot = async () => {
    recoveries += 1;
    return { cursor: "cursor-b" };
  };
  const reason = { kind: "recovery_required" as const, reason: "test" };
  const signal = new AbortController().signal;
  const context = {
    connectionGeneration: 2,
    userScope: "user:user-b",
    isCurrent: () => true,
  };

  await rejects(
    helpers.recoverSSESnapshotForScope(
      "user:user-a",
      "user:user-b",
      undefined,
      recoverSnapshot,
      [],
      reason,
      signal,
      context,
    ),
    {
      name: "AbortError",
      message: "stale SSE subscription scope",
    },
  );
  equal(recoveries, 0);
  deepEqual(
    await helpers.recoverSSESnapshotForScope(
      "user:user-b",
      "user:user-b",
      undefined,
      recoverSnapshot,
      [],
      reason,
      signal,
      context,
    ),
    { cursor: "cursor-b" },
  );
  equal(recoveries, 1);
});

test("same-user recovery after a fail-closed reset bypasses the recent snapshot cache", () => {
  const recent = {
    userScope: "user:user-a",
    userId: "user-a",
    identityEpoch: 7,
    syncedAt: 10_000,
  };

  equal(
    lumenSnapshotHelpers.shouldSkipRecentSnapshot(
      recent,
      {
        userScope: "user:user-a",
        userId: "user-a",
        identityEpoch: 7,
      },
      10_500,
    ),
    true,
  );
  equal(
    lumenSnapshotHelpers.shouldSkipRecentSnapshot(
      recent,
      {
        // The fail-closed null transition and same-user recovery each advance
        // the durable private identity epoch after client task state is cleared.
        userScope: "user:user-a",
        userId: "user-a",
        identityEpoch: 9,
      },
      10_500,
    ),
    false,
  );
  match(lumenSource, /reason\.kind === "initial_snapshot"/);
  match(
    lumenSource,
    /shouldSkipRecentSnapshot\(\s*lastSnapshot\.current,\s*\{[\s\S]*?identityEpoch,\s*\}\s*\)/,
  );
});

test("SSE defaults to infinite retry and exposes immediate reconnect", () => {
  match(
    subscriptionSource,
    /DEFAULT_MAX_RETRY_COUNT = Number\.POSITIVE_INFINITY/,
  );
  match(source, /runtimeRef\.current\?\.reconnect\(\)/);
  match(source, /return \{ status, reconnect \};/);
});

test("SSE subscriber adapter binds scope and registers only real recovery", async () => {
  const emitted: string[] = [];
  const subscriber = helpers.createSSESubscriber({
    subscribedScope: "user:user-a",
    eventNames: ["completion.image"],
    emit: (scope, invocation) =>
      emitted.push(`${scope}:${invocation.kind}`),
  });
  equal(subscriber.recoverSnapshot, undefined);
  equal(subscriber.maxRetryCount, Number.POSITIVE_INFINITY);
  subscriber.handlers["completion.image"]({}, "cursor-a");
  deepEqual(emitted, ["user:user-a:event"]);

  const recoverable = helpers.createSSESubscriber({
    subscribedScope: "user:user-b",
    eventNames: [],
    emit: () => {},
    recoverSnapshot: async (scope) => ({ cursor: scope }),
  });
  const recoverSnapshot = recoverable.recoverSnapshot;
  ok(recoverSnapshot);
  const result = await recoverSnapshot(
    [],
    { kind: "recovery_required", reason: "test" },
    new AbortController().signal,
    {
      connectionGeneration: 3,
      userScope: "user:user-b",
      isCurrent: () => true,
    },
  );
  deepEqual(result, { cursor: "user:user-b" });

  match(
    source,
    /const hasRecoveryAdapter = typeof options\.recoverSnapshot === "function";/,
  );
  match(
    source,
    /recoverSnapshot: hasRecoveryAdapter[\s\S]*?emitRecoverSnapshot[\s\S]*?: undefined,/,
  );
  match(
    source,
    /\[\s*channelKey,\s*eventKey,\s*hasRecoveryAdapter,/,
  );
});

test("initial snapshots are runtime-owned and fenced by connection, scope, and identity", () => {
  doesNotMatch(lumenSource, /onOpen:\s*\(/);
  match(runtimeSource, /snapshotRequired: this\.hasSnapshotAdapters\(\)/);
  match(runtimeSource, /const controller = new AbortController\(\)/);
  match(runtimeSource, /this\.recoveryAbort\?\.abort\(\)/);
  match(
    lumenSource,
    /assertSnapshotCurrent\(\s*signal,\s*context,\s*userScope,\s*userId,\s*identityEpoch,\s*\)/,
  );
  match(lumenSource, /!context\.isCurrent\(\)/);
});
