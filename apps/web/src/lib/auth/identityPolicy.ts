import {
  getRuntimeResilienceSnapshot,
  isHighRiskIdentityWrite,
  type SessionRuntimeStatus,
} from "@/lib/runtimeResilience";
import { ApiError } from "@/lib/api/errors";

export interface IdentityWritePolicy {
  assertAllowed(method: string, path: string): void;
}

function blockedError(session: SessionRuntimeStatus): ApiError {
  const unauthorized = session === "unauthorized";
  return new ApiError({
    code: unauthorized ? "unauthorized" : "identity_degraded",
    message: unauthorized
      ? "登录状态已失效，请重新登录后再操作"
      : "正在确认登录状态，高风险操作已暂时切换为只读",
    status: unauthorized ? 401 : 409,
  });
}

export const identityWritePolicy: IdentityWritePolicy = {
  assertAllowed(method, path) {
    const session = getRuntimeResilienceSnapshot().session;
    if (
      session !== "authenticated" &&
      session !== "public" &&
      isHighRiskIdentityWrite(method, path)
    ) {
      throw blockedError(session);
    }
  },
};
