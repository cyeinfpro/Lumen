import {
  getRuntimeResilienceSnapshot,
  isHighRiskIdentityWrite,
  normalizeApiPath,
  requestSessionInvalidation,
  type SessionRuntimeStatus,
} from "@/lib/runtimeResilience";
import { ApiError } from "@/lib/api/errors";
import {
  getPrivateIdentitySnapshot,
  isPrivateIdentitySnapshotCurrent,
  type PrivateIdentitySnapshot,
} from "./privateIdentityEpoch";

export interface IdentityWritePolicy {
  assertAllowed(method: string, path: string): void;
}

export const EXPECTED_USER_ID_HEADER = "X-Lumen-Expected-User-Id";

const IDENTITY_BOOTSTRAP_PATHS = new Set([
  "/auth/api-key/verify",
  "/auth/api-suppliers",
  "/auth/csrf",
  "/auth/login",
  "/auth/me",
  "/auth/password/reset-confirm",
  "/auth/password/reset-request",
  "/auth/signup",
  "/auth/signup/byok",
]);

function blockedError(session: SessionRuntimeStatus): ApiError {
  const unauthorized = session === "unauthorized";
  return new ApiError({
    code: unauthorized ? "unauthorized" : "identity_degraded",
    message: unauthorized
      ? "登录状态已失效，请重新登录后再操作"
      : "正在确认登录状态，写操作已暂时切换为只读",
    status: unauthorized ? 401 : 409,
  });
}

function hasConfirmedIdentity(): boolean {
  return getPrivateIdentitySnapshot().userId !== null;
}

function shouldBindConfirmedIdentity(path: string): boolean {
  return !IDENTITY_BOOTSTRAP_PATHS.has(normalizeApiPath(path));
}

export function applyConfirmedIdentityHeader(
  headers: Headers,
  path: string,
): PrivateIdentitySnapshot | null {
  headers.delete(EXPECTED_USER_ID_HEADER);
  if (!shouldBindConfirmedIdentity(path)) return null;
  const identity = getPrivateIdentitySnapshot();
  if (!identity.userId) return null;
  headers.set(EXPECTED_USER_ID_HEADER, identity.userId);
  return identity;
}

export function bindConfirmedIdentityXhr(
  xhr: Pick<XMLHttpRequest, "setRequestHeader">,
  path: string,
): PrivateIdentitySnapshot | null {
  const headers = new Headers();
  const identity = applyConfirmedIdentityHeader(headers, path);
  headers.forEach((value, name) => {
    xhr.setRequestHeader(name, value);
  });
  return identity;
}

export function assertConfirmedIdentityResponse(
  identity: PrivateIdentitySnapshot | null,
): void {
  if (!identity || isPrivateIdentitySnapshotCurrent(identity)) return;
  throw new ApiError({
    code: "identity_changed",
    message: "请求期间登录身份已变化，已忽略旧身份响应",
    status: 409,
  });
}

function errorCode(value: unknown): string | null {
  if (typeof value === "string") {
    try {
      return errorCode(JSON.parse(value));
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object") return null;
  const record = value as {
    code?: unknown;
    detail?: unknown;
    error?: unknown;
  };
  if (typeof record.code === "string") return record.code;
  return errorCode(record.error) ?? errorCode(record.detail);
}

export function coordinateIdentityMismatchResponse(
  status: number,
  payload: unknown,
): boolean {
  if (status !== 409 || errorCode(payload) !== "identity_mismatch") {
    return false;
  }
  requestSessionInvalidation("request_identity_mismatch");
  return true;
}

export const identityWritePolicy: IdentityWritePolicy = {
  assertAllowed(method, path) {
    const session = getRuntimeResilienceSnapshot().session;
    if (
      isHighRiskIdentityWrite(method, path) &&
      (session !== "authenticated" || !hasConfirmedIdentity())
    ) {
      throw blockedError(session);
    }
  },
};
