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
import { logError } from "@/lib/logger";
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
  setRealtimeRuntimeStatus,
} from "@/lib/runtimeResilience";
import { useSSE, type SSEHandlers } from "@/lib/useSSE";
import {
  disposeChatStoreRuntime,
  useChatStore,
} from "@/store/useChatStore";
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
import type { SnapshotAdapter } from "./replayCoordinator";
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

async function refreshCompletions(): Promise<void> {
  const ids = completionIds(useChatStore.getState().messages).slice(0, 16);
  await Promise.all(
    ids.map(async (id) => {
      try {
        applyCompletionSnapshot(await getTask("completions", id));
      } catch (error) {
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
  const lastSnapshotAt = useRef(0);

  const channels = useMemo(() => channelsFor(userId), [userId]);

  const effectContext = useMemo<LumenRealtimeEffectContext>(
    () => ({
      applyStoreEvent(name, payload) {
        useChatStore.getState().applySSEEvent(name, payload);
      },
      invalidateTasks() {
        void queryClient.invalidateQueries({
          queryKey: userScopedQueryKey(userId, ["tasks"]),
        });
      },
      invalidateConversations() {
        void queryClient.invalidateQueries({
          queryKey: qk.user(userId).conversationsAll(),
        });
      },
      invalidateMemorySettings() {
        void queryClient.invalidateQueries({
          queryKey: userMemoryQueryKeys.settings(userId),
        });
        void queryClient.invalidateQueries({
          queryKey: userMemoryQueryKeys.scopes(userId),
        });
      },
      invalidateConversationMemory(nextConversationId) {
        void queryClient.invalidateQueries({
          queryKey: userConversationQueryKeys.usedMemories(
            userId,
            nextConversationId,
          ),
        });
      },
    }),
    [queryClient, userId],
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
    async (scopes, _reason, signal) => {
      signal.throwIfAborted();
      const store = useChatStore.getState();
      const results = await Promise.allSettled([
        store.hydrateActiveTasks(),
        store.pollInflightTasks({ maxChecks: 50 }),
        refreshCompletions(),
        invalidateSnapshotQueries(queryClient, userId, scopes),
      ]);
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
      signal.throwIfAborted();
      lastSnapshotAt.current = Date.now();
      return { syncedAt: lastSnapshotAt.current };
    },
    [queryClient, userId],
  );

  const { status, reconnect } = useSSE(channels, handlers, {
    recoverSnapshot,
    onOpen: () => {
      if (Date.now() - lastSnapshotAt.current > RECENT_SNAPSHOT_WINDOW_MS) {
        const signal = new AbortController().signal;
        void recoverSnapshot([
          "identity",
          "conversations",
          "activeTasks",
          "wallet",
          "runtimeDefaults",
        ], { kind: "replay_gap", reason: "connection_open" }, signal).catch(
          (error) => {
            logError(error, { scope: "sse-open-snapshot" });
          },
        );
      }
    },
  });

  useEffect(() => {
    setRealtimeRuntimeStatus(channels.length > 0 ? status : "idle");
  }, [channels.length, status]);

  useEffect(
    () =>
      registerRuntimeRecovery("realtime", () => {
        reconnect();
      }),
    [reconnect],
  );

  useEffect(
    () => () => {
      disposeChatStoreRuntime();
    },
    [],
  );
}
