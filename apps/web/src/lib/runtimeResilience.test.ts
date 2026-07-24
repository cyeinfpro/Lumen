import { equal } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const runtimeResilience = await import(
  new URL("./runtimeResilience.ts", import.meta.url).href
);
const {
  getRuntimeResilienceSnapshot,
  installHighRiskIdentityWriteGuard,
  isHighRiskIdentityWrite,
  setSessionRuntimeStatus,
} = runtimeResilience;

test("high-risk identity writes are narrowly classified", () => {
  equal(isHighRiskIdentityWrite("DELETE", "/api/me"), true);
  equal(isHighRiskIdentityWrite("DELETE", "/api/me/sessions/s-1"), true);
  equal(isHighRiskIdentityWrite("PUT", "/api/me/api-credentials/x"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/me/redemptions"), true);
  equal(isHighRiskIdentityWrite("PATCH", "/api/admin/settings"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/conversations"), false);
  equal(isHighRiskIdentityWrite("PATCH", "/api/me/memory-settings"), false);
  equal(isHighRiskIdentityWrite("GET", "/api/admin/settings"), false);
});

test("identity guard degrades high-risk writes while preserving normal writes", async () => {
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    requests.push(String(input));
    return new Response("ok");
  }) as typeof fetch;
  const uninstall = installHighRiskIdentityWriteGuard();

  try {
    setSessionRuntimeStatus("degraded");
    const blocked = await fetch("http://localhost/api/admin/settings", {
      method: "PATCH",
    });
    equal(blocked.status, 409);
    equal(requests.length, 0);

    const allowed = await fetch("http://localhost/api/conversations", {
      method: "POST",
    });
    equal(allowed.status, 200);
    equal(requests.length, 1);

    setSessionRuntimeStatus("unauthorized");
    const unauthorized = await fetch("http://localhost/api/me", {
      method: "DELETE",
    });
    equal(unauthorized.status, 401);
    equal(getRuntimeResilienceSnapshot().session, "unauthorized");
  } finally {
    uninstall();
    globalThis.fetch = originalFetch;
    setSessionRuntimeStatus("unknown");
  }
});

test("global runtime status exposes recovery and session/realtime copy", () => {
  const source = readFileSync(
    new URL("../components/RuntimeResilienceStatus.tsx", import.meta.url),
    "utf8",
  );
  equal(source.includes("requestRuntimeRecovery()"), true);
  equal(source.includes("会话验证暂不可用"), true);
  equal(source.includes("实时连接已中断"), true);
  equal(source.includes("高风险操作已切换为只读"), true);
});
