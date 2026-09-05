import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  fauxAssistantMessage,
  fauxProvider,
  lazyStream,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { once } from "node:events";
import { request as httpRequest, type IncomingMessage } from "node:http";
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
    async prepareProvider(_request, onDispatch) {
      const streamSimple = modelRuntime.streamSimple.bind(modelRuntime);
      modelRuntime.streamSimple = (model, context, options) =>
        lazyStream(model, async () => {
          await onDispatch();
          return streamSimple(model, context, options);
        });
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
    maxConcurrentRuns: 2,
    maxConcurrentBodyReads: 4,
    maxInflightRequestBytes: 16 * 1024 * 1024,
    requestBodyTimeoutSeconds: 2,
    heartbeatIntervalSeconds: 1,
    toolGatewayTimeoutSeconds: 30,
    toolGatewayMaxResponseBytes: 64 * 1024,
    maxRunSeconds: 60,
    maxProviderDispatches: 16,
    maxTurns: 16,
    maxTotalTokens: 1_000_000,
    maxEventBytes: 1024 * 1024,
    maxRepeatedToolCalls: 4,
    shutdownGraceSeconds: 2,
  };
}

describe("Runtime HTTP boundary", () => {
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
        runtime_version: "pi-0.84.4",
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

  it("keeps an authenticated run slot available behind nine slow unauthenticated bodies", async () => {
    const config = {
      ...testConfig(),
      maxConcurrentRuns: 1,
      maxConcurrentBodyReads: 10,
      requestBodyTimeoutSeconds: 1,
    };
    const runtime = createRuntimeServer({
      config,
      dependencies: await dependencies(),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    const stalledClosed = Promise.all(
      Array.from({ length: 9 }, (_, index) => new Promise<void>((resolve) => {
        const stalled = httpRequest({
          host: "127.0.0.1",
          port: address.port,
          path: "/v1/runs",
          method: "POST",
          headers: {
            "content-type": "application/json",
            [AUTH_TIMESTAMP_HEADER]: String(Math.floor(Date.now() / 1000)),
            [AUTH_NONCE_HEADER]: `slow-unauthenticated-${String(index)}`,
            [AUTH_SIGNATURE_HEADER]: "0".repeat(64),
          },
        });
        stalled.on("response", (response) => response.resume());
        stalled.on("error", () => resolve());
        stalled.on("close", () => resolve());
        stalled.write("{");
      })),
    );
    await new Promise((resolve) => setTimeout(resolve, 25));
    try {
      const run = runtimeRequest({
        allowed_tools: [],
        tool_gateway_url: null,
        tool_capability: null,
      });
      const body = Buffer.from(JSON.stringify(run), "utf8");
      const timestamp = String(Math.floor(Date.now() / 1000));
      const nonce = "valid-request-after-slow-body";
      const signature = signRuntimeRequest(
        TEST_SECRET,
        "POST",
        "/v1/runs",
        timestamp,
        nonce,
        body,
      );
      const accepted = await fetch(
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
      expect(accepted.status).toBe(200);
      const events = await accepted.text();
      expect(events).toContain('"type":"run.completed"');
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

  it.each(["draining", "not_ready"] as const)(
    "rechecks %s after a delayed HTTP body and drains an already accepted run",
    async (lifecycle) => {
      const provider = await dependencies();
      let prepareCalls = 0;
      let releaseProvider!: () => void;
      let providerStarted!: () => void;
      const providerBarrier = new Promise<void>((resolve) => { releaseProvider = resolve; });
      const started = new Promise<void>((resolve) => { providerStarted = resolve; });
      const runtime = createRuntimeServer({
        config: testConfig(),
        dependencies: {
          ...provider,
          async prepareProvider(request, onDispatch) {
            prepareCalls += 1;
            providerStarted();
            await providerBarrier;
            return provider.prepareProvider(request, onDispatch);
          },
        },
      });
      runtime.readiness.state.ready = true;
      runtime.readiness.state.checkedAt = new Date().toISOString();
      runtime.server.listen(0, "127.0.0.1");
      await once(runtime.server, "listening");
      const address = runtime.server.address() as AddressInfo;
      const body = Buffer.from(JSON.stringify(runtimeRequest({
        allowed_tools: [],
        tool_gateway_url: null,
        tool_capability: null,
      })), "utf8");
      const timestamp = String(Math.floor(Date.now() / 1000));
      const headers = (nonce: string) => ({
        "content-type": "application/json",
        [AUTH_TIMESTAMP_HEADER]: timestamp,
        [AUTH_NONCE_HEADER]: nonce,
        [AUTH_SIGNATURE_HEADER]: signRuntimeRequest(
          TEST_SECRET, "POST", "/v1/runs", timestamp, nonce, body,
        ),
      });
      const delayed = httpRequest({
        host: "127.0.0.1",
        port: address.port,
        path: "/v1/runs",
        method: "POST",
        headers: {
          ...headers(`delayed-body-${lifecycle}-nonce`),
          "content-length": String(body.byteLength),
        },
      });
      // Observe actual body consumption, not a timer or a mocked readBody.
      const bodyStarted = new Promise<void>((resolve) => {
        runtime.server.once("request", (request) => {
          request.once("data", () => resolve());
        });
      });
      const rejectedResponse = once(delayed, "response");
      delayed.write(body.subarray(0, 1));
      let shutdown: Promise<void> | undefined;
      try {
        await bodyStarted;
        const accepted = await fetch(`http://127.0.0.1:${String(address.port)}/v1/runs`, {
          method: "POST",
          headers: headers(`accepted-${lifecycle}-nonce`),
          body,
        });
        expect(accepted.status).toBe(200);
        await started;
        expect((await runtime.metrics.activeRuns.get()).values[0]?.value).toBe(1);

        if (lifecycle === "draining") shutdown = runtime.shutdown();
        else runtime.readiness.state.ready = false;
        delayed.end(body.subarray(1));
        const [rejected] = await rejectedResponse as [IncomingMessage];
        const chunks: Buffer[] = [];
        for await (const chunk of rejected as AsyncIterable<Buffer>) chunks.push(chunk);
        expect(rejected.statusCode).toBe(503);
        expect(JSON.parse(Buffer.concat(chunks).toString("utf8"))).toMatchObject({
          error: { code: `agent_runtime_${lifecycle}` },
        });
        expect(prepareCalls).toBe(1);
        expect((await runtime.metrics.activeRuns.get()).values[0]?.value).toBe(1);

        releaseProvider();
        const events = (await accepted.text()).trim().split("\n")
          .map((line) => JSON.parse(line) as { type: string });
        expect(events.at(-1)?.type).toBe("run.completed");
        expect(events.filter((event) => /run\.(completed|failed|cancelled)/u.test(event.type)))
          .toHaveLength(1);
        await (shutdown ?? runtime.shutdown());
        expect((await runtime.metrics.activeRuns.get()).values[0]?.value).toBe(0);
        expect(prepareCalls).toBe(1);
      } finally {
        releaseProvider();
        delayed.destroy();
        await rejectedResponse.catch(() => undefined);
        await runtime.shutdown();
      }
    },
  );

  it.each(["destroyed", "ended"] as const)(
    "does not reserve a run when the response is %s at body completion",
    async (closed) => {
      let prepareCalls = 0;
      const runtime = createRuntimeServer({
        config: testConfig(),
        dependencies: {
          async prepareProvider() {
            prepareCalls += 1;
            throw new Error("closed response must not execute");
          },
          createGateway: () => async () => {
            throw new Error("closed response must not call tools");
          },
        },
      });
      runtime.readiness.state.ready = true;
      runtime.server.once("request", (request, response) => {
        request.once("end", () => {
          if (closed === "destroyed") response.destroy();
          else response.end();
        });
      });
      runtime.server.listen(0, "127.0.0.1");
      await once(runtime.server, "listening");
      const address = runtime.server.address() as AddressInfo;
      const body = Buffer.from(JSON.stringify(runtimeRequest()));
      const timestamp = String(Math.floor(Date.now() / 1000));
      const nonce = `closed-response-${closed}-nonce`;
      try {
        const result = await new Promise<number | string>((resolve) => {
          const request = httpRequest({
            host: "127.0.0.1",
            port: address.port,
            path: "/v1/runs",
            method: "POST",
            headers: {
              "content-type": "application/json",
              [AUTH_TIMESTAMP_HEADER]: timestamp,
              [AUTH_NONCE_HEADER]: nonce,
              [AUTH_SIGNATURE_HEADER]: signRuntimeRequest(
                TEST_SECRET, "POST", "/v1/runs", timestamp, nonce, body,
              ),
            },
          });
          request.on("response", (response) => {
            response.on("end", () => resolve(response.statusCode ?? 0));
            response.resume();
          });
          request.on("error", (error: NodeJS.ErrnoException) => resolve(error.code ?? "unknown"));
          request.end(body);
        });
        expect(result).toBe(closed === "destroyed" ? "ECONNRESET" : 200);
        expect(prepareCalls).toBe(0);
        expect((await runtime.metrics.activeRuns.get()).values[0]?.value).toBe(0);
      } finally {
        await runtime.shutdown();
      }
    },
  );

  it("marks readiness draining and aborts a run after the shutdown grace period", async () => {
    const runtime = createRuntimeServer({
      config: { ...testConfig(), shutdownGraceSeconds: 1 },
      dependencies: await dependencies(1, "slow ".repeat(100)),
    });
    runtime.readiness.state.ready = true;
    runtime.readiness.state.checkedAt = new Date().toISOString();
    runtime.server.listen(0, "127.0.0.1");
    await once(runtime.server, "listening");
    const address = runtime.server.address() as AddressInfo;
    const run = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const body = Buffer.from(JSON.stringify(run), "utf8");
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = "shutdown-active-run-nonce";
    const response = await fetch(
      `http://127.0.0.1:${String(address.port)}/v1/runs`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          [AUTH_TIMESTAMP_HEADER]: timestamp,
          [AUTH_NONCE_HEADER]: nonce,
          [AUTH_SIGNATURE_HEADER]: signRuntimeRequest(
            TEST_SECRET,
            "POST",
            "/v1/runs",
            timestamp,
            nonce,
            body,
          ),
        },
        body,
      },
    );

    await runtime.shutdown();
    const events = (await response.text())
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line) as Record<string, unknown>);
    expect(runtime.readiness.state).toMatchObject({
      ready: false,
      errorCode: "agent_runtime_draining",
    });
    expect(events.at(-1)).toMatchObject({
      type: "run.failed",
      error_code: "agent_runtime_shutdown",
    });
  }, 15_000);
});
