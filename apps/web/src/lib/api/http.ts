import { activatePrivateClientState } from "@/lib/auth/privateStateCleanup";
import {
  coordinateUnauthorized,
  invalidateSessionClientState,
} from "@/lib/auth/authFailureCoordinator";
import { safeAuthNextPath } from "@/lib/auth/navigation";
import { API_BASE } from "./baseUrl";
import { commandClient } from "./commandClient";
import { csrfService } from "./csrf";
import { ApiError } from "./errors";
import { queryClient } from "./queryClient";
import {
  DEFAULT_API_TIMEOUT_MS,
  deadline,
  type RequestBudget,
} from "./requestBudget";
import { uploadClient } from "./uploadClient";

export {
  API_BASE,
  ApiError,
  DEFAULT_API_TIMEOUT_MS,
  invalidateSessionClientState,
  safeAuthNextPath,
};

export type NoContent = undefined;

export type ApiFetchInit = RequestInit & {
  expectNoContent?: boolean;
  /**
   * @deprecated Prefer a typed client and RequestBudget. Omitted requests use
   * the standard 30 second total deadline.
   */
  timeoutMs?: number;
};

function compatibilityBudget(timeoutMs: number | undefined):
  | RequestBudget
  | undefined {
  if (timeoutMs === undefined) return undefined;
  return deadline(timeoutMs);
}

function isUploadBody(body: BodyInit | null | undefined): body is FormData | Blob {
  return (
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof Blob !== "undefined" && body instanceof Blob)
  );
}

export function handle401(): void {
  coordinateUnauthorized();
}

export function invalidateCsrfTokenRefresh(): void {
  csrfService.invalidate();
}

export function refreshCsrfToken(signal?: AbortSignal): Promise<string | null> {
  return csrfService.refresh(signal);
}

export function ensureCsrfToken(signal?: AbortSignal): Promise<string | null> {
  return csrfService.token(signal);
}

export function resumeSessionClientState(userId: string): Promise<void> {
  return activatePrivateClientState(userId);
}

export async function apiFetch(
  path: string,
  init: ApiFetchInit & { expectNoContent: true },
): Promise<NoContent>;
export async function apiFetch<T = unknown>(
  path: string,
  init?: ApiFetchInit,
): Promise<T>;
export async function apiFetch<T = unknown>(
  path: string,
  init: ApiFetchInit = {},
): Promise<T | NoContent> {
  const {
    expectNoContent = false,
    timeoutMs,
    ...requestInit
  } = init;
  const method = (requestInit.method ?? "GET").toUpperCase();
  const budget = compatibilityBudget(timeoutMs);
  if (method === "GET" || method === "HEAD") {
    return queryClient.get<T>(path, { ...requestInit, budget });
  }
  if (isUploadBody(requestInit.body)) {
    return uploadClient.send<T>(path, requestInit.body, {
      ...requestInit,
      method: method as "POST" | "PUT" | "PATCH",
      budget,
    });
  }
  return commandClient.request<T>(path, {
    ...requestInit,
    method: method as "POST" | "PUT" | "PATCH" | "DELETE",
    budget,
    expectNoContent,
  });
}

export function apiFetchNoContent(
  path: string,
  init: ApiFetchInit = {},
): Promise<NoContent> {
  return apiFetch(path, { ...init, expectNoContent: true });
}
