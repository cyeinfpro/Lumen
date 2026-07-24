"use client";

// 把 useSSE 封装成"贴在根布局里的一层"。
// 从 useChatStore 读当前 userId / convId + 活动 generation/completion id，
// 合成 channels；收到事件后调 applySSEEvent。
//
// DESIGN §5.7：
//  - 个人通道 user:{uid}（user.notice 等）
//  - 会话通道 conv:{convId}（message.intent_resolved / conv.message.appended / conv.renamed）
//  - 任务通道 task:{id}（Worker 把 generation 和 completion 事件都 publish 到 task:{task_id}；
//    task_id 即 generation.id / completion.id；后端会校验 ref 归属）
//  - 注意：不要用 gen:{id} / comp:{id} / msg:{id}——后端不接收这些前缀。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { disposeChatStoreRuntime, useChatStore } from "@/store/useChatStore";
import {
  userConversationQueryKeys,
  userMemoryQueryKeys,
  userScopedQueryKey,
} from "@/components/QueryProvider";
import { getTask, type BackendCompletion } from "@/lib/apiClient";
import { onOnlineRestore, startConnectivity } from "@/lib/connectivity";
import { logError } from "@/lib/logger";
import { qk } from "@/lib/queries/queryKeys";
import {
  registerRuntimeRecovery,
  setRealtimeRuntimeStatus,
} from "@/lib/runtimeResilience";
import { useSSE, type SSEHandlers } from "@/lib/useSSE";
import type { AssistantMessage, Generation, Message } from "@/lib/types";
import { RuntimeResilienceStatus } from "@/components/RuntimeResilienceStatus";

const GENERATION_EVENTS = [
  "generation.queued",
  "generation.started",
  "generation.progress",
  "generation.partial_image",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
] as const;

const COMPLETION_EVENTS = [
  "completion.queued",
  "completion.started",
  "completion.progress",
  "completion.delta",
  "completion.thinking_delta",
  "completion.image",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
] as const;

const CONV_EVENTS = [
  "message.intent_resolved",
  "conv.message.appended",
  "conv.renamed",
  "memory.writes",
  "conversation.memory.updated",
] as const;

const USER_EVENTS = ["user.notice", "account_settings_updated"] as const;
const TASK_QUERY_EVENTS = new Set<string>([
  "generation.queued",
  "generation.started",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
  "completion.queued",
  "completion.started",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
]);

// API accepts 64 effective channels and auto-adds user:{id} when omitted.
// Keep the client below that ceiling; overflow tasks are still repaired by
// pollInflightTasks(), just without live progress events until capacity frees.
const MAX_SSE_CHANNELS_PER_CONNECTION = 62;
const SSE_RECOVERY_POLL_MS = 10_000;
const SSE_RECOVERY_COALESCE_MS = 600;
const SSE_BROADCAST_CHANNEL = "lumen:sse-events:v1";
const MAX_SEEN_SSE_EVENT_IDS = 2_000;

type SSEBroadcastPayload = {
  source: string;
  sourceUserId: string | null;
  name: string;
  data: unknown;
  eventId?: string;
  sentAt: number;
};

type SeenEventResult = "accepted" | "duplicate" | null;
type RecoveryPollMode = "none" | "overflow" | "limited" | "all";
type RecoveryRequest = {
  phase: string;
  hydrateTasks: boolean;
  refreshCompletionText: boolean;
  pollTasks: RecoveryPollMode;
};

const POLL_MODE_WEIGHT: Record<RecoveryPollMode, number> = {
  none: 0,
  overflow: 1,
  limited: 2,
  all: 3,
};

function mergePollMode(
  current: RecoveryPollMode,
  next: RecoveryPollMode,
): RecoveryPollMode {
  return POLL_MODE_WEIGHT[next] > POLL_MODE_WEIGHT[current] ? next : current;
}

function mergeRecoveryRequest(
  current: RecoveryRequest | null,
  next: RecoveryRequest,
): RecoveryRequest {
  if (!current) return next;
  return {
    phase: `${current.phase}+${next.phase}`,
    hydrateTasks: current.hydrateTasks || next.hydrateTasks,
    refreshCompletionText:
      current.refreshCompletionText || next.refreshCompletionText,
    pollTasks: mergePollMode(current.pollTasks, next.pollTasks),
  };
}

function createBroadcastSourceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function normalizePayloadEventId(raw: unknown): string | null {
  if (typeof raw === "string" && raw) return raw;
  if (typeof raw === "number" && Number.isFinite(raw)) return String(raw);
  return null;
}

function payloadEventId(data: unknown, eventId?: string): string | null {
  if (data && typeof data === "object") {
    const record = data as {
      event_id?: unknown;
      sse_id?: unknown;
      msg_id?: unknown;
    };
    const raw = record.event_id;
    const rawId = normalizePayloadEventId(raw);
    if (rawId) return rawId;
    const sseId = record.sse_id;
    if (typeof sseId === "number" && Number.isFinite(sseId)) {
      return String(sseId);
    }
    if (typeof sseId === "string" && sseId) return sseId;
    const msgId = record.msg_id;
    if (typeof msgId === "number" && Number.isFinite(msgId)) {
      return String(msgId);
    }
    if (typeof msgId === "string" && msgId) return msgId;
  }
  return eventId || null;
}

function isSSEBroadcastPayload(value: unknown): value is SSEBroadcastPayload {
  if (!value || typeof value !== "object") return false;
  const raw = value as Partial<SSEBroadcastPayload>;
  const hasSourceUserId = Object.prototype.hasOwnProperty.call(
    raw,
    "sourceUserId",
  );
  return (
    typeof raw.source === "string" &&
    hasSourceUserId &&
    (raw.sourceUserId === null ||
      (typeof raw.sourceUserId === "string" && raw.sourceUserId.length > 0)) &&
    typeof raw.name === "string" &&
    typeof raw.sentAt === "number"
  );
}

function sortedUniqueTaskKey(ids: Iterable<string>): string {
  return [...new Set(ids)].sort().join(",");
}

function splitTaskKey(key: string): string[] {
  return key ? key.split(",").filter(Boolean) : [];
}

function activeGenerationTaskKey(generations: Record<string, Generation>): string {
  const ids: string[] = [];
  for (const gen of Object.values(generations)) {
    if (
      (gen.status === "queued" || gen.status === "running") &&
      !gen.id.startsWith("opt-")
    ) {
      ids.push(gen.id);
    }
  }
  return sortedUniqueTaskKey(ids);
}

function activeAssistantTaskKey(messages: Message[]): string {
  const ids: string[] = [];
  for (const m of messages) {
    if (m.role !== "assistant") continue;
    const asst = m as AssistantMessage;
    if (asst.status !== "pending" && asst.status !== "streaming") continue;
    if (asst.completion_id && !asst.completion_id.startsWith("opt-")) {
      ids.push(asst.completion_id);
    }
    for (const gid of asst.generation_ids ?? (asst.generation_id ? [asst.generation_id] : [])) {
      if (!gid.startsWith("opt-")) ids.push(gid);
    }
  }
  return sortedUniqueTaskKey(ids);
}

function activeCompletionIds(messages: Message[]): string[] {
  const ids: string[] = [];
  for (const m of messages) {
    if (m.role !== "assistant") continue;
    const asst = m as AssistantMessage;
    if (asst.status !== "pending" && asst.status !== "streaming") continue;
    if (asst.completion_id && !asst.completion_id.startsWith("opt-")) {
      ids.push(asst.completion_id);
    }
  }
  return splitTaskKey(sortedUniqueTaskKey(ids));
}

function completionStatusToAssistantStatus(
  status: BackendCompletion["status"],
): AssistantMessage["status"] | null {
  switch (status) {
    case "queued":
      return "pending";
    case "streaming":
      return "streaming";
    case "succeeded":
      return "succeeded";
    case "failed":
      return "failed";
    case "canceled":
      return "canceled";
    default:
      return null;
  }
}

function applyCompletionStatus(
  next: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): void {
  const nextStatus = completionStatusToAssistantStatus(fresh.status);
  if (nextStatus && next.status !== nextStatus) {
    next.status = nextStatus;
  }
  if (nextStatus === "streaming" && !next.stream_started_at) {
    next.stream_started_at = now;
  }
}

function applyCompletionText(
  next: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): void {
  const serverText = typeof fresh.text === "string" ? fresh.text : "";
  const localText = next.text ?? "";
  const shouldUseServerText =
    serverText &&
    (fresh.status === "succeeded" || serverText.length >= localText.length);
  if (shouldUseServerText) {
    next.text = serverText;
    next.last_delta_at = now;
  } else if (fresh.status === "failed" && !localText && fresh.error_message) {
    next.text = fresh.error_message;
    next.last_delta_at = now;
  }
}

function completionMessageChanged(
  previous: AssistantMessage,
  next: AssistantMessage,
): boolean {
  const changed =
    next.status !== previous.status ||
    next.text !== previous.text ||
    next.stream_started_at !== previous.stream_started_at ||
    next.last_delta_at !== previous.last_delta_at;
  return changed;
}

function updateCompletionMessage(
  message: Message,
  fresh: BackendCompletion,
  now: number,
): { message: Message; changed: boolean } {
  if (message.role !== "assistant") return { message, changed: false };
  const previous = message as AssistantMessage;
  if (previous.completion_id !== fresh.id) {
    return { message, changed: false };
  }
  const next = { ...previous };
  applyCompletionStatus(next, fresh, now);
  applyCompletionText(next, fresh, now);
  const changed = completionMessageChanged(previous, next);
  return { message: changed ? next : message, changed };
}

function mergeCompletionMessages(
  messages: Message[],
  fresh: BackendCompletion,
  now: number,
): { messages: Message[]; changed: boolean } {
  let changed = false;
  const nextMessages = messages.map((message) => {
    const updated = updateCompletionMessage(message, fresh, now);
    changed ||= updated.changed;
    return updated.message;
  });
  return { messages: nextMessages, changed };
}

function applyCompletionSnapshot(fresh: BackendCompletion): void {
  const now = Date.now();
  useChatStore.setState((state) => {
    const merged = mergeCompletionMessages(state.messages, fresh, now);
    return merged.changed ? { messages: merged.messages } : state;
  });
}

async function refreshActiveCompletionText(opts: {
  signal?: AbortSignal;
  completionIds?: string[];
  maxChecks?: number;
} = {}): Promise<void> {
  const sourceIds =
    opts.completionIds ?? activeCompletionIds(useChatStore.getState().messages);
  const maxChecks =
    typeof opts.maxChecks === "number" && Number.isFinite(opts.maxChecks)
      ? Math.max(0, Math.trunc(opts.maxChecks))
      : undefined;
  const ids =
    maxChecks === undefined ? sourceIds : sourceIds.slice(0, maxChecks);
  await Promise.all(
    ids.map(async (id) => {
      try {
        if (opts.signal?.aborted) return;
        applyCompletionSnapshot(
          await getTask("completions", id, { signal: opts.signal }),
        );
      } catch (err) {
        if (opts.signal?.aborted) return;
        logError(err, {
          scope: "sse-recovery",
          extra: { task: "completion", id },
        });
      }
    }),
  );
}

function buildRecoveryJobs(
  request: RecoveryRequest,
  signal: AbortSignal,
  overflowGenerationIds: string[],
  overflowCompletionIds: string[],
): Array<Promise<unknown>> {
  const store = useChatStore.getState();
  const jobs: Array<Promise<unknown>> = [];
  if (request.hydrateTasks) jobs.push(store.hydrateActiveTasks({ signal }));
  if (request.pollTasks === "overflow") {
    if (overflowGenerationIds.length > 0 || overflowCompletionIds.length > 0) {
      jobs.push(
        store.pollInflightTasks({
          signal,
          generationIds: overflowGenerationIds,
          completionIds: overflowCompletionIds,
          maxChecks: 24,
        }),
      );
    }
  } else if (request.pollTasks === "limited") {
    jobs.push(store.pollInflightTasks({ signal, maxChecks: 12 }));
  } else if (request.pollTasks === "all") {
    jobs.push(store.pollInflightTasks({ signal, maxChecks: 50 }));
  }
  if (request.refreshCompletionText) {
    jobs.push(
      refreshActiveCompletionText({
        signal,
        maxChecks: request.pollTasks === "all" ? 16 : 8,
      }),
    );
  }
  return jobs;
}

export function SSEProvider({ children }: { children: React.ReactNode }) {
  const [broadcastSourceId] = useState(createBroadcastSourceId);
  const userId = useChatStore((s) => s.currentUserId);
  const convId = useChatStore((s) => s.currentConvId);
  const generationTaskKey = useChatStore((s) => activeGenerationTaskKey(s.generations));
  const assistantTaskKey = useChatStore((s) => activeAssistantTaskKey(s.messages));
  const generationTaskIds = useMemo(
    () => splitTaskKey(generationTaskKey),
    [generationTaskKey],
  );
  const assistantTaskIds = useMemo(
    () => splitTaskKey(assistantTaskKey),
    [assistantTaskKey],
  );
  const qc = useQueryClient();
  const qcRef = useRef(qc);
  const broadcastRef = useRef<BroadcastChannel | null>(null);
  const seenEventIdsRef = useRef<Set<string>>(new Set());
  const seenEventIdQueueRef = useRef<string[]>([]);
  const overflowGenerationIdsRef = useRef<string[]>([]);
  const overflowCompletionIdsRef = useRef<string[]>([]);
  const recoveryAbortRef = useRef<AbortController | null>(null);
  const initialSSEOpenRef = useRef(false);
  const observedUserIdRef = useRef<string | null>(userId);
  const lastHydratedUserIdRef = useRef<string | null>(null);
  const lastOpenChannelsKeyRef = useRef<string | null>(null);
  const taskInvalidationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const pendingTaskInvalidationUsersRef = useRef<Set<string | null>>(new Set());

  useEffect(() => {
    qcRef.current = qc;
  }, [qc]);

  useEffect(() => {
    if (observedUserIdRef.current === userId) return;
    observedUserIdRef.current = userId;
    lastHydratedUserIdRef.current = null;
  }, [userId]);

  const scheduleTaskInvalidation = useCallback((scopeUserId: string | null) => {
    pendingTaskInvalidationUsersRef.current.add(scopeUserId);
    if (taskInvalidationTimerRef.current) return;
    taskInvalidationTimerRef.current = setTimeout(() => {
      taskInvalidationTimerRef.current = null;
      const scopeUserIds = [...pendingTaskInvalidationUsersRef.current];
      pendingTaskInvalidationUsersRef.current.clear();
      for (const scopeId of scopeUserIds) {
        void qcRef.current.invalidateQueries({
          queryKey: userScopedQueryKey(scopeId, ["tasks"]),
        });
      }
      // Clear any legacy task keys left during the user-scoped cache migration.
      void qcRef.current.invalidateQueries({ queryKey: ["tasks"] });
    }, 500);
  }, []);

  const markEventSeen = useCallback(
    (data: unknown, eventId?: string): SeenEventResult => {
      const id = payloadEventId(data, eventId);
      if (!id) return null;

      const seen = seenEventIdsRef.current;
      if (seen.has(id)) return "duplicate";

      seen.add(id);
      const queue = seenEventIdQueueRef.current;
      queue.push(id);
      while (queue.length > MAX_SEEN_SSE_EVENT_IDS) {
        const old = queue.shift();
        if (old) seen.delete(old);
      }
      return "accepted";
    },
    [],
  );

  const channels = useMemo(() => {
    const out: string[] = [];
    const seen = new Set<string>();
    const add = (channel: string) => {
      if (seen.has(channel) || out.length >= MAX_SSE_CHANNELS_PER_CONNECTION) {
        return;
      }
      seen.add(channel);
      out.push(channel);
    };
    if (userId) add(`user:${userId}`);
    if (convId) add(`conv:${convId}`);
    for (const id of assistantTaskIds) add(`task:${id}`);
    for (const id of generationTaskIds) add(`task:${id}`);
    return out;
  }, [userId, convId, generationTaskIds, assistantTaskIds]);

  const channelsKey = useMemo(() => channels.join(","), [channels]);

  useEffect(() => {
    const liveTaskIds = new Set(
      channels
        .filter((channel) => channel.startsWith("task:"))
        .map((channel) => channel.slice("task:".length)),
    );
    overflowGenerationIdsRef.current = generationTaskIds.filter(
      (id) => !liveTaskIds.has(id),
    );
    overflowCompletionIdsRef.current = assistantTaskIds.filter(
      (id) => !liveTaskIds.has(id),
    );
  }, [channels, generationTaskIds, assistantTaskIds]);

  const applySSEEventWithSideEffects = useCallback((name: string, data: unknown) => {
    useChatStore.getState().applySSEEvent(name, data);

    if (TASK_QUERY_EVENTS.has(name)) {
      scheduleTaskInvalidation(userId);
    }

    if (name === "conv.renamed") {
      qcRef.current.invalidateQueries({
        queryKey: qk.user(userId).conversationsAll(),
      });
      return;
    }

    if (name === "account_settings_updated") {
      // This event only changes user-level memory settings. Keep the refresh
      // inside the current user's private cache.
      qcRef.current.invalidateQueries({
        queryKey: userMemoryQueryKeys.settings(userId),
      });
      qcRef.current.invalidateQueries({
        queryKey: userMemoryQueryKeys.scopes(userId),
      });
      return;
    }

    if (name === "conversation.memory.updated") {
      // Refresh only this conversation's used memories for the current user.
      const nextConvId =
        data && typeof data === "object" && "conversation_id" in data
          ? (data as { conversation_id?: unknown }).conversation_id
          : null;
      if (typeof nextConvId === "string" && nextConvId) {
        qcRef.current.invalidateQueries({
          queryKey: userConversationQueryKeys.usedMemories(
            userId,
            nextConvId,
          ),
        });
      }
    }
  }, [scheduleTaskInvalidation, userId]);

  const deliverSSEEvent = useCallback(
    (
      name: string,
      data: unknown,
      eventId?: string,
      opts?: { broadcast?: boolean; source?: "sse" | "broadcast" },
    ) => {
      const sourceUserId = useChatStore.getState().currentUserId;
      const seenResult = markEventSeen(data, eventId);
      if (seenResult === "duplicate") return;
      if (seenResult === null && opts?.source === "broadcast") {
        return;
      }

      applySSEEventWithSideEffects(name, data);

      if (seenResult === null) return;
      if (opts?.broadcast === false) return;
      try {
        broadcastRef.current?.postMessage({
          source: broadcastSourceId,
          sourceUserId,
          name,
          data,
          eventId,
          sentAt: Date.now(),
        } satisfies SSEBroadcastPayload);
      } catch (err) {
        logError(err, {
          scope: "sse-broadcast",
          extra: { phase: "postMessage", event: name },
        });
      }
    },
    [applySSEEventWithSideEffects, broadcastSourceId, markEventSeen],
  );

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;

    const channel = new BroadcastChannel(SSE_BROADCAST_CHANNEL);
    broadcastRef.current = channel;
    channel.onmessage = (event: MessageEvent) => {
      const message = event.data;
      if (!isSSEBroadcastPayload(message)) return;
      const receiverUserId = useChatStore.getState().currentUserId;
      if (message.sourceUserId !== receiverUserId) return;
      if (message.source === broadcastSourceId) return;
      deliverSSEEvent(message.name, message.data, message.eventId, {
        broadcast: false,
        source: "broadcast",
      });
    };

    return () => {
      channel.close();
      if (broadcastRef.current === channel) {
        broadcastRef.current = null;
      }
    };
  }, [broadcastSourceId, deliverSSEEvent]);

  useEffect(() => {
    const clearSeen = () => {
      seenEventIdsRef.current.clear();
      seenEventIdQueueRef.current = [];
    };
    window.addEventListener("lumen:chat-store-reset", clearSeen);
    return () => window.removeEventListener("lumen:chat-store-reset", clearSeen);
  }, []);

  const handlers = useMemo<SSEHandlers>(() => {
    const h: SSEHandlers = {};
    for (const name of [
      ...GENERATION_EVENTS,
      ...COMPLETION_EVENTS,
      ...CONV_EVENTS,
      ...USER_EVENTS,
    ]) {
      h[name] = (data: unknown, eventId: string) =>
        deliverSSEEvent(name, data, eventId);
    }
    return h;
  }, [deliverSSEEvent]);

  const recoveryInFlightRef = useRef(false);
  const queuedRecoveryRef = useRef<RecoveryRequest | null>(null);
  const recoveryCooldownTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const recoveryDisposedRef = useRef(false);
  const recoveryLifecycleRef = useRef(0);
  const lastRecoveryStartedAtRef = useRef(0);
  const runRecovery = useCallback(
    function runRecoveryImpl(
      phase: string,
      hydrateTasks: boolean,
      refreshCompletionText: boolean,
      pollTasks: RecoveryPollMode,
    ) {
      if (recoveryDisposedRef.current) return;
      const lifecycle = recoveryLifecycleRef.current;
      const request: RecoveryRequest = {
        phase,
        hydrateTasks,
        refreshCompletionText,
        pollTasks,
      };
      const queueRequest = () => {
        queuedRecoveryRef.current = mergeRecoveryRequest(
          queuedRecoveryRef.current,
          request,
        );
      };

      if (recoveryInFlightRef.current || recoveryCooldownTimerRef.current) {
        queueRequest();
        return;
      }

      const elapsed = Date.now() - lastRecoveryStartedAtRef.current;
      if (elapsed < SSE_RECOVERY_COALESCE_MS) {
        queueRequest();
        recoveryCooldownTimerRef.current = setTimeout(() => {
          recoveryCooldownTimerRef.current = null;
          if (
            recoveryDisposedRef.current ||
            lifecycle !== recoveryLifecycleRef.current
          ) {
            return;
          }
          const queued = queuedRecoveryRef.current;
          queuedRecoveryRef.current = null;
          if (queued) {
            runRecoveryImpl(
              queued.phase,
              queued.hydrateTasks,
              queued.refreshCompletionText,
              queued.pollTasks,
            );
          }
        }, SSE_RECOVERY_COALESCE_MS - elapsed);
        return;
      }

      recoveryInFlightRef.current = true;
      lastRecoveryStartedAtRef.current = Date.now();
      recoveryAbortRef.current?.abort(
        new DOMException("superseded SSE recovery", "AbortError"),
      );
      const recoveryAbort = new AbortController();
      recoveryAbortRef.current = recoveryAbort;
      const jobs = buildRecoveryJobs(
        request,
        recoveryAbort.signal,
        overflowGenerationIdsRef.current,
        overflowCompletionIdsRef.current,
      );

      if (jobs.length === 0) {
        recoveryInFlightRef.current = false;
        if (recoveryAbortRef.current === recoveryAbort) {
          recoveryAbortRef.current = null;
        }
        return;
      }

      void Promise.allSettled(jobs)
        .then((results) => {
          for (const result of results) {
            if (result.status === "rejected") {
              logError(result.reason, {
                scope: "sse-recovery",
                extra: { phase },
              });
            }
          }
        })
        .finally(() => {
          if (
            recoveryDisposedRef.current ||
            lifecycle !== recoveryLifecycleRef.current
          ) {
            return;
          }
          recoveryInFlightRef.current = false;
          if (recoveryAbortRef.current === recoveryAbort) {
            recoveryAbortRef.current = null;
          }
          const queued = queuedRecoveryRef.current;
          queuedRecoveryRef.current = null;
          if (queued) {
            runRecoveryImpl(
              queued.phase,
              queued.hydrateTasks,
              queued.refreshCompletionText,
              queued.pollTasks,
            );
          }
        });
    },
    [],
  );

  const handleSSEOpen = useCallback(() => {
    const previousChannelsKey = lastOpenChannelsKeyRef.current;
    lastOpenChannelsKeyRef.current = channelsKey;
    const openedUserId =
      userId && channels.includes(`user:${userId}`) ? userId : null;
    const shouldHydrateNewUserChannel =
      openedUserId !== null &&
      lastHydratedUserIdRef.current !== openedUserId;
    if (shouldHydrateNewUserChannel) {
      lastHydratedUserIdRef.current = openedUserId;
    }

    if (!initialSSEOpenRef.current) {
      initialSSEOpenRef.current = true;
      runRecovery("initial-open", true, true, "overflow");
      return;
    }

    if (previousChannelsKey !== channelsKey) {
      runRecovery(
        "channel-open",
        shouldHydrateNewUserChannel,
        false,
        "overflow",
      );
      return;
    }

    runRecovery("reconnect-open", true, true, "limited");
  }, [channels, channelsKey, runRecovery, userId]);

  const { status: sseStatus, reconnect: reconnectSSE } = useSSE(
    channels,
    handlers,
    { onOpen: handleSSEOpen },
  );

  useEffect(() => {
    setRealtimeRuntimeStatus(channels.length > 0 ? sseStatus : "idle");
  }, [channels.length, sseStatus]);

  useEffect(() => {
    recoveryDisposedRef.current = false;
    recoveryLifecycleRef.current += 1;
    const pendingTaskInvalidationUsers =
      pendingTaskInvalidationUsersRef.current;
    const unsubscribeOnlineRestore = onOnlineRestore(() => {
      reconnectSSE();
      runRecovery("online-restore", true, false, "limited");
    });
    const unsubscribeRuntimeRecovery = registerRuntimeRecovery(
      "realtime",
      () => {
        reconnectSSE();
        runRecovery("manual-reconnect", true, true, "all");
      },
    );
    const stopConnectivity = startConnectivity();
    return () => {
      recoveryDisposedRef.current = true;
      recoveryLifecycleRef.current += 1;
      unsubscribeOnlineRestore();
      unsubscribeRuntimeRecovery();
      stopConnectivity();
      if (taskInvalidationTimerRef.current) {
        clearTimeout(taskInvalidationTimerRef.current);
        taskInvalidationTimerRef.current = null;
      }
      pendingTaskInvalidationUsers.clear();
      if (recoveryCooldownTimerRef.current) {
        clearTimeout(recoveryCooldownTimerRef.current);
        recoveryCooldownTimerRef.current = null;
      }
      recoveryAbortRef.current?.abort(
        new DOMException("SSE provider unmounted", "AbortError"),
      );
      recoveryAbortRef.current = null;
      recoveryInFlightRef.current = false;
      queuedRecoveryRef.current = null;
      disposeChatStoreRuntime();
    };
  }, [reconnectSSE, runRecovery]);

  // 自愈轮询：周期扫描 in-flight 任务，发现 SSE 漏接的 terminal 状态时主动 refetch。
  // 覆盖 Redis Pub/Sub 不持久化的盲区（刷新瞬间错过的 succeeded/failed event）。
  useEffect(() => {
    const tick = () => {
      if (
        typeof document !== "undefined" &&
        document.visibilityState !== "visible"
      ) {
        return;
      }
      runRecovery("poll-inflight", false, false, "limited");
    };
    // 挂载后立刻跑一次，覆盖刷新瞬间错过的 terminal event；
    // tick 走 runRecovery，无 in-flight 任务时为 no-op，重复调用安全。
    tick();
    const t = setInterval(tick, SSE_RECOVERY_POLL_MS);
    return () => clearInterval(t);
  }, [runRecovery]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        reconnectSSE();
        runRecovery("visible-restore", true, false, "limited");
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () =>
      document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [reconnectSSE, runRecovery]);

  return (
    <>
      {children}
      <RuntimeResilienceStatus />
    </>
  );
}
