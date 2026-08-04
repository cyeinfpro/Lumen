import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
  throws,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { loadTsModule } from "../../test-support/load-ts-module.mjs";

const {
  isHighRiskIdentityWrite,
  getRuntimeResilienceSnapshot,
  registerSessionInvalidation,
  requestSessionInvalidation,
  setSessionRuntimeStatus,
} = loadTsModule(new URL("./runtimeResilience.ts", import.meta.url), {
  react: {
    useSyncExternalStore() {
      return undefined;
    },
  },
}) as {
  isHighRiskIdentityWrite(method: string, path: string): boolean;
  getRuntimeResilienceSnapshot(): { session: string };
  registerSessionInvalidation(
    handler: (reason: string) => void,
  ): () => void;
  requestSessionInvalidation(reason: string): void;
  setSessionRuntimeStatus(status: string): void;
};

test("all authenticated writes are fail-closed except public auth bootstrap", () => {
  equal(isHighRiskIdentityWrite("DELETE", "/api/me"), true);
  equal(isHighRiskIdentityWrite("DELETE", "/api/me/sessions/s-1"), true);
  equal(isHighRiskIdentityWrite("PUT", "/api/me/api-credentials/x"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/me/redemptions"), true);
  equal(isHighRiskIdentityWrite("PATCH", "/api/admin/settings"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/conversations"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/auth/login"), false);
  equal(
    isHighRiskIdentityWrite(
      "POST",
      "https://example.test/api/auth/password/reset-request?next=1",
    ),
    false,
  );
  equal(isHighRiskIdentityWrite("GET", "/api/admin/settings"), false);
  setSessionRuntimeStatus("unknown");
});

test("identity policy binds requests to the confirmed user and rejects stale responses", () => {
  let session = "authenticated";
  let identity: { userId: string | null; epoch: number } = {
    userId: "user-a",
    epoch: 7,
  };
  const invalidations: string[] = [];
  class TestApiError extends Error {
    code: string;
    status: number;

    constructor(opts: { code: string; message: string; status: number }) {
      super(opts.message);
      this.code = opts.code;
      this.status = opts.status;
    }
  }
  const policyModule = loadTsModule(
    new URL("./auth/identityPolicy.ts", import.meta.url),
    {
      "@/lib/runtimeResilience": {
        getRuntimeResilienceSnapshot: () => ({ session }),
        isHighRiskIdentityWrite,
        normalizeApiPath: (path: string) =>
          path.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api/, "") || "/",
        requestSessionInvalidation: (reason: string) => {
          invalidations.push(reason);
        },
      },
      "@/lib/api/errors": { ApiError: TestApiError },
      "./privateIdentityEpoch": {
        getPrivateIdentitySnapshot: () => identity,
        isPrivateIdentitySnapshotCurrent: (candidate: {
          userId: string | null;
          epoch: number;
        }) =>
          candidate.userId === identity.userId &&
          candidate.epoch === identity.epoch,
      },
    },
  ) as {
    EXPECTED_USER_ID_HEADER: string;
    applyConfirmedIdentityHeader(
      headers: Headers,
      path: string,
    ): { userId: string | null; epoch: number } | null;
    assertConfirmedIdentityResponse(identity: {
      userId: string | null;
      epoch: number;
    } | null): void;
    coordinateIdentityMismatchResponse(status: number, payload: unknown): boolean;
    identityWritePolicy: {
      assertAllowed(method: string, path: string): void;
    };
  };

  const headers = new Headers({
    "X-Lumen-Expected-User-Id": "spoofed",
  });
  const captured = policyModule.applyConfirmedIdentityHeader(
    headers,
    "/conversations",
  );
  equal(
    headers.get(policyModule.EXPECTED_USER_ID_HEADER),
    "user-a",
  );
  deepEqual(captured, identity);

  const bootstrapHeaders = new Headers({
    "X-Lumen-Expected-User-Id": "stale",
  });
  equal(
    policyModule.applyConfirmedIdentityHeader(
      bootstrapHeaders,
      "/auth/me",
    ),
    null,
  );
  equal(
    bootstrapHeaders.has(policyModule.EXPECTED_USER_ID_HEADER),
    false,
  );

  policyModule.identityWritePolicy.assertAllowed("POST", "/conversations");
  identity = { userId: null, epoch: 8 };
  throws(
    () =>
      policyModule.identityWritePolicy.assertAllowed(
        "POST",
        "/conversations",
      ),
    (error: unknown) =>
      error instanceof TestApiError && error.code === "identity_degraded",
  );
  session = "public";
  policyModule.identityWritePolicy.assertAllowed("POST", "/auth/login");

  identity = { userId: "user-b", epoch: 9 };
  throws(
    () => policyModule.assertConfirmedIdentityResponse(captured),
    (error: unknown) =>
      error instanceof TestApiError && error.code === "identity_changed",
  );
  equal(
    policyModule.coordinateIdentityMismatchResponse(
      409,
      JSON.stringify({
        detail: { error: { code: "identity_mismatch" } },
      }),
    ),
    true,
  );
  deepEqual(invalidations, ["request_identity_mismatch"]);
});

test("auth invalidation marks the session unauthorized and invokes cleanup callback", () => {
  const reasons: string[] = [];
  const unregister = registerSessionInvalidation((reason) => {
    reasons.push(reason);
  });

  requestSessionInvalidation("realtime_auth_invalidated");

  equal(getRuntimeResilienceSnapshot().session, "unauthorized");
  deepEqual(reasons, ["realtime_auth_invalidated"]);
  unregister();
  setSessionRuntimeStatus("unknown");
});

test("identity policy is explicit and global fetch is never patched", () => {
  const runtime = readFileSync(
    new URL("./runtimeResilience.ts", import.meta.url),
    "utf8",
  );
  const policy = readFileSync(
    new URL("./auth/identityPolicy.ts", import.meta.url),
    "utf8",
  );
  const commandClient = readFileSync(
    new URL("./api/commandClient.ts", import.meta.url),
    "utf8",
  );
  const transport = readFileSync(
    new URL("./api/transport.ts", import.meta.url),
    "utf8",
  );
  const apparelUpload = readFileSync(
    new URL(
      "../components/ui/projects/ApparelWorkflowNewPage.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  const posterUpload = readFileSync(
    new URL(
      "../components/ui/projects/PosterWorkflowNewPage.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  doesNotMatch(runtime, /globalThis\.fetch|window\.fetch\s*=|installHighRisk/);
  match(policy, /assertAllowed\(method, path\)/);
  match(commandClient, /this\.policy\.assertAllowed\(method, path\)/);
  match(transport, /applyConfirmedIdentityHeader\(headers, path\)/);
  match(apparelUpload, /bindConfirmedIdentityXhr\(/);
  match(posterUpload, /bindConfirmedIdentityXhr\(/);
});

test("runtime recovery login uses the shared safe replace navigation", () => {
  const source = readFileSync(
    new URL("../components/RuntimeResilienceStatus.tsx", import.meta.url),
    "utf8",
  );
  match(source, /replaceWithLogin\(\)/);
  match(source, /top-\[calc\(var\(--mobile-topbar-h\)/);
  match(source, /md:bottom-4 md:left-auto md:right-4 md:top-auto/);
  match(source, /aria-label=\{unauthorized \? "登录" : "立即恢复实时连接"\}/);
  match(source, /className="pointer-events-none fixed/);
  match(
    source,
    /if \(!unauthorized && !sessionDegraded && !realtimeDegraded\) return null;/,
  );
  doesNotMatch(source, /"正在连接"|"正在确认会话"|animate-spin/);
  doesNotMatch(source, /location\.assign/);
});
