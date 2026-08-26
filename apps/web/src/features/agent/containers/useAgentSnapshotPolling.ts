"use client";

import { useEffect } from "react";
import type { AgentRealtimeStatus } from "@/store/agent/useAgentStore";


export function useAgentSnapshotPolling(input: {
  sessionId: string | null;
  intervalMs: number;
  refresh: (signal: AbortSignal) => Promise<void>;
  setStatus: (status: AgentRealtimeStatus) => void;
}): void {
  useEffect(() => {
    if (!input.sessionId) return;
    let timer: number | null = null;
    let controller: AbortController | null = null;
    let running = false;
    let disposed = false;
    let trailing = false;
    const clearTimer = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
    };
    const schedule = () => {
      clearTimer();
      if (disposed || document.visibilityState !== "visible") return;
      timer = window.setTimeout(run, input.intervalMs);
    };
    async function run() {
      if (disposed || document.visibilityState !== "visible") return;
      if (running) {
        trailing = true;
        return;
      }
      running = true;
      controller = new AbortController();
      try {
        await input.refresh(controller.signal);
      } catch (error) {
        if (!(error instanceof Error && error.name === "AbortError")) {
          input.setStatus("error");
        }
      } finally {
        running = false;
        controller = null;
        if (disposed) return;
        if (trailing) {
          trailing = false;
          void run();
        } else schedule();
      }
    }
    const onFocus = () => void run();
    const onVisibility = () => {
      if (document.visibilityState === "visible") void run();
      else {
        clearTimer();
        controller?.abort();
      }
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    void run();
    return () => {
      disposed = true;
      clearTimer();
      controller?.abort();
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [input]);
}
