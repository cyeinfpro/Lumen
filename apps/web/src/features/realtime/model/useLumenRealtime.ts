"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
} from "react";
import {
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { getTask, type BackendCompletion } from "@/lib/apiClient";
import { logError, logWarn } from "@/lib/logger";
import {
  AUTH_USER_QUERY_KEY,
  userBillingQueryKeys,
  userConversationQueryKeys,
  userMemoryQueryKeys,
  userScopedQueryKey,
} from "@/lib/queries/userScope";
import { qk } from "@/lib/queries/queryKeys";
import {
  registerRuntimeRecovery,
  requestSessionInvalidation,
  setRealtimeRuntimeStatus,
} from "@/lib/runtimeResilience";
import { getPrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";
import { notifyAuthSessionChanged } from "@/lib/auth/sessionChangeBus";
import { useSSE, type SSEHandlers } from "./useSSE";
import {
  disposeChatStoreRuntime,
  useChatStore,
} from "@/store/useChatStore";
import type { TaskRecoveryOutcome } from "@/store/chat/types";
import type {
  AssistantMessage,
  Message,
} from "@/lib/types";
import {
  createLumenEffectRegistry,
  type LumenRealtimeEffectContext,
} from "./eventEffects";
import { EventRouter } from "./eventRouter";
import { ProgressEventCoalescer } from "./progressCoalescer";
import type {
  SnapshotAdapter,
  SnapshotExecutionContext,
} from "./replayCoordinator";
import type { SnapshotScope } from "./snapshotScopes";

const EVENT_NAMES = [
  "generation.queued",
  "generation.started",
  "generation.progress",
  "generation.partial_image",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
  "completion.queued",
  "completion.started",
  "completion.progress",
  "completion.delta",
  "completion.thinking_delta",
  "completion.image",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
  "message.intent_resolved",
  "conv.message.appended",
  "conv.renamed",
  "memory.writes",
  "conversation.memory.updated",
  "user.notice",
  "account_settings_updated",
] as const;

const RECENT_SNAPSHOT_WINDOW_MS = 2_000;
const FULL_SNAPSHOT_SCOPES = [
  "identity",
  "conversations",
  "activeTasks",
  "wallet",
  "runtimeDefaults",
] as const satisfies readonly SnapshotScope[];

type InitialSnapshotFlight = {
  controller: AbortController;
  connectionGeneration: number;
  userScope: string;
  userId: string | null;
  identityEpoch: number;
};

type SnapshotIdentity = {
  userScope: string;
  userId: string | null;
  identityEpoch: number;
};

type RecentSnapshot = SnapshotIdentity & {
  syncedAt: number;
};

function shouldSkipRecentSnapshot(
  recent: RecentSnapshot,
  identity: SnapshotIdentity,
  now = Date.now(),
): boolean {
  return (
    recent.userScope === identity.userScope &&
    recent.userId === identity.userId &&
    recent.identityEpoch === identity.identityEpoch &&
    now - recent.syncedAt <= RECENT_SNAPSHOT_WINDOW_MS
  );
}

function staleSnapshotError(): Error {
  const error = new Error("stale snapshot generation");
  error.name = "AbortError";
  return error;
}

function assertSnapshotCurrent(
  signal: AbortSignal,
  context: SnapshotExecutionContext,
  expectedUserScope: string,
  expectedUserId: string | null,
  expectedIdentityEpoch: number,
): void {
  signal.throwIfAborted();
  const identity = getPrivateIdentitySnapshot();
  if (
    context.userScope !== expectedUserScope ||
    useChatStore.getState().currentUserId !== expectedUserId ||
    identity.userId !== expectedUserId ||
    identity.epoch !== expectedIdentityEpoch ||
    !context.isCurrent()
  ) {
    throw staleSnapshotError();
  }
}

function requireCompleteTaskRecovery(
  outcome: TaskRecoveryOutcome,
  label: string,
  signal: AbortSignal,
): void {
  if (outcome.status === "complete") return;
  if (outcome.status === "aborted") {
    signal.throwIfAborted();
    throw staleSnapshotError();
  }
  if (outcome.error instanceof Error) throw outcome.error;
  throw new Error(`${label} failed`);
}

function sortedTaskIds(ids: Iterable<string>): string[] {
  // 修复非法频道名：空串 id 会拼出 `task:` 这种订阅不到任何东西的频道，并挤占
  // MAX_CHANNELS 名额。completion_id 各处已有真值判断，但 generation_ids 数组元素
  // 只判了前缀（`"".startsWith("opt-")` 为 false 会漏过），故在汇聚点统一过滤。
  return [...new Set(ids)].filter((id) => id.length > 0).sort();
}

function completionIds(messages: Message[]): string[] {
  return sortedTaskIds(
    messages.flatMap((message) => {
      if (message.role !== "assistant") return [];
      const assistant = message as AssistantMessage;
      return (assistant.status === "pending" ||
        assistant.status === "streaming") &&
        assistant.completion_id &&
        !assistant.completion_id.startsWith("opt-")
        ? [assistant.completion_id]
        : [];
    }),
  );
}

function completionStatus(
  status: BackendCompletion["status"],
): AssistantMessage["status"] | null {
  if (status === "queued") return "pending";
  if (status === "streaming") return "streaming";
  if (status === "succeeded") return "succeeded";
  if (status === "failed") return "failed";
  if (status === "canceled") return "canceled";
  return null;
}

function applyCompletionSnapshot(fresh: BackendCompletion): void {
  const now = Date.now();
  useChatStore.setState((state) => {
    let changed = false;
    const messages = state.messages.map((message) => {
      const update = updateCompletionMessage(message, fresh, now);
      changed ||= update.changed;
      return update.message;
    });
    return changed ? { messages } : state;
  });
}

function updateCompletionMessage(
  message: Message,
  fresh: BackendCompletion,
  now: number,
): { message: Message; changed: boolean } {
  if (
    message.role !== "assistant" ||
    (message as AssistantMessage).completion_id !== fresh.id
  ) {
    return { message, changed: false };
  }
  const previous = message as AssistantMessage;
  const next = { ...previous };
  applyCompletionStatus(next, fresh, now);
  applyCompletionText(next, fresh, now);
  const changed =
    next.status !== previous.status ||
    next.text !== previous.text ||
    next.last_delta_at !== previous.last_delta_at ||
    next.stream_started_at !== previous.stream_started_at;
  return { message: changed ? next : message, changed };
}

function applyCompletionStatus(
  message: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): void {
  const status = completionStatus(fresh.status);
  if (status && status !== message.status) message.status = status;
  if (status === "streaming" && !message.stream_started_at) {
    message.stream_started_at = now;
  }
}

function applyCompletionText(
  message: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): void {
  const serverText = typeof fresh.text === "string" ? fresh.text : "";
  if (
    serverText &&
    (fresh.status === "succeeded" ||
      serverText.length >= (message.text ?? "").length)
  ) {
    message.text = serverText;
    message.last_delta_at = now;
  }
}

async function refreshCompletions(
  signal: AbortSignal,
  context: SnapshotExecutionContext,
  userScope: string,
  userId: string | null,
  identityEpoch: number,
): Promise<void> {
  const ids = completionIds(useChatStore.getState().messages).slice(0, 16);
  await Promise.all(
    ids.map(async (id) => {
      try {
        const completion = await getTask("completions", id);
        assertSnapshotCurrent(
          signal,
          context,
          userScope,
          userId,
          identityEpoch,
        );
        applyCompletionSnapshot(completion);
      } catch (error) {
        if (error instanceof Error && error.name === "AbortError") throw error;
        logError(error, {
          scope: "sse-snapshot",
          extra: { task: "completion", id },
        });
        throw error;
      }
    }),
  );
}

function channelsFor(userId: string | null): string[] {
  return userId ? [`user:${userId}`] : [];
}

async function invalidateSnapshotQueries(
  queryClient: QueryClient,
  userId: string | null,
  scopes: readonly SnapshotScope[],
): Promise<void> {
  const jobs: Array<Promise<unknown>> = [];
  if (scopes.includes("identity") || scopes.includes("runtimeDefaults")) {
    jobs.push(
      queryClient.invalidateQueries({ queryKey: AUTH_USER_QUERY_KEY }),
    );
  }
  if (scopes.includes("conversations")) {
    jobs.push(
      queryClient.invalidateQueries({
        queryKey: qk.user(userId).conversationsAll(),
      }),
    );
  }
  if (scopes.includes("activeTasks")) {
    jobs.push(
      queryClient.invalidateQueries({
        queryKey: userScopedQueryKey(userId, ["tasks"]),
      }),
    );
  }
  if (scopes.includes("wallet")) {
    jobs.push(
      queryClient.invalidateQueries({
        queryKey: userBillingQueryKeys.all(userId),
      }),
    );
  }
  await Promise.all(jobs);
}

export function useLumenRealtime(): void {
  const userId = useChatStore((state) => state.currentUserId);
  const queryClient = useQueryClient();
  const lastSnapshot = useRef<RecentSnapshot>({
    userScope: "",
    userId: null,
    identityEpoch: -1,
    syncedAt: 0,
  });
  const initialSnapshotFlight = useRef<InitialSnapshotFlight | null>(null);

  const channels = useMemo(() => channelsFor(userId), [userId]);
  const userScope = channels.join(",");
  const identityEpoch = getPrivateIdentitySnapshot().epoch;
  const isRealtimeScopeCurrent = useCallback(
    (scopeIdentity: string) => {
      const identity = getPrivateIdentitySnapshot();
      return (
        scopeIdentity === userScope &&
        useChatStore.getState().currentUserId === userId &&
        identity.userId === userId &&
        identity.epoch === identityEpoch
      );
    },
    [identityEpoch, userId, userScope],
  );

  const effectContext = useMemo<LumenRealtimeEffectContext>(
    () => ({
      applyStoreEvent(name, payload, cursor) {
        if (!isRealtimeScopeCurrent(userScope)) return;
        useChatStore.getState().applySSEEvent(name, payload, cursor);
      },
      invalidateTasks() {
        if (!isRealtimeScopeCurrent(userScope)) return;
        void queryClient.invalidateQueries({
          queryKey: userScopedQueryKey(userId, ["tasks"]),
        });
      },
      invalidateConversations() {
        if (!isRealtimeScopeCurrent(userScope)) return;
        void queryClient.invalidateQueries({
          queryKey: qk.user(userId).conversationsAll(),
        });
      },
      invalidateMemorySettings() {
        if (!isRealtimeScopeCurrent(userScope)) return;
        void queryClient.invalidateQueries({
          queryKey: userMemoryQueryKeys.settings(userId),
        });
        void queryClient.invalidateQueries({
          queryKey: userMemoryQueryKeys.scopes(userId),
        });
      },
      invalidateConversationMemory(nextConversationId) {
        if (!isRealtimeScopeCurrent(userScope)) return;
        void queryClient.invalidateQueries({
          queryKey: userConversationQueryKeys.usedMemories(
            userId,
            nextConversationId,
          ),
        });
      },
    }),
    [isRealtimeScopeCurrent, queryClient, userId, userScope],
  );
  const router = useMemo(
    () => new EventRouter(createLumenEffectRegistry(EVENT_NAMES, effectContext)),
    [effectContext],
  );
  const eventCoalescer = useMemo(
    () =>
      new ProgressEventCoalescer((event) => {
        router.route(event);
      }),
    [router],
  );
  useEffect(() => () => eventCoalescer.dispose(), [eventCoalescer]);
  const handlers = useMemo<SSEHandlers>(
    () =>
      Object.fromEntries(
        EVENT_NAMES.map((name) => [
          name,
          (payload: unknown, cursor: string) => {
            if (!payload || typeof payload !== "object") return;
            eventCoalescer.route({
              kind: "domain",
              type: name,
              version: 1,
              payload: payload as Record<string, unknown>,
              cursor: cursor || undefined,
            });
          },
        ]),
      ),
    [eventCoalescer],
  );

  const recoverSnapshot = useCallback<SnapshotAdapter>(
    async (scopes, _reason, signal, context) => {
      assertSnapshotCurrent(
        signal,
        context,
        userScope,
        userId,
        identityEpoch,
      );
      const store = useChatStore.getState();
      const hydration = await store.hydrateActiveTasks({ signal });
      requireCompleteTaskRecovery(hydration, "active task hydration", signal);
      assertSnapshotCurrent(
        signal,
        context,
        userScope,
        userId,
        identityEpoch,
      );
      const polling = await store.pollInflightTasks({
        maxChecks: 50,
        signal,
      });
      requireCompleteTaskRecovery(polling, "active task polling", signal);
      const results = await Promise.allSettled([
        refreshCompletions(
          signal,
          context,
          userScope,
          userId,
          identityEpoch,
        ),
        invalidateSnapshotQueries(queryClient, userId, scopes),
      ]);
      assertSnapshotCurrent(
        signal,
        context,
        userScope,
        userId,
        identityEpoch,
      );
      const failures = results.filter(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
      if (failures.length > 0) {
        throw new AggregateError(
          failures.map((failure) => failure.reason),
          "realtime snapshot recovery failed",
        );
      }
      const syncedAt = Date.now();
      lastSnapshot.current = {
        userScope,
        userId,
        identityEpoch,
        syncedAt,
      };
      return { syncedAt };
    },
    [identityEpoch, queryClient, userId, userScope],
  );

  const { status, reconnect } = useSSE(channels, handlers, {
    scopeIdentity: userScope,
    isScopeCurrent: isRealtimeScopeCurrent,
    recoverSnapshot,
    onProtocolIssue: (issue) => {
      logWarn("realtime protocol validation failed", {
        scope: "sse-protocol",
        code: issue.reason,
        extra: issue,
      });
    },
    onAuthInvalidated: () => {
      notifyAuthSessionChanged();
      requestSessionInvalidation("realtime_auth_invalidated");
    },
    onOpen: (_event, connectionContext) => {
      initialSnapshotFlight.current?.controller.abort();
      initialSnapshotFlight.current = null;
      const recent = lastSnapshot.current;
      if (
        shouldSkipRecentSnapshot(recent, {
          userScope: connectionContext.userScope,
          userId,
          identityEpoch,
        })
      ) {
        return;
      }
      const controller = new AbortController();
      const flight: InitialSnapshotFlight = {
        controller,
        connectionGeneration: connectionContext.connectionGeneration,
        userScope: connectionContext.userScope,
        userId,
        identityEpoch,
      };
      initialSnapshotFlight.current = flight;
      void recoverSnapshot(
        FULL_SNAPSHOT_SCOPES,
        { kind: "replay_gap", reason: "connection_open" },
        controller.signal,
        {
          ...connectionContext,
          isCurrent: () =>
            !controller.signal.aborted &&
            initialSnapshotFlight.current === flight &&
            flight.connectionGeneration ===
              connectionContext.connectionGeneration &&
            flight.userScope === connectionContext.userScope &&
            flight.userId === userId &&
            flight.identityEpoch === identityEpoch &&
            isRealtimeScopeCurrent(connectionContext.userScope) &&
            connectionContext.isCurrent(),
        },
      )
        .catch((error) => {
          if (error instanceof Error && error.name === "AbortError") return;
          logError(error, { scope: "sse-open-snapshot" });
        })
        .finally(() => {
          if (initialSnapshotFlight.current === flight) {
            initialSnapshotFlight.current = null;
          }
        });
    },
  });

  useEffect(
    () => () => {
      initialSnapshotFlight.current?.controller.abort();
      initialSnapshotFlight.current = null;
    },
    [identityEpoch, userScope],
  );

  useEffect(() => {
    setRealtimeRuntimeStatus(channels.length > 0 ? status : "idle");
  }, [channels.length, status]);

  useEffect(
    () =>
      registerRuntimeRecovery("realtime", () => {
        lastSnapshot.current = {
          userScope,
          userId,
          identityEpoch,
          syncedAt: 0,
        };
        reconnect();
      }),
    [identityEpoch, reconnect, userId, userScope],
  );

  useEffect(
    () => () => {
      disposeChatStoreRuntime();
    },
    [],
  );
}
