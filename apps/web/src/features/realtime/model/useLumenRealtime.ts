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
import { logWarn } from "@/lib/logger";
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
import { INITIAL_SNAPSHOT_RECOVERY_REASON } from "./contracts";
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
const POLLING_INTERVAL_MS = 8_000;
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
  const pollNowRef = useRef<() => void>(() => {});

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
    async (scopes, reason, signal, context) => {
      assertSnapshotCurrent(
        signal,
        context,
        userScope,
        userId,
        identityEpoch,
      );
      const initialSnapshot =
        reason.kind === "initial_snapshot" ||
        (reason.kind === "recovery_required" &&
          reason.reason === INITIAL_SNAPSHOT_RECOVERY_REASON);
      if (
        initialSnapshot &&
        shouldSkipRecentSnapshot(lastSnapshot.current, {
          userScope: context.userScope,
          userId,
          identityEpoch,
        })
      ) {
        return { syncedAt: lastSnapshot.current.syncedAt };
      }
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
      await invalidateSnapshotQueries(queryClient, userId, scopes);
      assertSnapshotCurrent(
        signal,
        context,
        userScope,
        userId,
        identityEpoch,
      );
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
  });

  useEffect(() => {
    setRealtimeRuntimeStatus(channels.length > 0 ? status : "idle");
  }, [channels.length, status]);

  useEffect(() => {
    if (!userId || !userScope) {
      pollNowRef.current = () => {};
      return;
    }

    let disposed = false;
    let running = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const clearTimer = () => {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
    };
    const scheduleNext = () => {
      clearTimer();
      if (disposed || document.visibilityState !== "visible") return;
      timer = window.setTimeout(requestPoll, POLLING_INTERVAL_MS);
    };
    const runPoll = async () => {
      if (
        disposed ||
        running ||
        document.visibilityState !== "visible"
      ) {
        return;
      }
      running = true;
      controller = new AbortController();
      const context: SnapshotExecutionContext = {
        connectionGeneration: identityEpoch,
        userScope,
        isCurrent: () => isRealtimeScopeCurrent(userScope),
      };
      try {
        await recoverSnapshot(
          ["activeTasks"],
          { kind: "initial_snapshot" },
          controller.signal,
          context,
        );
      } catch (error) {
        if (!(error instanceof Error && error.name === "AbortError")) {
          logWarn("polling task recovery failed", {
            scope: "realtime-poll",
            extra: { err: String(error) },
          });
        }
      } finally {
        controller = null;
        running = false;
        scheduleNext();
      }
    };
    function requestPoll() {
      if (
        disposed ||
        running ||
        document.visibilityState !== "visible"
      ) {
        return;
      }
      clearTimer();
      void runPoll();
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        requestPoll();
        return;
      }
      clearTimer();
      controller?.abort();
    };

    pollNowRef.current = requestPoll;
    window.addEventListener("focus", requestPoll);
    document.addEventListener("visibilitychange", onVisibilityChange);
    requestPoll();

    return () => {
      disposed = true;
      pollNowRef.current = () => {};
      clearTimer();
      controller?.abort();
      window.removeEventListener("focus", requestPoll);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [
    identityEpoch,
    isRealtimeScopeCurrent,
    recoverSnapshot,
    userId,
    userScope,
  ]);

  useEffect(
    () =>
      registerRuntimeRecovery("realtime", () => {
        lastSnapshot.current.syncedAt = 0;
        pollNowRef.current();
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
