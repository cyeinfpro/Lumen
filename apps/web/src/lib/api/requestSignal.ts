import { timeoutError } from "./errors";
import type { RequestBudget } from "./requestBudget";

type AbortSource = "caller" | "deadline" | null;

export type RequestSignal = {
  signal?: AbortSignal;
  throwIfAborted(error?: unknown): void;
  cleanup(): void;
};

export function abortReason(signal?: AbortSignal | null): unknown {
  return (
    signal?.reason ??
    new DOMException("The operation was aborted", "AbortError")
  );
}

export function createRequestSignal(
  callerSignal: AbortSignal | null | undefined,
  budget: RequestBudget,
): RequestSignal {
  if (budget.kind === "none") {
    return {
      signal: callerSignal ?? undefined,
      throwIfAborted() {
        if (callerSignal?.aborted) throw abortReason(callerSignal);
      },
      cleanup() {},
    };
  }

  const controller = new AbortController();
  let source: AbortSource = null;
  const onCallerAbort = () => {
    if (source) return;
    source = "caller";
    controller.abort(abortReason(callerSignal));
  };
  if (callerSignal?.aborted) onCallerAbort();
  else callerSignal?.addEventListener("abort", onCallerAbort, { once: true });

  const timer = setTimeout(() => {
    if (source) return;
    source = "deadline";
    controller.abort(new DOMException("Request timed out", "TimeoutError"));
  }, budget.totalMs);

  return {
    signal: controller.signal,
    throwIfAborted(error?: unknown) {
      if (!controller.signal.aborted) return;
      if (source === "caller") throw abortReason(callerSignal);
      throw timeoutError(error ?? controller.signal.reason);
    },
    cleanup() {
      clearTimeout(timer);
      callerSignal?.removeEventListener("abort", onCallerAbort);
    },
  };
}
