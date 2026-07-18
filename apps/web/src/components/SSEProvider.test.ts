import { doesNotMatch, equal, ok, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { QueryClient } from "@tanstack/react-query";

const source = readFileSync(
  new URL("./SSEProvider.tsx", import.meta.url),
  "utf8",
);

test("idless SSE events are not rebroadcast across tabs", () => {
  const untrackedGuard = source.indexOf(
    "if (seenResult === null) return;",
  );
  const broadcastPost = source.indexOf("postMessage({");

  ok(untrackedGuard >= 0, "missing untracked-event broadcast guard");
  ok(broadcastPost >= 0, "missing BroadcastChannel postMessage call");
  ok(
    untrackedGuard < broadcastPost,
    "untracked events must return before BroadcastChannel postMessage",
  );
});

test("idless BroadcastChannel messages are dropped before store delivery", () => {
  match(
    source,
    /seenResult === null && opts\?\.source === "broadcast"[\s\S]*?return;/,
  );
});

test("BroadcastChannel payloads carry the source authenticated user identity", () => {
  match(source, /sourceUserId: string \| null;/);
  match(
    source,
    /const sourceUserId = useChatStore\.getState\(\)\.currentUserId;/,
  );
  match(
    source,
    /postMessage\(\{[\s\S]*?source: broadcastSourceId,[\s\S]*?sourceUserId,/,
  );
});

test("BroadcastChannel identity guard rejects missing or mismatched users before delivery", () => {
  match(
    source,
    /Object\.prototype\.hasOwnProperty\.call\(\s*raw,\s*"sourceUserId",\s*\)/,
  );
  match(
    source,
    /raw\.sourceUserId === null \|\|[\s\S]*?typeof raw\.sourceUserId === "string"/,
  );

  const handlerStart = source.indexOf("channel.onmessage =");
  const handlerEnd = source.indexOf("return () =>", handlerStart);
  ok(handlerStart >= 0, "missing BroadcastChannel message handler");
  ok(handlerEnd > handlerStart, "missing BroadcastChannel handler boundary");
  const handlerSource = source.slice(handlerStart, handlerEnd);

  match(
    handlerSource,
    /const receiverUserId = useChatStore\.getState\(\)\.currentUserId;/,
  );
  const guardExpression = handlerSource.match(
    /if \((message\.sourceUserId !== receiverUserId)\) return;/,
  )?.[1];
  ok(guardExpression, "missing source/receiver identity guard");

  const shouldDrop = new Function(
    "message",
    "receiverUserId",
    `return ${guardExpression};`,
  ) as (
    message: { sourceUserId?: string | null },
    receiverUserId: string | null,
  ) => boolean;

  equal(shouldDrop({ sourceUserId: "user-a" }, "user-b"), true);
  equal(shouldDrop({ sourceUserId: "user-a" }, "user-a"), false);
  equal(shouldDrop({ sourceUserId: null }, null), false);
  equal(shouldDrop({}, null), true);

  const identityGuard = handlerSource.indexOf(
    "if (message.sourceUserId !== receiverUserId) return;",
  );
  const storeDelivery = handlerSource.indexOf("deliverSSEEvent(");
  ok(identityGuard >= 0, "missing identity guard");
  ok(storeDelivery >= 0, "missing BroadcastChannel store delivery");
  ok(
    identityGuard < storeDelivery,
    "identity guard must run before dedupe and store side effects",
  );
});

test("SSE event identity accepts payload ids, sse ids, and EventSource ids", () => {
  match(source, /event_id\?: unknown;[\s\S]*?sse_id\?: unknown;[\s\S]*?msg_id\?: unknown/);
  match(source, /typeof raw === "number" && Number\.isFinite\(raw\)[\s\S]*?return String\(raw\)/);
  match(source, /typeof sseId === "number" && Number\.isFinite\(sseId\)[\s\S]*?return String\(sseId\)/);
  match(source, /typeof msgId === "number" && Number\.isFinite\(msgId\)[\s\S]*?return String\(msgId\)/);
  match(source, /return eventId \|\| null;/);
});

test("BroadcastChannel cleanup only clears its own channel instance", () => {
  match(source, /const channel = new BroadcastChannel\(SSE_BROADCAST_CHANNEL\)/);
  match(source, /if \(broadcastRef\.current === channel\) \{[\s\S]*?broadcastRef\.current = null;/);
});

test("chat store reset clears the local seen event id cache", () => {
  match(source, /lumen:chat-store-reset/);
  match(source, /seenEventIdsRef\.current\.clear\(\)/);
  match(source, /seenEventIdQueueRef\.current = \[\]/);
});

test("opening a new authenticated channel rehydrates after an anonymous open", () => {
  match(
    source,
    /const lastHydratedUserIdRef = useRef<string \| null>\(null\)/,
  );
  match(
    source,
    /const observedUserIdRef = useRef<string \| null>\(userId\)/,
  );
  match(
    source,
    /userId && channels\.includes\(`user:\$\{userId\}`\) \? userId : null/,
  );
  match(
    source,
    /lastHydratedUserIdRef\.current !== openedUserId/,
  );
  match(
    source,
    /runRecovery\(\s*"channel-open",\s*shouldHydrateNewUserChannel,\s*false,\s*"overflow"/,
  );
  match(
    source,
    /if \(observedUserIdRef\.current === userId\) return;[\s\S]*?observedUserIdRef\.current = userId;[\s\S]*?lastHydratedUserIdRef\.current = null;/,
  );
});

test("account settings SSE invalidation targets only current-user memory settings and scopes", async () => {
  const branchStart = source.indexOf(
    'if (name === "account_settings_updated") {',
  );
  const branchEnd = source.indexOf(
    'if (name === "conversation.memory.updated") {',
    branchStart,
  );
  ok(branchStart >= 0, "missing account settings SSE side effect");
  ok(branchEnd > branchStart, "missing account settings branch boundary");
  const branchSource = source.slice(branchStart, branchEnd);

  match(
    branchSource,
    /queryKey: userMemoryQueryKeys\.settings\(userId\)/,
  );
  match(
    branchSource,
    /queryKey: userMemoryQueryKeys\.scopes\(userId\)/,
  );
  doesNotMatch(
    branchSource,
    /queryKey:\s*\["me",\s*"memory"/,
    "private memory invalidation must not use an unscoped legacy key",
  );

  const client = new QueryClient();
  const currentSettingsKey = [
    "user",
    "user-1",
    "me",
    "memory",
    "settings",
  ] as const;
  const currentScopesKey = [
    "user",
    "user-1",
    "me",
    "memory",
    "scopes",
  ] as const;
  const currentItemsKey = [
    "user",
    "user-1",
    "me",
    "memory",
    "items",
    "all",
  ] as const;
  const otherSettingsKey = [
    "user",
    "user-2",
    "me",
    "memory",
    "settings",
  ] as const;
  const legacySettingsKey = ["me", "memory", "settings"] as const;

  for (const queryKey of [
    currentSettingsKey,
    currentScopesKey,
    currentItemsKey,
    otherSettingsKey,
    legacySettingsKey,
  ]) {
    client.setQueryData(queryKey, { loaded: true });
  }

  await Promise.all([
    client.invalidateQueries({ queryKey: currentSettingsKey }),
    client.invalidateQueries({ queryKey: currentScopesKey }),
  ]);

  equal(client.getQueryState(currentSettingsKey)?.isInvalidated, true);
  equal(client.getQueryState(currentScopesKey)?.isInvalidated, true);
  equal(client.getQueryState(currentItemsKey)?.isInvalidated, false);
  equal(client.getQueryState(otherSettingsKey)?.isInvalidated, false);
  equal(client.getQueryState(legacySettingsKey)?.isInvalidated, false);
});

test("conversation memory SSE invalidation targets only current-user used memories", async () => {
  const branchStart = source.indexOf(
    'if (name === "conversation.memory.updated") {',
  );
  const branchEnd = source.indexOf(
    "const deliverSSEEvent = useCallback(",
    branchStart,
  );
  ok(branchStart >= 0, "missing conversation memory SSE side effect");
  ok(branchEnd > branchStart, "missing conversation memory branch boundary");
  const branchSource = source.slice(branchStart, branchEnd);

  match(
    branchSource,
    /queryKey: userConversationQueryKeys\.usedMemories\(\s*userId,\s*nextConvId,\s*\)/,
  );
  doesNotMatch(
    branchSource,
    /queryKey:\s*\["conversation"/,
    "private conversation invalidation must not use an unscoped legacy key",
  );

  const client = new QueryClient();
  const targetUsedMemoriesKey = [
    "user",
    "user-1",
    "conversation",
    "conv-1",
    "used-memories",
  ] as const;
  const targetConversationKey = [
    "user",
    "user-1",
    "conversation",
    "conv-1",
  ] as const;
  const siblingUsedMemoriesKey = [
    "user",
    "user-1",
    "conversation",
    "conv-2",
    "used-memories",
  ] as const;
  const otherUserUsedMemoriesKey = [
    "user",
    "user-2",
    "conversation",
    "conv-1",
    "used-memories",
  ] as const;
  const legacyUsedMemoriesKey = [
    "conversation",
    "conv-1",
    "used-memories",
  ] as const;

  for (const queryKey of [
    targetUsedMemoriesKey,
    targetConversationKey,
    siblingUsedMemoriesKey,
    otherUserUsedMemoriesKey,
    legacyUsedMemoriesKey,
  ]) {
    client.setQueryData(queryKey, { loaded: true });
  }

  await client.invalidateQueries({ queryKey: targetUsedMemoriesKey });

  equal(client.getQueryState(targetUsedMemoriesKey)?.isInvalidated, true);
  equal(client.getQueryState(targetConversationKey)?.isInvalidated, false);
  equal(client.getQueryState(siblingUsedMemoriesKey)?.isInvalidated, false);
  equal(client.getQueryState(otherUserUsedMemoriesKey)?.isInvalidated, false);
  equal(client.getQueryState(legacyUsedMemoriesKey)?.isInvalidated, false);
});

test("task SSE invalidation targets the current user-scoped task cache", async () => {
  const client = new QueryClient();
  const taskKey = ["user", "user-1", "tasks", "recent"] as const;
  client.setQueryData(taskKey, { items: [] });

  await client.invalidateQueries({
    queryKey: ["user", "user-1", "tasks"],
  });

  equal(client.getQueryState(taskKey)?.isInvalidated, true);
  match(source, /scheduleTaskInvalidation\(userId\)/);
  match(
    source,
    /queryKey: userScopedQueryKey\(scopeId, \["tasks"\]\)/,
  );
});

test("conversation rename invalidates only the current user's conversation cache", () => {
  match(
    source,
    /if \(name === "conv\.renamed"\) \{[\s\S]*?queryKey: qk\.user\(userId\)\.conversationsAll\(\)/,
  );
  doesNotMatch(
    source,
    /if \(name === "conv\.renamed"\) \{[\s\S]*?queryKey: \["conversations"\]/,
  );
});
