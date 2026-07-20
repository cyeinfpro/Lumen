import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  ok,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import {
  onlineManager,
  QueryClient,
  QueryObserver,
  type QueryKey,
} from "@tanstack/react-query";
import * as ts from "typescript";

const source = readFileSync(
  new URL("./DesktopAccountMenu.tsx", import.meta.url),
  "utf8",
);
const queryProviderSource = readFileSync(
  new URL("../../QueryProvider.tsx", import.meta.url),
  "utf8",
);
const queryIdentitySource = readFileSync(
  new URL("../../../lib/queries/userScope.ts", import.meta.url),
  "utf8",
);
const runtimeDefaultsSource = readFileSync(
  new URL("../../RuntimeDefaultsBootstrap.tsx", import.meta.url),
  "utf8",
);
const identityRevalidationSource = readFileSync(
  new URL("../../useIdentityRevalidation.ts", import.meta.url),
  "utf8",
);
const taskIslandSource = readFileSync(
  new URL("../tray/TaskIsland.tsx", import.meta.url),
  "utf8",
);
const taskCenterSource = readFileSync(
  new URL("../tray/TaskCenter.tsx", import.meta.url),
  "utf8",
);
const globalTaskTraySource = readFileSync(
  new URL("../GlobalTaskTray.tsx", import.meta.url),
  "utf8",
);
const conversationMemorySource = readFileSync(
  new URL("../chat/ConversationMemoryButton.tsx", import.meta.url),
  "utf8",
);
const memoryPageSource = readFileSync(
  new URL("../../../app/settings/memory/page.tsx", import.meta.url),
  "utf8",
);
const mobileTopBarSource = readFileSync(
  new URL("./MobileTopBar.tsx", import.meta.url),
  "utf8",
);
const accountCenterSource = readFileSync(
  new URL("../me/AccountCenter.tsx", import.meta.url),
  "utf8",
);
const composerCostSource = readFileSync(
  new URL("../composer/shared/useComposerCostEstimate.ts", import.meta.url),
  "utf8",
);
const usagePageSource = readFileSync(
  new URL("../../../app/settings/usage/page.tsx", import.meta.url),
  "utf8",
);
const billingPanelSource = readFileSync(
  new URL("../../../app/admin/_panels/BillingPanel.tsx", import.meta.url),
  "utf8",
);
const walletPageSource = readFileSync(
  new URL("../../../app/me/wallet/page.tsx", import.meta.url),
  "utf8",
);
const popoverSource = readFileSync(
  new URL("../composer/desktop/DesktopPopover.tsx", import.meta.url),
  "utf8",
);
const { calculateDesktopPopoverPosition } = await import(
  new URL(
    "../composer/desktop/desktopPopoverPosition.ts",
    import.meta.url,
  ).href
);

type QueryProviderHelpers = {
  AUTH_USER_QUERY_KEY: readonly ["me"];
  userScopedQueryKey: (
    userId: string | null | undefined,
    queryKey: QueryKey,
  ) => QueryKey;
  clearPreviousUserQueryCache: (
    client: QueryClient,
    previousUserId: string,
  ) => void;
  prepareUserIdentityRevalidation: (
    client: QueryClient,
    previousUserId: string | null | undefined,
  ) => void;
  userBillingQueryKeys: {
    all: (userId: string | null | undefined) => QueryKey;
    wallet: (userId: string | null | undefined) => QueryKey;
    walletTransactions: (
      userId: string | null | undefined,
      params: {
        kind: string;
        limit: number;
        pagination: "infinite" | "list";
      },
    ) => QueryKey;
    pricing: (userId: string | null | undefined) => QueryKey;
    snapshot: (userId: string | null | undefined) => QueryKey;
    redemptions: (
      userId: string | null | undefined,
      params: {
        limit: number;
        pagination: "infinite" | "list";
      },
    ) => QueryKey;
  };
  userMemoryQueryKeys: {
    all: (userId: string | null | undefined) => QueryKey;
    settings: (userId: string | null | undefined) => QueryKey;
    scopes: (userId: string | null | undefined) => QueryKey;
    items: (userId: string | null | undefined, scopeId: string) => QueryKey;
    staging: (userId: string | null | undefined) => QueryKey;
    timeline: (userId: string | null | undefined) => QueryKey;
  };
  userTaskQueryKeys: {
    all: (userId: string | null | undefined) => QueryKey;
    recent: (
      userId: string | null | undefined,
      status?: "all" | "active" | "failed",
    ) => QueryKey;
    islandActive: (userId: string | null | undefined) => QueryKey;
    islandRecent: (userId: string | null | undefined) => QueryKey;
    presence: (userId: string | null | undefined) => QueryKey;
  };
  userConversationQueryKeys: {
    detail: (
      userId: string | null | undefined,
      conversationId: string,
    ) => QueryKey;
    usedMemories: (
      userId: string | null | undefined,
      conversationId: string,
    ) => QueryKey;
  };
};

function loadQueryProviderHelpers(): QueryProviderHelpers {
  const start = queryIdentitySource.indexOf(
    "export const AUTH_USER_QUERY_KEY",
  );
  const end = queryIdentitySource.indexOf(
    "export function useUserQueryScope",
  );
  ok(start >= 0 && end > start, "missing query identity helper block");
  const output = ts.transpileModule(
    queryIdentitySource.slice(start, end),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = { exports: {} as QueryProviderHelpers };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports;
}

function plainValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function loadIdentityRetryDelay(): (attempt: number) => number {
  const start = identityRevalidationSource.indexOf(
    "const IDENTITY_REVALIDATION_RETRY_DELAYS_MS",
  );
  const end = identityRevalidationSource.indexOf(
    "function isUnauthorizedIdentityError",
    start,
  );
  ok(start >= 0 && end > start, "missing identity retry policy");
  const output = ts.transpileModule(
    `${identityRevalidationSource.slice(start, end)}
module.exports.getIdentityRevalidationRetryDelay =
  getIdentityRevalidationRetryDelay;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as {
      getIdentityRevalidationRetryDelay: (attempt: number) => number;
    },
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports.getIdentityRevalidationRetryDelay;
}

function loadIdentityErrorPolicy() {
  class TestApiError extends Error {
    status: number;

    constructor(status: number) {
      super(`HTTP ${status}`);
      this.status = status;
    }
  }

  const start = identityRevalidationSource.indexOf(
    "function isUnauthorizedIdentityError",
  );
  const end = identityRevalidationSource.indexOf("function isAuthUser", start);
  ok(start >= 0 && end > start, "missing identity error policy");
  const output = ts.transpileModule(
    `${identityRevalidationSource.slice(start, end)}
module.exports.isUnauthorizedIdentityError = isUnauthorizedIdentityError;
module.exports.isRetryableIdentityError = isRetryableIdentityError;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as {
      isUnauthorizedIdentityError: (error: unknown) => boolean;
      isRetryableIdentityError: (error: unknown) => boolean;
    },
  };
  runInNewContext(output, {
    ApiError: TestApiError,
    Error,
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return {
    ApiError: TestApiError,
    ...moduleRecord.exports,
  };
}

test("desktop account menu exposes admin routes to administrators", () => {
  match(source, /if \(isAdmin\) \{/);
});

test("user query keys isolate accounts and identity changes clear only old user data", () => {
  const helpers = loadQueryProviderHelpers();
  const client = new QueryClient();
  const userATasks = helpers.userScopedQueryKey("user-a", ["tasks"]);
  const userBTasks = helpers.userScopedQueryKey("user-b", ["tasks"]);
  const unknownTasks = helpers.userScopedQueryKey(null, ["tasks"]);

  deepEqual(plainValue(userATasks), ["user", "user-a", "tasks"]);
  deepEqual(plainValue(userBTasks), ["user", "user-b", "tasks"]);
  deepEqual(plainValue(unknownTasks), [
    "user",
    "__identity_unknown__",
    "tasks",
  ]);

  client.setQueryData(userATasks, "a");
  client.setQueryData(userBTasks, "b");
  client.setQueryData(["tasks", "legacy"], "legacy-a");
  client.setQueryData(helpers.AUTH_USER_QUERY_KEY, { id: "user-b" });
  client.setQueryData(["auth", "api-suppliers"], ["public"]);

  helpers.clearPreviousUserQueryCache(client, "user-a");

  equal(client.getQueryData(userATasks), undefined);
  equal(client.getQueryData(["tasks", "legacy"]), undefined);
  equal(client.getQueryData(userBTasks), "b");
  deepEqual(client.getQueryData(helpers.AUTH_USER_QUERY_KEY), {
    id: "user-b",
  });
  deepEqual(client.getQueryData(["auth", "api-suppliers"]), ["public"]);
});

test("initial identity establishment does not purge a fresh query client", () => {
  match(
    queryProviderSource,
    /if \(previousUserId !== userId && previousUserId\) \{\s*clearPreviousUserQueryCache\(\s*client,\s*previousUserId,\s*\);/,
  );
  doesNotMatch(
    queryProviderSource,
    /previousUserId !== userId && \(previousUserId \|\| userId\)/,
  );
});

test("a changed authenticated user clears old private data and keeps the new auth result", () => {
  const start = identityRevalidationSource.indexOf(
    "const currentUserId = useChatStore.getState().currentUserId;",
  );
  const end = identityRevalidationSource.indexOf(
    "state.request = null;",
    start,
  );
  ok(start >= 0 && end > start, "missing changed-user acceptance branch");
  const branch = identityRevalidationSource.slice(start, end);

  match(branch, /currentUserId && currentUserId !== user\.id/);
  match(branch, /setCurrentUser\(null\)/);
  match(branch, /clearPreviousUserQueryCache\(queryClient, currentUserId\)/);
  doesNotMatch(branch, /removeAuthUserQuery/);
  match(
    identityRevalidationSource.slice(end),
    /setCurrentUser\(user\.id\)/,
  );
});

test("identity cache cleanup clears data from mounted observers before removal", () => {
  const helpers = loadQueryProviderHelpers();
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  const userAKey = helpers.userScopedQueryKey("user-a", ["tasks"]);
  const legacyKey = ["me", "memory", "staging"] as const;
  client.setQueryData(userAKey, "user-a");
  client.setQueryData(legacyKey, "legacy-user-a");

  const userAObserver = new QueryObserver(client, {
    queryKey: userAKey,
    queryFn: async () => "refetched-user-a",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const legacyObserver = new QueryObserver(client, {
    queryKey: legacyKey,
    queryFn: async () => "refetched-legacy-user-a",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const userAResults: unknown[] = [];
  const legacyResults: unknown[] = [];
  const stopUserA = userAObserver.subscribe((result) =>
    userAResults.push(result.data),
  );
  const stopLegacy = legacyObserver.subscribe((result) =>
    legacyResults.push(result.data),
  );

  try {
    equal(userAObserver.getCurrentResult().data, "user-a");
    equal(legacyObserver.getCurrentResult().data, "legacy-user-a");

    helpers.clearPreviousUserQueryCache(client, "user-a");

    equal(userAObserver.getCurrentResult().data, undefined);
    equal(legacyObserver.getCurrentResult().data, undefined);
    equal(userAResults[userAResults.length - 1], undefined);
    equal(legacyResults[legacyResults.length - 1], undefined);
    equal(client.getQueryData(userAKey), undefined);
    equal(client.getQueryData(legacyKey), undefined);
  } finally {
    stopUserA();
    stopLegacy();
  }
});

test("identity revalidation clears a mounted old-user observer before accepting the changed user", async () => {
  const helpers = loadQueryProviderHelpers();
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  const userAKey = helpers.userScopedQueryKey("user-a", ["tasks"]);
  client.setQueryData(helpers.AUTH_USER_QUERY_KEY, { id: "user-a" });
  client.setQueryData(userAKey, "user-a-private-data");

  let identityFetches = 0;
  const authObserver = new QueryObserver<{ id: string }>(client, {
    queryKey: helpers.AUTH_USER_QUERY_KEY,
    queryFn: async () => {
      identityFetches += 1;
      return { id: "user-b" };
    },
    networkMode: "online",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const privateObserver = new QueryObserver<string>(client, {
    queryKey: userAKey,
    queryFn: async () => "unexpected-old-user-refetch",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const privateResults: Array<string | undefined> = [];
  const stopAuth = authObserver.subscribe(() => undefined);
  const stopPrivate = privateObserver.subscribe((result) =>
    privateResults.push(result.data),
  );
  let currentUserId: string | null = "user-a";
  client.mount();
  onlineManager.setOnline(false);

  try {
    equal(privateObserver.getCurrentResult().data, "user-a-private-data");

    const previousUserId = currentUserId;
    currentUserId = null;
    helpers.prepareUserIdentityRevalidation(client, previousUserId);
    const revalidation = authObserver.refetch({ cancelRefetch: true });

    equal(currentUserId, null);
    equal(authObserver.getCurrentResult().data, undefined);
    equal(privateObserver.getCurrentResult().data, undefined);
    equal(privateResults[privateResults.length - 1], undefined);
    equal(authObserver.getCurrentResult().fetchStatus, "paused");
    equal(identityFetches, 0);

    onlineManager.setOnline(true);
    const result = await revalidation;
    currentUserId = result.data?.id ?? null;

    equal(currentUserId, "user-b");
    equal(identityFetches, 1);
    deepEqual(
      plainValue(client.getQueryData(helpers.AUTH_USER_QUERY_KEY)),
      { id: "user-b" },
    );
    equal(client.getQueryData(userAKey), undefined);
  } finally {
    onlineManager.setOnline(true);
    client.unmount();
    stopAuth();
    stopPrivate();
  }
});

test("transient identity failure stays fail-closed before a later response restores the user", async () => {
  const helpers = loadQueryProviderHelpers();
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  const userAKey = helpers.userScopedQueryKey("user-a", ["tasks"]);
  client.setQueryData(helpers.AUTH_USER_QUERY_KEY, { id: "user-a" });
  client.setQueryData(userAKey, "user-a-private-data");

  let identityFetches = 0;
  const authObserver = new QueryObserver<{ id: string }>(client, {
    queryKey: helpers.AUTH_USER_QUERY_KEY,
    queryFn: async () => {
      identityFetches += 1;
      if (identityFetches === 1) {
        throw new Error("temporary network failure");
      }
      return { id: "user-a" };
    },
    networkMode: "online",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const privateObserver = new QueryObserver<string>(client, {
    queryKey: userAKey,
    queryFn: async () => "unexpected-old-user-refetch",
    staleTime: Number.POSITIVE_INFINITY,
  });
  const stopAuth = authObserver.subscribe(() => undefined);
  const privateResults: Array<string | undefined> = [];
  const stopPrivate = privateObserver.subscribe((result) =>
    privateResults.push(result.data),
  );
  let currentUserId: string | null = "user-a";
  client.mount();
  onlineManager.setOnline(true);

  try {
    const previousUserId = currentUserId;
    currentUserId = null;
    helpers.prepareUserIdentityRevalidation(client, previousUserId);
    const failedRevalidation = await authObserver.refetch({
      cancelRefetch: true,
    });

    equal(failedRevalidation.status, "error");
    equal(currentUserId, null);
    equal(privateObserver.getCurrentResult().data, undefined);
    equal(privateResults[privateResults.length - 1], undefined);

    // This is the next bounded retry: isolation remains in place until the
    // identity request actually succeeds.
    helpers.prepareUserIdentityRevalidation(client, previousUserId);
    const recoveredRevalidation = await authObserver.refetch({
      cancelRefetch: true,
    });
    equal(recoveredRevalidation.status, "success");
    currentUserId = recoveredRevalidation.data?.id ?? null;

    equal(identityFetches, 2);
    equal(currentUserId, "user-a");
    equal(client.getQueryData(userAKey), undefined);
  } finally {
    onlineManager.setOnline(true);
    client.unmount();
    stopAuth();
    stopPrivate();
  }
});

test("billing query-key factory is user-scoped and one root invalidation refetches active wallet and pricing", async () => {
  const { userBillingQueryKeys } = loadQueryProviderHelpers();
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  const userAAll = userBillingQueryKeys.all("user-a");
  const userAWallet = userBillingQueryKeys.wallet("user-a");
  const userAPricing = userBillingQueryKeys.pricing("user-a");
  const userASnapshot = userBillingQueryKeys.snapshot("user-a");
  const userATransactions = userBillingQueryKeys.walletTransactions("user-a", {
    kind: "all",
    limit: 30,
    pagination: "infinite",
  });
  const userARedemptions = userBillingQueryKeys.redemptions("user-a", {
    limit: 20,
    pagination: "infinite",
  });
  const userBWallet = userBillingQueryKeys.wallet("user-b");

  for (const key of [
    userAWallet,
    userAPricing,
    userASnapshot,
    userATransactions,
    userARedemptions,
  ]) {
    deepEqual(plainValue(key.slice(0, userAAll.length)), plainValue(userAAll));
  }
  deepEqual(plainValue(userAWallet), [
    "user",
    "user-a",
    "billing",
    "wallet",
    "summary",
  ]);
  deepEqual(plainValue(userBWallet), [
    "user",
    "user-b",
    "billing",
    "wallet",
    "summary",
  ]);

  let walletFetches = 0;
  let pricingFetches = 0;
  let otherUserFetches = 0;
  client.setQueryData(userAWallet, "wallet-before");
  client.setQueryData(userAPricing, "pricing-before");
  client.setQueryData(userASnapshot, "snapshot-before");
  client.setQueryData(userBWallet, "other-before");
  const observers = [
    new QueryObserver(client, {
      queryKey: userAWallet,
      queryFn: async () => `wallet-${++walletFetches}`,
      staleTime: Number.POSITIVE_INFINITY,
    }),
    new QueryObserver(client, {
      queryKey: userAPricing,
      queryFn: async () => `pricing-${++pricingFetches}`,
      staleTime: Number.POSITIVE_INFINITY,
    }),
    new QueryObserver(client, {
      queryKey: userBWallet,
      queryFn: async () => `other-${++otherUserFetches}`,
      staleTime: Number.POSITIVE_INFINITY,
    }),
  ];
  const unsubscribe = observers.map((observer) =>
    observer.subscribe(() => undefined),
  );

  try {
    await client.invalidateQueries({ queryKey: userAAll });
  } finally {
    unsubscribe.forEach((stop) => stop());
  }

  equal(walletFetches, 1);
  equal(pricingFetches, 1);
  equal(otherUserFetches, 0);
  equal(client.getQueryData(userAWallet), "wallet-1");
  equal(client.getQueryData(userAPricing), "pricing-1");
  equal(client.getQueryData(userBWallet), "other-before");
  equal(client.getQueryState(userASnapshot)?.isInvalidated, true);
});

test("authenticated queries share one identity scope and stay disabled while unknown", () => {
  for (const componentSource of [
    taskIslandSource,
    taskCenterSource,
    globalTaskTraySource,
  ]) {
    match(componentSource, /useUserQueryScope\(\)/);
    match(componentSource, /user(?:ScopedQueryKey|TaskQueryKeys)/);
  }
  for (const componentSource of [source, mobileTopBarSource]) {
    match(componentSource, /useUserQueryScope\(\)/);
    match(componentSource, /userBillingQueryKeys\.wallet\(userScope\.userId\)/);
    match(componentSource, /userBillingQueryKeys\.pricing\(userScope\.userId\)/);
  }
  match(taskIslandSource, /enabled: userScope\.enabled/);
  match(taskCenterSource, /enabled: userScope\.enabled/);
  match(globalTaskTraySource, /enabled: userScope\.enabled/);
  match(source, /meQuery\.data\?\.id === userScope\.userId/);
  match(mobileTopBarSource, /meQuery\.data\?\.id === userScope\.userId/);
  match(runtimeDefaultsSource, /queryKey: AUTH_USER_QUERY_KEY/);
  match(
    identityRevalidationSource,
    /queryClient\.removeQueries\(\{[\s\S]*?queryKey: AUTH_USER_QUERY_KEY,[\s\S]*?exact: true/,
  );
});

test("runtime identity bootstrap revalidates on focus and visible-tab restoration", () => {
  match(runtimeDefaultsSource, /networkMode: "online"/);
  match(
    identityRevalidationSource,
    /const retainedUserId =[\s\S]*?state\.retainedUserId = retainedUserId;[\s\S]*?enterFailClosed\(retainedUserId\);[\s\S]*?refetch\(\{ cancelRefetch: true \}\)/,
  );
  match(
    identityRevalidationSource,
    /isPublicPath\(window\.location\.pathname\)/,
  );
  match(
    identityRevalidationSource,
    /document\.visibilityState !== "visible"/,
  );
  match(
    identityRevalidationSource,
    /window\.addEventListener\("focus", resume\)/,
  );
  match(
    identityRevalidationSource,
    /document\.addEventListener\("visibilitychange", handleVisibilityChange\)/,
  );
  match(
    identityRevalidationSource,
    /window\.removeEventListener\("focus", resume\)/,
  );
  match(
    identityRevalidationSource,
    /document\.removeEventListener\("visibilitychange", handleVisibilityChange\)/,
  );
  match(
    identityRevalidationSource,
    /if \(isPublicAuthPath\) return;[\s\S]*?window\.addEventListener/,
  );
});

test("identity recovery uses bounded retries, fail-closed isolation, and 401 termination", () => {
  const retryDelay = loadIdentityRetryDelay();
  const errorPolicy = loadIdentityErrorPolicy();
  deepEqual(
    [retryDelay(0), retryDelay(1), retryDelay(2), retryDelay(3), retryDelay(99)],
    [1_000, 3_000, 10_000, 30_000, 30_000],
  );
  equal(
    errorPolicy.isRetryableIdentityError(new errorPolicy.ApiError(0)),
    true,
  );
  equal(
    errorPolicy.isRetryableIdentityError(new errorPolicy.ApiError(503)),
    true,
  );
  equal(
    errorPolicy.isUnauthorizedIdentityError(new errorPolicy.ApiError(401)),
    true,
  );
  equal(
    errorPolicy.isRetryableIdentityError(new errorPolicy.ApiError(401)),
    false,
  );
  equal(
    errorPolicy.isRetryableIdentityError(new errorPolicy.ApiError(403)),
    false,
  );
  match(runtimeDefaultsSource, /useIdentityRevalidation\(\{/);
  match(identityRevalidationSource, /enterFailClosed/);
  match(identityRevalidationSource, /state\.handledError/);
  match(identityRevalidationSource, /state\.generation !== generation/);
  match(identityRevalidationSource, /state\.retryTimer !== null/);
  match(identityRevalidationSource, /runRef\.current\(true\)/);
  match(runtimeDefaultsSource, /refetchOnReconnect: false/);
  match(
    identityRevalidationSource,
    /function removeAuthUserQuery[\s\S]*?\.reset\(\);[\s\S]*?removeQueries\(\{/,
  );
  match(
    identityRevalidationSource,
    /currentUserId && currentUserId !== user\.id[\s\S]*?setCurrentUser\(null\);[\s\S]*?clearPreviousUserQueryCache\(queryClient, currentUserId\);/,
  );
  match(
    identityRevalidationSource,
    /if \(isUnauthorizedIdentityError\(error\)\) \{[\s\S]*?resetRecovery\(true, true\);[\s\S]*?setCurrentUser\(null\);[\s\S]*?return;/,
  );
  match(
    identityRevalidationSource,
    /if \(isRetryableIdentityError\(error\)\) scheduleRetry\(\);/,
  );
});

test("memory and conversation-private queries use scoped keys and identity gates", () => {
  match(memoryPageSource, /userMemoryQueryKeys\.settings\(userScope\.userId\)/);
  match(memoryPageSource, /userMemoryQueryKeys\.scopes\(userScope\.userId\)/);
  match(memoryPageSource, /userMemoryQueryKeys\.items\(userScope\.userId/);
  match(memoryPageSource, /userMemoryQueryKeys\.staging\(userScope\.userId\)/);
  match(memoryPageSource, /userMemoryQueryKeys\.timeline\(userScope\.userId\)/);
  match(memoryPageSource, /enabled: userScope\.enabled/);
  match(accountCenterSource, /userMemoryQueryKeys\.staging\(userScope\.userId\)/);
  match(accountCenterSource, /enabled: identityReady/);
  match(conversationMemorySource, /userConversationQueryKeys\.detail\(/);
  match(conversationMemorySource, /userConversationQueryKeys\.usedMemories\(/);
  match(conversationMemorySource, /userMemoryQueryKeys\.scopes\(/);
  match(conversationMemorySource, /enabled: canQueryConversation/);
  match(
    usagePageSource,
    /isUserScopedQueryKeyForUser\(\s*previousQuery\?\.queryKey/,
  );

  for (const componentSource of [
    memoryPageSource,
    accountCenterSource,
    globalTaskTraySource,
    conversationMemorySource,
    usagePageSource,
  ]) {
    doesNotMatch(
      componentSource,
      /queryKey:\s*\["(?:me",\s*"memory|tasks|conversation)/,
    );
  }
});

test("wallet, pricing, and billing consumers consistently use the shared factory", () => {
  for (const componentSource of [
    source,
    mobileTopBarSource,
    accountCenterSource,
    walletPageSource,
  ]) {
    match(componentSource, /userBillingQueryKeys\.wallet\(userScope\.userId\)/);
  }
  for (const componentSource of [
    source,
    mobileTopBarSource,
    composerCostSource,
  ]) {
    match(componentSource, /userBillingQueryKeys\.pricing\(userScope\.userId\)/);
  }
  for (const componentSource of [walletPageSource, usagePageSource]) {
    match(componentSource, /userBillingQueryKeys\.snapshot\(userScope\.userId\)/);
    match(
      componentSource,
      /userBillingQueryKeys\.walletTransactions\(userScope\.userId/,
    );
  }
  match(
    billingPanelSource,
    /userBillingQueryKeys\.all\(userScope\.userId\)/,
  );

  for (const componentSource of [
    source,
    mobileTopBarSource,
    accountCenterSource,
    composerCostSource,
    usagePageSource,
    billingPanelSource,
    walletPageSource,
  ]) {
    doesNotMatch(
      componentSource,
      /queryKey:\s*\["me",\s*"(?:wallet|pricing|billing|redemptions)"/,
    );
    doesNotMatch(
      componentSource,
      /invalidateQueries\(\{\s*queryKey:\s*\["me",\s*"(?:wallet|pricing|billing|redemptions)"/,
    );
  }
});

test("desktop account popover stays within the viewport", () => {
  match(source, /align="right"/);
  match(popoverSource, /const panelWidth = panel\.offsetWidth/);
  match(popoverSource, /calculateDesktopPopoverPosition\(\{/);
  match(popoverSource, /maxWidth: "calc\(100vw - 24px\)"/);
  match(popoverSource, /useLayoutEffect\(\(\) =>/);
  match(popoverSource, /resizeObserver\?\.observe\(anchor\)/);
  doesNotMatch(popoverSource, /translateX/);
});

test("desktop popover positioning clamps every edge", () => {
  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 930,
        right: 970,
        top: 700,
        bottom: 740,
        width: 40,
      },
      panelWidth: 256,
      panelHeight: 320,
      viewportWidth: 1000,
      viewportHeight: 800,
      align: "right",
    }),
    { left: 714, top: 372 },
  );

  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 0,
        right: 32,
        top: 100,
        bottom: 132,
        width: 32,
      },
      panelWidth: 300,
      panelHeight: 80,
      viewportWidth: 800,
      viewportHeight: 600,
      align: "center",
    }),
    { left: 12, top: 12 },
  );

  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 40,
        right: 80,
        top: -60,
        bottom: -20,
        width: 40,
      },
      panelWidth: 220,
      panelHeight: 120,
      viewportWidth: 800,
      viewportHeight: 600,
      align: "left",
    }),
    { left: 40, top: 12 },
  );
});
