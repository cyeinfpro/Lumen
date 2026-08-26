import { describe, expect, it } from "vitest";

import {
  MIN_RUNTIME_SHARED_SECRET_BYTES,
  validateRuntimeConfig,
  type RuntimeConfig,
} from "../src/config.js";


function validConfig(): RuntimeConfig {
  return {
    host: "127.0.0.1",
    port: 8090,
    sharedSecret: "x".repeat(MIN_RUNTIME_SHARED_SECRET_BYTES),
    authClockSkewSeconds: 30,
    nonceTtlSeconds: 120,
    nonceCacheSize: 10_000,
    maxRequestBytes: 16 * 1024 * 1024,
    maxLineBytes: 64 * 1024,
    maxConcurrentRuns: 8,
    maxConcurrentBodyReads: 16,
    maxInflightRequestBytes: 64 * 1024 * 1024,
    requestBodyTimeoutSeconds: 10,
    heartbeatIntervalSeconds: 15,
    toolGatewayTimeoutSeconds: 30,
    toolGatewayMaxResponseBytes: 64 * 1024,
    maxRunSeconds: 6 * 60 * 60,
    maxProviderDispatches: 128,
    maxTurns: 128,
    maxTotalTokens: 4_000_000,
    maxEventBytes: 16 * 1024 * 1024,
    maxRepeatedToolCalls: 8,
    shutdownGraceSeconds: 20,
  };
}

describe("Runtime config validation", () => {
  it("normalizes injected host and secret strings", () => {
    expect(
      validateRuntimeConfig({
        ...validConfig(),
        host: " 0.0.0.0 ",
        sharedSecret: ` ${"x".repeat(MIN_RUNTIME_SHARED_SECRET_BYTES)} `,
      }),
    ).toMatchObject({
      host: "0.0.0.0",
      sharedSecret: "x".repeat(MIN_RUNTIME_SHARED_SECRET_BYTES),
    });
  });

  it("rejects short injected secrets and unsupported bind host syntax", () => {
    expect(() => validateRuntimeConfig({
      ...validConfig(),
      sharedSecret: "short",
    })).toThrow(/sharedSecret/u);
    expect(() => validateRuntimeConfig({
      ...validConfig(),
      host: "runtime.internal",
    })).toThrow(/host/u);
  });

  it.each([
    { maxConcurrentRuns: 0 },
    { port: 65_536 },
    { maxRequestBytes: 1.5 },
    { maxInflightRequestBytes: 1024 },
    { maxEventBytes: 64 * 1024 },
    { shutdownGraceSeconds: 21 },
  ])("rejects invalid injected numeric config %#", (override) => {
    expect(() => validateRuntimeConfig({ ...validConfig(), ...override })).toThrow();
  });

  it("rejects missing and extra fields", () => {
    const missing: Record<string, unknown> = { ...validConfig() };
    delete missing.port;
    expect(() => validateRuntimeConfig(missing)).toThrow(/missing/u);
    expect(() => validateRuntimeConfig({ ...validConfig(), surprise: true })).toThrow(
      /unsupported/u,
    );
  });
});
