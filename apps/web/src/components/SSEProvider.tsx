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

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { disposeChatStoreRuntime, useChatStore } from "@/store/useChatStore";
import { getTask, type BackendCompletion } from "@/lib/apiClient";
import { onOnlineRestore, startConnectivity } from "@/lib/connectivity";
import { logError } from "@/lib/logger";
import { useSSE, type SSEHandlers } from "@/lib/useSSE";
import type { AssistantMessage, Generation, Message } from "@/lib/types";

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

// API accepts 64 effective channels and auto-adds user:{id} when omitted.
// Keep the client below that ceiling; overflow tasks are still repaired by
// pollInflightTasks(), just without live progress events until capacity frees.
const MAX_SSE_CHANNELS_PER_CONNECTION = 62;
const SSE_RECOVERY_POLL_MS = 10_000;

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

function applyCompletionSnapshot(fresh: BackendCompletion): void {
  const now = Date.now();
  useChatStore.setState((s) => {
    let changed = false;
    const nextMessages = s.messages.map((m) => {
      if (m.role !== "assistant") return m;
      if ((m as AssistantMessage).completion_id !== fresh.id) return m;

      const asst = m as AssistantMessage;
      const next = { ...asst };
      const nextStatus = completionStatusToAssistantStatus(fresh.status);
      if (nextStatus && next.status !== nextStatus) {
        next.status = nextStatus;
      }
      if (nextStatus === "streaming" && !next.stream_started_at) {
        next.stream_started_at = now;
      }

      const serverText = typeof fresh.text === "string" ? fresh.text : "";
      const localText = next.text ?? "";
      if (
        serverText &&
        (fresh.status === "succeeded" || serverText.length >= localText.length)
      ) {
        next.text = serverText;
        next.last_delta_at = now;
      } else if (fresh.status === "failed" && !localText && fresh.error_message) {
        next.text = fresh.error_message;
        next.last_delta_at = now;
      }

      if (
        next.status !== asst.status ||
        next.text !== asst.text ||
        next.stream_started_at !== asst.stream_started_at ||
        next.last_delta_at !== asst.last_delta_at
      ) {
        changed = true;
        return next;
      }
      return m;
    });

    return changed ? { messages: nextMessages } : s;
  });
}

async function refreshActiveCompletionText(): Promise<void> {
  const ids = activeCompletionIds(useChatStore.getState().messages);
  await Promise.all(
    ids.map(async (id) => {
      try {
        applyCompletionSnapshot(await getTask("completions", id));
      } catch (err) {
        logError(err, {
          scope: "sse-recovery",
          extra: { task: "completion", id },
        });
      }
    }),
  );
}

export function SSEProvider({ children }: { children: React.ReactNode }) {
  const userId = useChatStore((s) => s.currentUserId);
  const convId = useChatStore((s) => s.currentConvId);
  const generationTaskKey = useChatStore((s) => activeGenerationTaskKey(s.generations));
  const assistantTaskKey = useChatStore((s) => activeAssistantTaskKey(s.messages));
  const qc = useQueryClient();
  const qcRef = useRef(qc);

  useEffect(() => {
    qcRef.current = qc;
  }, [qc]);

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
    for (const id of splitTaskKey(assistantTaskKey)) add(`task:${id}`);
    for (const id of splitTaskKey(generationTaskKey)) add(`task:${id}`);
    return out;
  }, [userId, convId, generationTaskKey, assistantTaskKey]);

  const applyStoreEvent = useCallback((name: string, data: unknown) => {
      useChatStore.getState().applySSEEvent(name, data);
  }, []);

  const handleRenamed = useCallback(
    (data: unknown) => {
      applyStoreEvent("conv.renamed", data);
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
    [applyStoreEvent, qc],
  );

  const handleAccountSettingsUpdated = useCallback(
    (data: unknown) => {
      applyStoreEvent("account_settings_updated", data);
      // 之前用 ["me", "memory"] / ["conversation"] 全前缀失效, 任意一次后端推
      // 都会让 messages list / used-memories / context / scopes / staging /
      // timeline / settings 七八个 query 一起 refetch — 切页面或后台 worker
      // 写一条记忆都触发风暴, 是页面卡顿的主因.
      // settings 事件只代表 user-level memory 开关变了, 精确刷 settings + scopes 即可.
      qc.invalidateQueries({ queryKey: ["me", "memory", "settings"] });
      qc.invalidateQueries({ queryKey: ["me", "memory", "scopes"] });
    },
    [applyStoreEvent, qc],
  );

  const handleConversationMemoryUpdated = useCallback(
    (data: unknown) => {
      applyStoreEvent("conversation.memory.updated", data);
      // 只刷这个 conv 的 used-memories,不动 messages / context / 别的 conv.
      const convId =
        data && typeof data === "object" && "conversation_id" in data
          ? (data as { conversation_id?: unknown }).conversation_id
          : null;
      if (typeof convId === "string" && convId) {
        qc.invalidateQueries({
          queryKey: ["conversation", convId, "used-memories"],
        });
      }
    },
    [applyStoreEvent, qc],
  );

  const handlers = useMemo<SSEHandlers>(() => {
    const h: SSEHandlers = {};
    for (const name of [
      ...GENERATION_EVENTS,
      ...COMPLETION_EVENTS,
      ...CONV_EVENTS,
      ...USER_EVENTS,
    ]) {
      h[name] = (data: unknown) => applyStoreEvent(name, data);
    }
    h["conv.renamed"] = handleRenamed;
    h["account_settings_updated"] = handleAccountSettingsUpdated;
    h["conversation.memory.updated"] = handleConversationMemoryUpdated;
    return h;
  }, [
    applyStoreEvent,
    handleAccountSettingsUpdated,
    handleConversationMemoryUpdated,
    handleRenamed,
  ]);

  const recoveryInFlightRef = useRef(false);
  const runRecovery = useCallback(
    (phase: string, hydrateTasks: boolean, refreshCompletionText: boolean) => {
      if (recoveryInFlightRef.current) return;
      recoveryInFlightRef.current = true;

      const store = useChatStore.getState();
      const jobs: Array<Promise<unknown>> = [];
      if (hydrateTasks) jobs.push(store.hydrateActiveTasks());
      jobs.push(store.pollInflightTasks());
      if (refreshCompletionText) jobs.push(refreshActiveCompletionText());

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
          recoveryInFlightRef.current = false;
        });
    },
    [],
  );

  const handleSSEOpen = useCallback(() => {
    runRecovery("onOpen", true, true);
  }, [runRecovery]);

  useSSE(channels, handlers, { onOpen: handleSSEOpen });

  useEffect(() => {
    const unsubscribeOnlineRestore = onOnlineRestore(() => {
      runRecovery("online-restore", true, false);
    });
    const stopConnectivity = startConnectivity();
    return () => {
      unsubscribeOnlineRestore();
      stopConnectivity();
      disposeChatStoreRuntime();
    };
  }, [runRecovery]);

  // 自愈轮询：周期扫描 in-flight 任务，发现 SSE 漏接的 terminal 状态时主动 refetch。
  // 覆盖 Redis Pub/Sub 不持久化的盲区（刷新瞬间错过的 succeeded/failed event）。
  useEffect(() => {
    const tick = () => {
      runRecovery("poll-inflight", false, false);
    };
    // 挂载后立刻跑一次，覆盖刷新瞬间错过的 terminal event；
    // tick 走 runRecovery，无 in-flight 任务时为 no-op，重复调用安全。
    tick();
    const t = setInterval(tick, SSE_RECOVERY_POLL_MS);
    return () => clearInterval(t);
  }, [runRecovery]);

  return <>{children}</>;
}
