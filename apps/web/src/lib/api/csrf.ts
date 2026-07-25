import { coordinateUnauthorized } from "@/lib/auth/authFailureCoordinator";
import { API_BASE } from "./baseUrl";
import { executeFetch } from "./fetchExecutor";
import { budgetFor } from "./requestBudget";
import { createRequestSignal } from "./requestSignal";

export interface CsrfService {
  token(signal?: AbortSignal): Promise<string | null>;
  refresh(signal?: AbortSignal): Promise<string | null>;
  invalidate(): void;
  apply(headers: Headers, method: string, signal?: AbortSignal): Promise<void>;
}

type RefreshFlight = {
  epoch: number;
  controller: AbortController;
  promise: Promise<string | null>;
};

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  for (const raw of document.cookie ? document.cookie.split("; ") : []) {
    const index = raw.indexOf("=");
    if (index < 0 || raw.slice(0, index) !== name) continue;
    const value = raw.slice(index + 1);
    try {
      return decodeURIComponent(value);
    } catch {
      return value;
    }
  }
  return null;
}

export class DefaultCsrfService implements CsrfService {
  private epoch = 0;
  private flight: RefreshFlight | null = null;

  invalidate(): void {
    this.epoch += 1;
    const flight = this.flight;
    this.flight = null;
    flight?.controller.abort(
      new DOMException("CSRF refresh invalidated", "AbortError"),
    );
  }

  async token(signal?: AbortSignal): Promise<string | null> {
    return readCookie("csrf") ?? (await this.refresh(signal).catch(() => null));
  }

  refresh(signal?: AbortSignal): Promise<string | null> {
    if (typeof document === "undefined") return Promise.resolve(null);
    const epoch = this.epoch;
    if (this.flight?.epoch === epoch) {
      return waitForCaller(this.flight.promise, signal);
    }
    const controller = new AbortController();
    const promise = this.requestToken(epoch, controller.signal).finally(() => {
      if (this.flight?.promise === promise) this.flight = null;
    });
    this.flight = { epoch, controller, promise };
    return waitForCaller(promise, signal);
  }

  async apply(
    headers: Headers,
    method: string,
    signal?: AbortSignal,
  ): Promise<void> {
    if (!WRITE_METHODS.has(method) || headers.has("x-csrf-token")) return;
    const csrf = await this.token(signal);
    if (csrf) headers.set("x-csrf-token", csrf);
  }

  private async requestToken(
    epoch: number,
    callerSignal: AbortSignal,
  ): Promise<string | null> {
    const deadline = createRequestSignal(
      callerSignal,
      budgetFor("query"),
    );
    try {
      const response = await executeFetch(
        `${API_BASE.replace(/\/$/, "")}/auth/csrf`,
        {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          signal: deadline.signal,
        },
        { retryMode: "query" },
      );
      deadline.throwIfAborted();
      if (epoch !== this.epoch || callerSignal.aborted) return null;
      if (response.status === 401) {
        coordinateUnauthorized();
        return null;
      }
      if (!response.ok) return null;
      const data = (await response.json().catch(() => null)) as
        | { csrf_token?: unknown }
        | null;
      if (epoch !== this.epoch || callerSignal.aborted) return null;
      return typeof data?.csrf_token === "string"
        ? data.csrf_token
        : readCookie("csrf");
    } finally {
      deadline.cleanup();
    }
  }
}

export const csrfService = new DefaultCsrfService();

function waitForCaller<T>(
  promise: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason);
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => {
      signal.removeEventListener("abort", onAbort);
    });
  });
}
