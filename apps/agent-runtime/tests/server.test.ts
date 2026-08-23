import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  fauxAssistantMessage,
  fauxProvider,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { once } from "node:events";
import { request as httpRequest } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";

import {
  AUTH_NONCE_HEADER,
  AUTH_SIGNATURE_HEADER,
  AUTH_TIMESTAMP_HEADER,
  signRuntimeRequest,
} from "../src/auth.js";
import type { RuntimeConfig } from "../src/config.js";
import { createRuntimeServer } from "../src/server.js";
import type { RuntimeDependencies } from "../src/runtime.js";
import { runtimeRequest, TEST_SECRET } from "./fixtures.js";

async function dependencies(
  tokensPerSecond = 100_000,
  responseText = "HTTP boundary OK",
): Promise<RuntimeDependencies> {
  const faux = fauxProvider({
    provider: "lumen-http-faux",
    models: [{ id: "faux-http-model", reasoning: true }],
    tokensPerSecond,
  });
  faux.setResponses([fauxAssistantMessage(responseText)]);
  const modelRuntime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsPath: null,
    modelsStore: new InMemoryModelsStore(),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  modelRuntime.registerNativeProvider(faux.provider);
  return {
    async prepareProvider() {
      return {
        modelRuntime,
        model: faux.getModel(),
        transport: { fetch: globalThis.fetch, close: async () => undefined },
        close: async () => undefined,
      };
    },
    createGateway: () => async () => {
      throw new Error("tool must remain disabled");
    },
  };
}

function testConfig(): RuntimeConfig {
  return {
    host: "127.0.0.1",
    port: 8090,
    sharedSecret: TEST_SECRET,
    authClockSkewSeconds: 30,
    nonceTtlSeconds: 120,
    nonceCacheSize: 100,
    maxRequestBytes: 8 * 1024 * 1024,
    maxLineBytes: 64 * 1024,
    maxStreamBytes: 8 * 1024 * 1024,
    maxEvents: 4096,
    maxConcurrentRuns: 2,
    requestBodyTimeoutSeconds: 2,
    heartbeatIntervalSeconds: 1,
  };
}

describe("Runtime HTTP boundary", () => {
  it("rejects framing limits that cannot reserve a terminal event", () => {
    expect(() =>
      createRuntimeServer({
        config: {
          ...testConfig(),
          maxLineBytes: 64 * 1024,
          maxStreamBytes: 64 * 1024,
        },
      }),
    ).toThrow(/at least twice/u);
  });

  it("streams one terminal event and rejects a replayed nonce", async () => {
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const body = Buffer.from(JSON.stringify(request), "utf8");
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = "http-boundary-nonce-0001";
    const signature = signRuntimeRequest(
      TEST_SECRET,
      "POST",
      "/v1/runs",
      timestamp,
      nonce,
      body,
    );
    const runtime = createRuntimeServer({
      config: testConfig(),
      dependencies: await dependencies(),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    const url = `http://127.0.0.1:${String(address.port)}/v1/runs`;
    const headers = {
      "content-type": "application/json",
      [AUTH_TIMESTAMP_HEADER]: timestamp,
      [AUTH_NONCE_HEADER]: nonce,
      [AUTH_SIGNATURE_HEADER]: signature,
    };
    try {
      const health = await fetch(
        `http://127.0.0.1:${String(address.port)}/healthz`,
      );
      expect(health.status).toBe(200);
      await expect(health.json()).resolves.toMatchObject({
        ok: true,
        service: "lumen-agent-runtime",
      });
      const ready = await fetch(
        `http://127.0.0.1:${String(address.port)}/readyz`,
      );
      expect(ready.status).toBe(200);
      const readyPayload: unknown = await ready.json();
      expect(readyPayload).toMatchObject({
        ok: true,
        runtime_version: "pi-0.84.2",
      });
      expect(
        (readyPayload as { auth_key_id?: unknown }).auth_key_id,
      ).toMatch(/^[0-9a-f]{16}$/u);

      const response = await fetch(url, { method: "POST", headers, body });
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toContain("application/x-ndjson");
      const lines = (await response.text()).trim().split("\n");
      const events = lines.map((line) => JSON.parse(line) as { type: string; seq: number });
      expect(events.map((event) => event.seq)).toEqual(
        events.map((_event, index) => index + 1),
      );
      expect(events.filter((event) => event.type.startsWith("run.")).at(-1)?.type).toBe(
        "run.completed",
      );
      expect(
        events.filter((event) =>
          new Set(["run.completed", "run.failed", "run.cancelled"]).has(event.type),
        ),
      ).toHaveLength(1);

      const replay = await fetch(url, { method: "POST", headers, body });
      expect(replay.status).toBe(401);
      await expect(replay.json()).resolves.toMatchObject({
        error: { code: "agent_runtime_auth_replayed" },
      });
    } finally {
      runtime.server.close();
      await once(runtime.server, "close");
    }
  });

  it("keeps slow provider turns alive with ordered heartbeat events", async () => {
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const body = Buffer.from(JSON.stringify(request), "utf8");
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = "http-heartbeat-nonce-0001";
    const signature = signRuntimeRequest(
      TEST_SECRET,
      "POST",
      "/v1/runs",
      timestamp,
      nonce,
      body,
    );
    const runtime = createRuntimeServer({
      config: testConfig(),
      dependencies: await dependencies(2, "Heartbeat protected response."),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    try {
      const response = await fetch(
        `http://127.0.0.1:${String(address.port)}/v1/runs`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            [AUTH_TIMESTAMP_HEADER]: timestamp,
            [AUTH_NONCE_HEADER]: nonce,
            [AUTH_SIGNATURE_HEADER]: signature,
          },
          body,
        },
      );
      expect(response.status).toBe(200);
      const events = (await response.text())
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as { type: string; seq: number });
      expect(events.some((event) => event.type === "run.heartbeat")).toBe(true);
      expect(events.map((event) => event.seq)).toEqual(
        events.map((_event, index) => index + 1),
      );
      expect(events.at(-1)?.type).toBe("run.completed");
    } finally {
      runtime.server.close();
      await once(runtime.server, "close");
    }
  }, 15_000);

  it("classifies host timeout as agent_run_timeout on every execution path", async () => {
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const body = Buffer.from(JSON.stringify(request), "utf8");
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = "http-timeout-nonce-0001";
    const signature = signRuntimeRequest(
      TEST_SECRET,
      "POST",
      "/v1/runs",
      timestamp,
      nonce,
      body,
    );
    const runtime = createRuntimeServer({
      config: testConfig(),
      dependencies: await dependencies(20, "Timeout response."),
      runTimeoutSignal: () => AbortSignal.timeout(25),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    try {
      const response = await fetch(
        `http://127.0.0.1:${String(address.port)}/v1/runs`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            [AUTH_TIMESTAMP_HEADER]: timestamp,
            [AUTH_NONCE_HEADER]: nonce,
            [AUTH_SIGNATURE_HEADER]: signature,
          },
          body,
        },
      );
      const events = (await response.text())
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line) as {
          type: string;
          error_code?: string;
        });
      expect(events.at(-1)).toMatchObject({
        type: "run.failed",
        error_code: "agent_run_timeout",
      });
    } finally {
      runtime.server.close();
      await once(runtime.server, "close");
    }
  }, 15_000);

  it("admits slow bodies before reading and releases the slot at the deadline", async () => {
    const config = { ...testConfig(), maxConcurrentRuns: 1, requestBodyTimeoutSeconds: 1 };
    const runtime = createRuntimeServer({
      config,
      dependencies: await dependencies(),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    const stalledClosed = new Promise<void>((resolve) => {
      const stalled = httpRequest({
        host: "127.0.0.1",
        port: address.port,
        path: "/v1/runs",
        method: "POST",
        headers: { "content-type": "application/json" },
      });
      stalled.on("error", () => resolve());
      stalled.on("close", () => resolve());
      stalled.write("{");
    });
    await new Promise((resolve) => setTimeout(resolve, 25));
    try {
      const rejected = await fetch(
        `http://127.0.0.1:${String(address.port)}/v1/runs`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
        },
      );
      expect(rejected.status).toBe(503);
      await expect(rejected.json()).resolves.toMatchObject({
        error: { code: "agent_runtime_capacity_exhausted" },
      });
      await expect(
        Promise.race([
          stalledClosed.then(() => "closed"),
          new Promise<string>((resolve) => setTimeout(() => resolve("open"), 1500)),
        ]),
      ).resolves.toBe("closed");
    } finally {
      runtime.server.close();
      await once(runtime.server, "close");
    }
  });
});
