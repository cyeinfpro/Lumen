"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { qk } from "@/lib/queries";
import {
  adminUpdateStreamUrl,
  type AdminUpdateStatusOut,
  type UpdateStepRecord,
} from "@/lib/apiClient";
import type {
  AdminStreamStatus,
  AdminUpdateStreamHandle,
} from "./AdminUpdatePanel.helpers";

const LOG_BUFFER_MAX = 500;
const SSE_RETRY_DELAYS_MS = [1000, 2000, 5000, 15000, 15000];
const SSE_MAX_RETRIES = SSE_RETRY_DELAYS_MS.length;

export function useDisarmUpdateStream(
  armed: boolean,
  setArmed: (armed: boolean) => void,
  pending: boolean,
  running: boolean,
) {
  useEffect(() => {
    if (!armed || pending || running) return;
    const timeout = setTimeout(() => setArmed(false), 0);
    return () => clearTimeout(timeout);
  }, [armed, pending, running, setArmed]);
}

export function useAdminUpdateStream(
  enabled: boolean,
): AdminUpdateStreamHandle {
  const queryClient = useQueryClient();
  const [logBuffer, setLogBuffer] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] =
    useState<AdminStreamStatus>("idle");

  const queryClientRef = useRef(queryClient);
  useEffect(() => {
    queryClientRef.current = queryClient;
  });

  const clearLogs = useCallback(() => {
    setLogBuffer([]);
  }, []);

  useEffect(() => {
    if (!enabled) {
      const timeout = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(timeout);
    }
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      const timeout = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(timeout);
    }

    let eventSource: EventSource | null = null;
    let retryAttempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const clearRetry = () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const close = () => {
      clearRetry();
      if (eventSource) {
        try {
          eventSource.close();
        } catch {
          // Ignore close failures from already-closed browser streams.
        }
        eventSource = null;
      }
    };

    const mergeStep = (step: UpdateStepRecord) => {
      queryClientRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (previous) => {
          if (!previous) {
            return {
              running: step.status === "running",
              log_tail: "",
              phases: [step],
            };
          }
          const phases = previous.phases ? [...previous.phases] : [];
          const index = phases.findIndex((phase) => phase.phase === step.phase);
          if (index >= 0) {
            phases[index] = { ...phases[index], ...step };
          } else {
            phases.push(step);
          }
          return { ...previous, phases };
        },
      );
    };

    const mergeInfo = (payload: {
      phase: string;
      key: string;
      value: string;
    }) => {
      queryClientRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (previous) => {
          if (!previous) return previous;
          const phases = previous.phases ? [...previous.phases] : [];
          const index = phases.findIndex(
            (phase) => phase.phase === payload.phase,
          );
          if (index < 0) return previous;
          const current = phases[index];
          phases[index] = {
            ...current,
            info: {
              ...(current.info ?? {}),
              [payload.key]: payload.value,
            },
          };
          return { ...previous, phases };
        },
      );
    };

    const scheduleRetry = () => {
      if (disposed) return;
      clearRetry();
      if (retryAttempt >= SSE_MAX_RETRIES) {
        setStreamStatus("broken");
        return;
      }
      const delay = SSE_RETRY_DELAYS_MS[retryAttempt] ?? 15000;
      retryAttempt += 1;
      retryTimer = setTimeout(() => {
        if (!disposed) open();
      }, delay);
    };

    const parseData = <T,>(raw: string): T | null => {
      try {
        return JSON.parse(raw) as T;
      } catch {
        return null;
      }
    };

    const open = () => {
      if (disposed) return;
      close();
      setStreamStatus("connecting");
      try {
        eventSource = new EventSource(adminUpdateStreamUrl(), {
          withCredentials: true,
        });
      } catch {
        setStreamStatus("error");
        scheduleRetry();
        return;
      }

      eventSource.onopen = () => {
        retryAttempt = 0;
        setStreamStatus("open");
      };

      eventSource.addEventListener("state", (event: MessageEvent) => {
        const snapshot = parseData<AdminUpdateStatusOut>(event.data);
        if (!snapshot) return;
        queryClientRef.current.setQueryData(
          qk.adminUpdateStatus(),
          snapshot,
        );
        if (snapshot.releases) {
          queryClientRef.current.setQueryData(
            qk.adminReleases(),
            snapshot.releases,
          );
        }
      });

      eventSource.addEventListener("step", (event: MessageEvent) => {
        const step = parseData<UpdateStepRecord>(event.data);
        if (!step || !step.phase) return;
        mergeStep(step);
      });

      eventSource.addEventListener("info", (event: MessageEvent) => {
        const info = parseData<{
          phase: string;
          key: string;
          value: string;
        }>(event.data);
        if (!info || !info.phase || !info.key) return;
        mergeInfo(info);
      });

      eventSource.addEventListener("log", (event: MessageEvent) => {
        const payload = parseData<{ line?: string; lines?: string[] }>(
          event.data,
        );
        if (!payload) return;
        const lines = Array.isArray(payload.lines)
          ? payload.lines.filter(
              (line): line is string => typeof line === "string",
            )
          : typeof payload.line === "string"
            ? [payload.line]
            : [];
        if (lines.length === 0) return;
        setLogBuffer((previous) => {
          const next =
            previous.length >= LOG_BUFFER_MAX
              ? previous.slice(-(LOG_BUFFER_MAX - 1))
              : previous.slice();
          return [...next, ...lines].slice(-LOG_BUFFER_MAX);
        });
      });

      eventSource.addEventListener("done", (event: MessageEvent) => {
        const payload = parseData<{ final_status?: AdminUpdateStatusOut }>(
          event.data,
        );
        if (payload?.final_status) {
          queryClientRef.current.setQueryData(
            qk.adminUpdateStatus(),
            payload.final_status,
          );
        }
        queryClientRef.current.invalidateQueries({
          queryKey: qk.adminUpdateStatus(),
        });
        queryClientRef.current.invalidateQueries({
          queryKey: qk.adminReleases(),
        });
        close();
        setStreamStatus("idle");
      });

      eventSource.onerror = () => {
        setStreamStatus("error");
        close();
        scheduleRetry();
      };
    };

    open();

    return () => {
      disposed = true;
      close();
    };
  }, [enabled]);

  return { logBuffer, streamStatus, clearLogs };
}
