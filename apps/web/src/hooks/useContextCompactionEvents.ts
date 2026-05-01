"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getCompactConversationStatus } from "@/lib/apiClient";
import { useSSE } from "@/lib/useSSE";

export type CompactionPhase = "started" | "progress" | "completed";
export type CompactionTrigger = "auto" | "manual";

export interface CompactionEvent {
  conversationId: string;
  phase: CompactionPhase;
  trigger: CompactionTrigger;
  startedAt: string;
  completedAt: string | null;
  elapsedMs: number | null;
  ok: boolean | null;
  fallbackReason: string | null;
  progress?: { currentSegment: number; totalSegments: number };
  stats?: {
    summaryTokens: number;
    sourceMessageCount: number;
    sourceTokenEstimate: number;
    imageCaptionCount: number;
    tokensFreed: number;
    summaryUpToMessageId: string;
  };
}

interface HookState {
  active: boolean;
  latest: CompactionEvent | null;
}

export interface CompactJobTracker {
  jobId: string;
  startedAt: string;
}

const EVENT_NAME = "context.compaction";
const EVENT_KIND = "context.compaction";
// Why: an 8-segment long-conversation compaction can run for several minutes
// (each segment is a /v1/responses call with 45s HTTP timeout × up to 2 retries
// per provider × N providers). 30s used to fire a fake "completed/event_timeout"
// while the worker was still legitimately mid-summary, making the UI tell the
// user "压缩失败" and reset spinners that should have stayed live. 5 minutes
// covers the worst legitimate case while still bounding indefinite hangs from
// dropped SSE streams.
const STARTED_TIMEOUT_MS = 5 * 60 * 1000;
const PHASE_THROTTLE_MS = 200;
const JOB_POLL_DELAY_MS = 2000;
const JOB_POLL_MAX_ATTEMPTS = 600;

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function snakeToCamel(key: string): string {
  return key.replace(/_([a-z])/g, (_, ch: string) => ch.toUpperCase());
}

function camelCasePayload(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(camelCasePayload);
  if (!isRecord(value)) return value;

  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => [snakeToCamel(key), camelCasePayload(entry)]),
  );
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function asPhase(value: unknown): CompactionPhase | null {
  return value === "started" || value === "progress" || value === "completed" ? value : null;
}

function asTrigger(value: unknown): CompactionTrigger {
  return value === "manual" ? "manual" : "auto";
}

function parseCompactionEvent(raw: unknown): CompactionEvent | null {
  if (!isRecord(raw)) return null;

  const source = isRecord(raw.payload) ? { ...raw, ...raw.payload } : raw;
  if (source.kind !== EVENT_KIND) return null;

  const payload = camelCasePayload(source);
  if (!isRecord(payload)) return null;

  const conversationId = asString(payload.conversationId);
  const phase = asPhase(payload.phase);
  const startedAt = asString(payload.startedAt);
  if (!conversationId || !phase || !startedAt) return null;

  const progress = isRecord(payload.progress)
    ? {
        currentSegment: asNumber(payload.progress.currentSegment) ?? 0,
        totalSegments: asNumber(payload.progress.totalSegments) ?? 0,
      }
    : undefined;

  const stats = isRecord(payload.stats)
    ? {
        summaryTokens: asNumber(payload.stats.summaryTokens) ?? 0,
        sourceMessageCount: asNumber(payload.stats.sourceMessageCount) ?? 0,
        sourceTokenEstimate: asNumber(payload.stats.sourceTokenEstimate) ?? 0,
        imageCaptionCount: asNumber(payload.stats.imageCaptionCount) ?? 0,
        tokensFreed: asNumber(payload.stats.tokensFreed) ?? 0,
        summaryUpToMessageId: asString(payload.stats.summaryUpToMessageId) ?? "",
      }
    : undefined;

  return {
    conversationId,
    phase,
    trigger: asTrigger(payload.trigger),
    startedAt,
    completedAt: asString(payload.completedAt),
    elapsedMs: asNumber(payload.elapsedMs),
    ok: asBoolean(payload.ok),
    fallbackReason: asString(payload.fallbackReason),
    progress,
    stats,
  };
}

export function useContextCompactionEvents(
  conversationId: string | null,
  onEvent: (evt: CompactionEvent) => void,
  trackedJob?: CompactJobTracker | null,
): HookState {
  const [state, setState] = useState<HookState>({ active: false, latest: null });
  const onEventRef = useRef(onEvent);
  const conversationIdRef = useRef(conversationId);
  const latestStartedRef = useRef<CompactionEvent | null>(null);
  const startedTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<Partial<Record<CompactionPhase, CompactionEvent>>>({});
  const throttleTimersRef = useRef<Partial<Record<CompactionPhase, ReturnType<typeof setTimeout>>>>(
    {},
  );
  const lastEmitAtRef = useRef<Partial<Record<CompactionPhase, number>>>({});
  const trackedJobRef = useRef<CompactJobTracker | null>(trackedJob ?? null);

  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    trackedJobRef.current = trackedJob ?? null;
  }, [trackedJob]);

  const clearStartedTimeout = useCallback(() => {
    if (startedTimeoutRef.current) {
      clearTimeout(startedTimeoutRef.current);
      startedTimeoutRef.current = null;
    }
  }, []);

  const deliver = useCallback(
    (evt: CompactionEvent) => {
      setState({ active: evt.phase !== "completed", latest: evt });
      try {
        onEventRef.current(evt);
      } catch {
        // UI event consumers should never break the SSE subscription path.
      }
    },
    [],
  );

  const scheduleStartedTimeout = useCallback(
    (started: CompactionEvent) => {
      clearStartedTimeout();
      latestStartedRef.current = started;
      startedTimeoutRef.current = setTimeout(() => {
        const latestStarted = latestStartedRef.current;
        if (!latestStarted || latestStarted.conversationId !== conversationIdRef.current) return;

        clearStartedTimeout();
        latestStartedRef.current = null;
        deliver({
          ...latestStarted,
          phase: "completed",
          completedAt: new Date().toISOString(),
          elapsedMs: null,
          ok: false,
          fallbackReason: "event_timeout",
          progress: undefined,
          stats: undefined,
        });
      }, STARTED_TIMEOUT_MS);
    },
    [clearStartedTimeout, deliver],
  );

  const emit = useCallback(
    (evt: CompactionEvent) => {
      if (evt.conversationId !== conversationIdRef.current) return;

      if (evt.phase === "started") {
        scheduleStartedTimeout(evt);
      } else if (evt.phase === "completed") {
        clearStartedTimeout();
        latestStartedRef.current = null;
      }

      deliver(evt);
    },
    [clearStartedTimeout, deliver, scheduleStartedTimeout],
  );

  const flushPhase = useCallback(
    (phase: CompactionPhase) => {
      const timer = throttleTimersRef.current[phase];
      if (timer) {
        clearTimeout(timer);
        delete throttleTimersRef.current[phase];
      }

      const evt = pendingRef.current[phase];
      delete pendingRef.current[phase];
      if (!evt) return;

      lastEmitAtRef.current[phase] = Date.now();
      emit(evt);
    },
    [emit],
  );

  const queueEvent = useCallback(
    (evt: CompactionEvent) => {
      const phase = evt.phase;
      const now = Date.now();
      const lastEmitAt = lastEmitAtRef.current[phase] ?? 0;
      const elapsed = now - lastEmitAt;

      pendingRef.current[phase] = evt;

      if (elapsed >= PHASE_THROTTLE_MS && !throttleTimersRef.current[phase]) {
        flushPhase(phase);
        return;
      }

      if (!throttleTimersRef.current[phase]) {
        throttleTimersRef.current[phase] = setTimeout(
          () => flushPhase(phase),
          Math.max(PHASE_THROTTLE_MS - elapsed, PHASE_THROTTLE_MS),
        );
      }
    },
    [flushPhase],
  );

  const handlers = useMemo(
    () => ({
      [EVENT_NAME]: (data: unknown) => {
        const evt = parseCompactionEvent(data);
        if (!evt || evt.conversationId !== conversationIdRef.current) return;
        queueEvent(evt);
      },
    }),
    [queueEvent],
  );

  useEffect(() => {
    const resetTimer = setTimeout(() => {
      setState({ active: false, latest: null });
    }, 0);
    latestStartedRef.current = null;
    lastEmitAtRef.current = {};
    pendingRef.current = {};
    clearStartedTimeout();

    Object.values(throttleTimersRef.current).forEach((timer) => {
      if (timer) clearTimeout(timer);
    });
    throttleTimersRef.current = {};

    return () => clearTimeout(resetTimer);
  }, [clearStartedTimeout, conversationId]);

  useEffect(() => {
    return () => {
      clearStartedTimeout();
      Object.values(throttleTimersRef.current).forEach((timer) => {
        if (timer) clearTimeout(timer);
      });
      throttleTimersRef.current = {};
      pendingRef.current = {};
    };
  }, [clearStartedTimeout]);

  useSSE(conversationId ? [`conv:${conversationId}`] : [], handlers);

  useEffect(() => {
    if (!conversationId || !trackedJob?.jobId) return;

    let disposed = false;
    let attempts = 0;
    const startedAt = trackedJob.startedAt;
    // 推到 microtask 调用 deliver，避免 effect 同步路径里直接 setState
    // （react-hooks/set-state-in-effect）。disposed 哨兵防止已卸载后还触发。
    queueMicrotask(() => {
      if (disposed) return;
      deliver({
        conversationId,
        phase: "started",
        trigger: "manual",
        startedAt,
        completedAt: null,
        elapsedMs: null,
        ok: null,
        fallbackReason: null,
      });
    });

    const poll = async () => {
      while (!disposed && attempts < JOB_POLL_MAX_ATTEMPTS) {
        attempts += 1;
        await new Promise((resolve) => setTimeout(resolve, JOB_POLL_DELAY_MS));
        if (disposed) return;

        try {
          const status = await getCompactConversationStatus(conversationId, trackedJob.jobId);
          if (status.status === "pending") continue;

          const completedAt = new Date().toISOString();
          const elapsedMs = Date.parse(completedAt) - Date.parse(startedAt);
          if (status.status === "ok") {
            deliver({
              conversationId,
              phase: "completed",
              trigger: "manual",
              startedAt,
              completedAt,
              elapsedMs,
              ok: status.compacted,
              fallbackReason:
                status.compacted && status.summary.status === "created_local_fallback"
                  ? "local_fallback"
                  : null,
              stats: status.compacted
                ? {
                    summaryTokens: status.summary.tokens,
                    sourceMessageCount: status.summary.source_message_count,
                    sourceTokenEstimate: 0,
                    imageCaptionCount: 0,
                    tokensFreed: 0,
                    summaryUpToMessageId: status.summary.summary_up_to_message_id,
                  }
                : undefined,
            });
          } else {
            deliver({
              conversationId,
              phase: "completed",
              trigger: "manual",
              startedAt,
              completedAt,
              elapsedMs,
              ok: false,
              fallbackReason: status.reason,
            });
          }
          return;
        } catch {
          // SSE remains the primary live channel; polling is only a UI recovery
          // path for missed events or background-tab disconnects.
        }
      }

      if (disposed) return;
      deliver({
        conversationId,
        phase: "completed",
        trigger: "manual",
        startedAt,
        completedAt: new Date().toISOString(),
        elapsedMs: null,
        ok: false,
        fallbackReason: "event_timeout",
      });
    };

    void poll();
    return () => {
      disposed = true;
    };
  }, [conversationId, deliver, trackedJob?.jobId, trackedJob?.startedAt]);

  return state;
}

export const contextCompactionEventInternals = {
  parseCompactionEvent,
};
