import {
  doesNotMatch,
  equal,
  match,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { loadTsModule } from "../../test-support/load-ts-module.mjs";

const {
  isHighRiskIdentityWrite,
  setSessionRuntimeStatus,
} = loadTsModule(new URL("./runtimeResilience.ts", import.meta.url), {
  react: {
    useSyncExternalStore() {
      return undefined;
    },
  },
}) as {
  isHighRiskIdentityWrite(method: string, path: string): boolean;
  setSessionRuntimeStatus(status: string): void;
};

test("high-risk identity writes are narrowly classified", () => {
  equal(isHighRiskIdentityWrite("DELETE", "/api/me"), true);
  equal(isHighRiskIdentityWrite("DELETE", "/api/me/sessions/s-1"), true);
  equal(isHighRiskIdentityWrite("PUT", "/api/me/api-credentials/x"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/me/redemptions"), true);
  equal(isHighRiskIdentityWrite("PATCH", "/api/admin/settings"), true);
  equal(isHighRiskIdentityWrite("POST", "/api/conversations"), false);
  equal(isHighRiskIdentityWrite("GET", "/api/admin/settings"), false);
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
  doesNotMatch(runtime, /globalThis\.fetch|window\.fetch\s*=|installHighRisk/);
  match(policy, /assertAllowed\(method, path\)/);
  match(commandClient, /this\.policy\.assertAllowed\(method, path\)/);
});

test("runtime recovery login uses the shared safe replace navigation", () => {
  const source = readFileSync(
    new URL("../components/RuntimeResilienceStatus.tsx", import.meta.url),
    "utf8",
  );
  match(source, /replaceWithLogin\(\)/);
  doesNotMatch(source, /location\.assign/);
});
