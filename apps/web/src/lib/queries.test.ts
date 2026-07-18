import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  notEqual,
  ok,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import * as ts from "typescript";

const queriesSource = readFileSync(
  new URL("./queries.ts", import.meta.url),
  "utf8",
);
const queryKeysSource = readFileSync(
  new URL("./queries/queryKeys.ts", import.meta.url),
  "utf8",
);
const privateQueryScopeSource = readFileSync(
  new URL("./queries/privateQueryScope.ts", import.meta.url),
  "utf8",
);
const systemPromptsSource = readFileSync(
  new URL("./queries/systemPrompts.ts", import.meta.url),
  "utf8",
);
const systemPromptManagerSource = readFileSync(
  new URL("../components/ui/SystemPromptManager.tsx", import.meta.url),
  "utf8",
);

function hookSource(source: string, name: string): string {
  const start = source.indexOf(`export function ${name}`);
  ok(start >= 0, `missing ${name}`);
  const next = source.indexOf("\nexport function ", start + 1);
  return source.slice(start, next < 0 ? source.length : next);
}

function loadStandaloneFunction<T>(
  name: string,
  source = queriesSource,
  filename = "queries.ts",
): T {
  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const declaration = sourceFile.statements.find(
    (statement): statement is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(statement) && statement.name?.text === name,
  );
  ok(declaration, `missing ${name}`);
  const output = ts.transpileModule(
    `${declaration.getText(sourceFile)}\nmodule.exports[${JSON.stringify(name)}] = ${name};`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = { exports: {} as Record<string, unknown> };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports[name] as T;
}

test("proxy mutations keep internal invalidation and forward onSuccess", () => {
  for (const name of [
    "useTestProxyMutation",
    "useTestAllProxiesMutation",
    "useUpdateAdminProxiesMutation",
  ]) {
    const hook = hookSource(queriesSource, name);
    const optionsSpread = hook.indexOf("    ...options,");
    const internalOnSuccess = hook.indexOf("    onSuccess:");

    ok(optionsSpread >= 0, `${name} must spread caller options`);
    ok(
      optionsSpread < internalOnSuccess,
      `${name} must define internal onSuccess after caller options`,
    );
    match(
      hook,
      /options\?\.onSuccess\?\.\(data, vars, onMutateResult, ctx\)/,
    );
  }
});

test("proxy update invalidates both proxy and provider query keys", () => {
  const hook = hookSource(queriesSource, "useUpdateAdminProxiesMutation");
  match(hook, /qc\.invalidateQueries\(\{ queryKey: qk\.adminProxies\(\) \}\)/);
  match(hook, /qc\.invalidateQueries\(\{ queryKey: qk\.providers\(\) \}\)/);
});

type SystemPromptQueryKeys = {
  systemPrompts: () => readonly ["system_prompts"];
  allowedEmails: () => readonly ["admin", "allowed_emails"];
  publicInvite: (token: string) => readonly ["invite", string];
  user: (userId: string | null | undefined) => {
    myShares: () => readonly unknown[];
    mySessions: () => readonly unknown[];
    conversations: (params?: Record<string, unknown>) => readonly unknown[];
    conversationsInfinite: (params: {
      limit: number;
      q?: string;
    }) => readonly unknown[];
    conversationDetail: (conversationId: string) => readonly unknown[];
    conversationContext: (conversationId: string) => readonly unknown[];
    workflows: (params?: Record<string, unknown>) => readonly unknown[];
    workflow: (workflowId: string) => readonly unknown[];
    storyboards: (params?: Record<string, unknown>) => readonly unknown[];
    storyboard: (storyboardId: string) => readonly unknown[];
    apparelModelLibrary: (
      params?: Record<string, unknown>,
    ) => readonly unknown[];
    apparelModelLibraryJobs: () => readonly unknown[];
    apparelModelLibraryJobsList: (
      params?: Record<string, unknown>,
    ) => readonly unknown[];
    apparelModelLibraryJobsInfinite: (params: {
      limit: number;
    }) => readonly unknown[];
    posterStyles: (params?: Record<string, unknown>) => readonly unknown[];
    posterStyle: (itemId: string) => readonly unknown[];
    posterStyleJobs: (params?: Record<string, unknown>) => readonly unknown[];
  };
};

function loadQueryKeys(): { qk: SystemPromptQueryKeys } {
  const output = ts.transpileModule(queryKeysSource, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const moduleRecord = { exports: {} as { qk: SystemPromptQueryKeys } };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports;
}

test("system prompt hooks scope the preserved qk leaf by user identity", () => {
  const { qk } = loadQueryKeys();
  const userAKey = ["user", "user-a", ...qk.systemPrompts()];
  const userBKey = ["user", "user-b", ...qk.systemPrompts()];

  deepEqual(userAKey, ["user", "user-a", "system_prompts"]);
  deepEqual(userBKey, ["user", "user-b", "system_prompts"]);
  ok(JSON.stringify(userAKey) !== JSON.stringify(userBKey));
  match(
    systemPromptsSource,
    /function systemPromptsQueryKey\(userId: string \| null \| undefined\)[\s\S]*?userScopedQueryKey\(userId, qk\.systemPrompts\(\)\)/,
  );
  doesNotMatch(
    queryKeysSource,
    /from ["'](?:\.\.\/)+components\/QueryProvider/,
  );
});

test("authenticated query keys isolate real user data without scoping admin or public data", () => {
  const { qk } = loadQueryKeys();
  const userA = qk.user("user-a");
  const userB = qk.user("user-b");
  const keyPairs: Array<[readonly unknown[], readonly unknown[]]> = [
    [userA.myShares(), userB.myShares()],
    [userA.mySessions(), userB.mySessions()],
    [
      userA.conversations({ limit: 20 }),
      userB.conversations({ limit: 20 }),
    ],
    [
      userA.conversationsInfinite({ limit: 30, q: "poster" }),
      userB.conversationsInfinite({ limit: 30, q: "poster" }),
    ],
    [
      userA.conversationDetail("conversation-1"),
      userB.conversationDetail("conversation-1"),
    ],
    [
      userA.conversationContext("conversation-1"),
      userB.conversationContext("conversation-1"),
    ],
    [userA.workflows({ limit: 20 }), userB.workflows({ limit: 20 })],
    [userA.workflow("workflow-1"), userB.workflow("workflow-1")],
    [userA.storyboards({ limit: 20 }), userB.storyboards({ limit: 20 })],
    [userA.storyboard("storyboard-1"), userB.storyboard("storyboard-1")],
    [
      userA.apparelModelLibrary({ source: "all" }),
      userB.apparelModelLibrary({ source: "all" }),
    ],
    [userA.apparelModelLibraryJobs(), userB.apparelModelLibraryJobs()],
    [
      userA.apparelModelLibraryJobsList({ limit: 30 }),
      userB.apparelModelLibraryJobsList({ limit: 30 }),
    ],
    [
      userA.apparelModelLibraryJobsInfinite({ limit: 30 }),
      userB.apparelModelLibraryJobsInfinite({ limit: 30 }),
    ],
    [
      userA.posterStyles({ category: "all" }),
      userB.posterStyles({ category: "all" }),
    ],
    [userA.posterStyle("style-1"), userB.posterStyle("style-1")],
    [
      userA.posterStyleJobs({ limit: 50 }),
      userB.posterStyleJobs({ limit: 50 }),
    ],
  ];

  for (const [userAKey, userBKey] of keyPairs) {
    const normalizedUserAKey = JSON.parse(JSON.stringify(userAKey));
    const normalizedUserBKey = JSON.parse(JSON.stringify(userBKey));
    deepEqual(normalizedUserAKey.slice(0, 2), ["user", "user-a"]);
    deepEqual(normalizedUserBKey.slice(0, 2), ["user", "user-b"]);
    notEqual(
      JSON.stringify(normalizedUserAKey),
      JSON.stringify(normalizedUserBKey),
    );
  }

  deepEqual(JSON.parse(JSON.stringify(qk.allowedEmails())), [
    "admin",
    "allowed_emails",
  ]);
  deepEqual(JSON.parse(JSON.stringify(qk.publicInvite("invite-token"))), [
    "invite",
    "invite-token",
  ]);
  doesNotMatch(
    queryKeysSource,
    /^\s{2}(?:myShares|mySessions|conversations|workflows|storyboards|apparelModelLibrary|posterStyles):/m,
  );
});

test("anonymous private query gates keep QueryObserver idle without requesting", async () => {
  const { qk } = loadQueryKeys();
  const privateQueryEnabled = loadStandaloneFunction<
    (
      identityEnabled: boolean,
      requestedEnabled: boolean | undefined,
      ...requirements: boolean[]
    ) => boolean
  >(
    "privateQueryEnabled",
    privateQueryScopeSource,
    "privateQueryScope.ts",
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  let requestCount = 0;
  const observer = new QueryObserver(client, {
    queryKey: qk.user(null).mySessions(),
    queryFn: async () => {
      requestCount += 1;
      return { items: [] };
    },
    enabled: privateQueryEnabled(false, true),
  });
  const unsubscribe = observer.subscribe(() => undefined);

  await new Promise<void>((resolve) => setImmediate(resolve));

  equal(requestCount, 0);
  equal(observer.getCurrentResult().fetchStatus, "idle");
  unsubscribe();
  client.clear();
});

test("all private query hooks use scoped keys and identity gates", () => {
  const optionHooks = [
    "useMySessionsQuery",
    "useListConversationsQuery",
    "useConversationContextQuery",
    "useWorkflowsQuery",
    "useWorkflowQuery",
    "useStoryboardsQuery",
    "useStoryboardQuery",
    "useApparelModelLibraryQuery",
    "useApparelModelLibraryJobsQuery",
    "usePosterStylesQuery",
    "usePosterStyleQuery",
    "usePosterStyleJobsQuery",
    "usePosterWorkflowQuery",
  ];
  for (const name of optionHooks) {
    const hook = hookSource(queriesSource, name);
    match(hook, /userScope/);
    match(hook, /userKeys/);
    match(
      hook,
      /enabled:\s*privateQueryEnabled\(\s*userScope\.enabled,/,
    );
  }

  for (const name of [
    "useListConversationsInfiniteQuery",
    "useApparelModelLibraryJobsInfiniteQuery",
  ]) {
    const hook = hookSource(queriesSource, name);
    match(hook, /userKeys/);
    match(hook, /enabled: userScope\.enabled/);
  }
});

test("poster style previous data is retained only for the same user", () => {
  const hook = hookSource(queriesSource, "usePosterStylesQuery");

  match(
    hook,
    /placeholderData: \(previous, previousQuery\) =>[\s\S]*?isUserScopedQueryKeyForUser\([\s\S]*?previousQuery\?\.queryKey \?\? \[\],[\s\S]*?userScope\.userId,[\s\S]*?\? previous[\s\S]*?: undefined/,
  );
  doesNotMatch(hook, /placeholderData: \(prev\) => prev/);
});

test("direct conversation detail caller is scoped and disabled without identity", () => {
  match(
    systemPromptManagerSource,
    /function useCurrentConversationQuery\(currentConvId: string \| null\)/,
  );
  match(systemPromptManagerSource, /const userScope = useUserQueryScope\(\)/);
  match(
    systemPromptManagerSource,
    /queryKey: qk\.user\(userScope\.userId\)\.conversationDetail\(conversationId\)/,
  );
  match(
    systemPromptManagerSource,
    /enabled: userScope\.enabled && Boolean\(currentConvId\)/,
  );
  doesNotMatch(
    systemPromptManagerSource,
    /queryKey:\s*\["conversations",\s*"detail"/,
  );
});

test("queries facade preserves the system prompt hook exports", () => {
  match(
    queriesSource,
    /export \{[\s\S]*?useCreateSystemPromptMutation,[\s\S]*?useDeleteSystemPromptMutation,[\s\S]*?usePatchSystemPromptMutation,[\s\S]*?useSetDefaultSystemPromptMutation,[\s\S]*?useSystemPromptsQuery,[\s\S]*?\} from "\.\/queries\/systemPrompts";/,
  );
});

test("system prompt query composes identity gating with caller enabled", () => {
  const hook = hookSource(systemPromptsSource, "useSystemPromptsQuery");
  const optionsSpread = hook.indexOf("    ...options,");
  const identityEnabled = hook.indexOf(
    "enabled: userScope.enabled && (options?.enabled ?? true)",
  );

  match(hook, /const userScope = useUserQueryScope\(\)/);
  match(hook, /queryKey: systemPromptsQueryKey\(userScope\.userId\)/);
  ok(optionsSpread >= 0, "system prompt query must preserve caller options");
  ok(
    identityEnabled > optionsSpread,
    "identity gating must be applied after caller options",
  );
});

test("all system prompt mutations are identity-gated and invalidate the scoped key", () => {
  for (const name of [
    "useCreateSystemPromptMutation",
    "usePatchSystemPromptMutation",
    "useDeleteSystemPromptMutation",
    "useSetDefaultSystemPromptMutation",
  ]) {
    const hook = hookSource(systemPromptsSource, name);
    match(hook, /const userScope = useUserQueryScope\(\)/);
    match(
      hook,
      /mutationFn: guardSystemPromptMutation\([\s\S]*?userScope\.enabled/,
    );
    match(
      hook,
      /qc\.invalidateQueries\(\{\s*queryKey: systemPromptsQueryKey\(userScope\.userId\)/,
    );
  }
});

test("system prompt hooks have no remaining unscoped system prompt cache use", () => {
  doesNotMatch(systemPromptsSource, /queryKey:\s*qk\.systemPrompts\(\)/);
  doesNotMatch(
    systemPromptsSource,
    /invalidateQueries\(\{\s*queryKey:\s*qk\.systemPrompts\(\)/,
  );
});

type PosterStyleJobStatusFixture =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "partial";

type PosterStyleJobsFixture = {
  items: Array<{ job_id: string; status: PosterStyleJobStatusFixture }>;
};

function posterStyleJobsFixture(
  status: PosterStyleJobStatusFixture,
  jobId = "job-1",
): PosterStyleJobsFixture {
  return { items: [{ job_id: jobId, status }] };
}

test("poster style terminal transitions invalidate list and detail queries once", () => {
  const hasTerminalTransition = loadStandaloneFunction<
    (
      previous: PosterStyleJobsFixture | undefined,
      current: PosterStyleJobsFixture | undefined,
    ) => boolean
  >("posterStyleJobsHaveTerminalTransition");

  for (const activeStatus of ["queued", "running"] as const) {
    for (const terminalStatus of ["succeeded", "failed", "partial"] as const) {
      equal(
        hasTerminalTransition(
          posterStyleJobsFixture(activeStatus),
          posterStyleJobsFixture(terminalStatus),
        ),
        true,
      );
    }
  }
  equal(
    hasTerminalTransition(
      posterStyleJobsFixture("queued"),
      posterStyleJobsFixture("running"),
    ),
    false,
  );
  equal(
    hasTerminalTransition(
      posterStyleJobsFixture("succeeded"),
      posterStyleJobsFixture("succeeded"),
    ),
    false,
  );
  equal(
    hasTerminalTransition(undefined, posterStyleJobsFixture("succeeded")),
    false,
  );
  equal(
    hasTerminalTransition(
      posterStyleJobsFixture("running", "job-1"),
      posterStyleJobsFixture("succeeded", "job-2"),
    ),
    false,
  );

  const hook = hookSource(queriesSource, "usePosterStyleJobsQuery");
  const snapshotWrite = hook.indexOf("previousJobsRef.current = currentJobs");
  const transitionCheck = hook.indexOf(
    "posterStyleJobsHaveTerminalTransition(previousJobs, currentJobs)",
  );
  ok(snapshotWrite >= 0, "jobs hook must remember the latest snapshot");
  ok(
    transitionCheck > snapshotWrite,
    "snapshot must advance before invalidation to suppress duplicate terminal polls",
  );
  match(
    hook,
    /qc\.invalidateQueries\(\{ queryKey: scopedKeys\.posterStyleLists\(\) \}\)/,
  );
  match(
    hook,
    /qc\.invalidateQueries\(\{ queryKey: scopedKeys\.posterStyleDetails\(\) \}\)/,
  );
  match(hook, /const scopedKeys = qk\.user\(userScope\.userId\)/);
  match(
    hook,
    /previousJobsUserIdRef\.current = userScope\.userId;[\s\S]*?previousJobsRef\.current = undefined;/,
  );
  doesNotMatch(hook, /posterStyleKeys/);
});
