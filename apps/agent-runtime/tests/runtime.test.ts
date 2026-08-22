import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { describe, expect, it } from "vitest";

import { CollectingEventWriter } from "../src/ndjson.js";
import {
  boundedTurnUsage,
  executeAgentRun,
  RuntimeExecutionError,
  type RuntimeDependencies,
} from "../src/runtime.js";
import {
  ToolGatewayError,
  type CreateImageGateway,
} from "../src/tools/gateway.js";
import { runtimeRequest } from "./fixtures.js";

async function fakeDependencies(
  responses: ReturnType<typeof fauxAssistantMessage>[],
  gateway: CreateImageGateway,
  tokensPerSecond = 100_000,
): Promise<RuntimeDependencies> {
  const faux = fauxProvider({
    provider: "lumen-faux",
    models: [{ id: "faux-model", reasoning: true, input: ["text", "image"] }],
    tokensPerSecond,
  });
  faux.setResponses(responses);
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
    createGateway: () => gateway,
  };
}

describe("Pi Runtime execution", () => {
  it("proves text -> lumen tool -> text ordering with exactly one tool", async () => {
    const gatewayCalls: Array<{ id: string; ordinal: number }> = [];
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          [
            fauxText("I will submit the image. "),
            fauxToolCall(
              "lumen_create_image",
              { prompt: "A red product on white", count: 1, reference_labels: [] },
              { id: "call-image-1" },
            ),
          ],
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("The image job is now available in Lumen."),
      ],
      async (id, ordinal) => {
        gatewayCalls.push({ id, ordinal });
        return {
          generation_ids: ["generation-1"],
          mode: "text_to_image",
          replayed: false,
          accepted: {
            prompt: "A red product on white",
            reference_labels: [],
            count: 1,
            aspect_ratio: "1:1",
            quality: "2k",
            render_quality: "high",
            background: "auto",
            output_format: "webp",
          },
        };
      },
    );
    const request = runtimeRequest();
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);
    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result.outcome).toBe("succeeded");
    expect(result.turnCount).toBe(2);
    expect(result.toolCallCount).toBe(1);
    expect(gatewayCalls).toEqual([{ id: "call-image-1", ordinal: 0 }]);
    const types = writer.events.map((event) => event.type);
    const firstText = types.indexOf("text.delta");
    const toolStart = types.indexOf("tool.started");
    const toolSuccess = types.indexOf("tool.succeeded");
    const firstTurn = types.indexOf("turn.completed");
    const secondTurn = types.lastIndexOf("turn.completed");
    expect(types[0]).toBe("run.started");
    expect(firstText).toBeGreaterThan(0);
    expect(toolStart).toBeGreaterThan(firstText);
    expect(toolSuccess).toBeGreaterThan(toolStart);
    expect(firstTurn).toBeGreaterThan(toolSuccess);
    expect(secondTurn).toBeGreaterThan(firstTurn);
    expect(writer.events.find((event) => event.type === "tool.succeeded")).toMatchObject({
      generation_ids: ["generation-1"],
    });
  });

  it("cannot execute an unregistered bash tool", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall("bash", { command: "id" }, { id: "forbidden-call" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("That tool is unavailable."),
      ],
      async () => {
        gatewayCalls += 1;
        throw new Error("must not execute");
      },
    );
    const request = runtimeRequest();
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);
    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result.outcome).toBe("succeeded");
    expect(gatewayCalls).toBe(0);
    expect(writer.events.find((event) => event.type === "tool.failed")).toMatchObject({
      name: "bash",
      error_code: "agent_tool_not_allowed",
    });
  });

  it("registers no tool when image actions are disabled", async () => {
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("Text only response")],
      async () => {
        throw new Error("must not execute");
      },
    );
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);
    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );
    expect(result.outcome).toBe("succeeded");
    expect(writer.events[0]).toMatchObject({ type: "run.started", tools: [] });
  });

  it("marks an unknown image submission result partial without retrying", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall(
            "lumen_create_image",
            { prompt: "Unknown submission", count: 1 },
            { id: "unknown-image-call" },
          ),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("The submission result could not be confirmed."),
      ],
      async () => {
        gatewayCalls += 1;
        throw new ToolGatewayError("agent_tool_result_unknown", true);
      },
    );
    const request = runtimeRequest();
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(1);
    expect(result.outcome).toBe("partial");
    expect(result.errorCode).toBe("agent_tool_result_unknown");
    expect(writer.events.find((event) => event.type === "tool.failed")).toMatchObject({
      result_unknown: true,
      error_code: "agent_tool_result_unknown",
    });
  });

  it("blocks a new ordinal after an unknown image acknowledgement", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall(
            "lumen_create_image",
            { prompt: "first uncertain batch", count: 1 },
            { id: "unknown-first" },
          ),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage(
          fauxToolCall(
            "lumen_create_image",
            { prompt: "must not submit again", count: 1 },
            { id: "forbidden-second" },
          ),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("The first submission remains unconfirmed."),
      ],
      async () => {
        gatewayCalls += 1;
        throw new ToolGatewayError("agent_tool_result_unknown", true);
      },
    );
    const request = runtimeRequest();
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(1);
    expect(result.outcome).toBe("partial");
    expect(writer.events).toContainEqual(
      expect.objectContaining({
        type: "limit.reached",
        reason: "tool_result_unknown",
      }),
    );
  });

  it("preserves reasoning and one-hour cache usage without double counting", async () => {
    const usage = boundedTurnUsage({
      input: 100,
      output: 20,
      cacheRead: 40,
      cacheWrite: 10,
      cacheWrite1h: 3,
      reasoning: 7,
      totalTokens: 170,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    }, runtimeRequest());
    expect(usage).toEqual({
      input_tokens: 100,
      output_tokens: 20,
      cache_read_tokens: 40,
      cache_write_tokens: 10,
      cache_write_1h_tokens: 3,
      reasoning_tokens: 7,
      total_tokens: 170,
    });
  });

  it("rejects impossible provider usage above the request limits", async () => {
    expect(() =>
      boundedTurnUsage(
        {
          input: 1,
          output: 5000,
          cacheRead: 0,
          cacheWrite: 0,
          reasoning: 0,
          totalTokens: 5001,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        runtimeRequest(),
      ),
    ).toThrow(/invalid output usage/u);
  });

  it("carries completed-turn usage through a later Runtime exception", async () => {
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("paid turn")],
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });
    const collected = new CollectingEventWriter(request.run_id, request.execution_epoch);
    const writer = {
      get sequence() { return collected.sequence; },
      get bytesWritten() { return collected.bytesWritten; },
      async emit(type: string, payload: Record<string, unknown> = {}, force = false) {
        if (type === "turn.completed") return false;
        return collected.emit(type, payload, force);
      },
    };

    let captured: RuntimeExecutionError | null = null;
    try {
      await executeAgentRun(
        request,
        writer,
        new AbortController().signal,
        undefined,
        dependencies,
      );
    } catch (error) {
      if (error instanceof RuntimeExecutionError) captured = error;
      else throw error;
    }
    expect(captured).not.toBeNull();
    expect(captured?.result.turnCount).toBeGreaterThanOrEqual(1);
    expect(captured?.result.usage.total_tokens).toBeGreaterThan(0);
  });

  it("enforces image count before the Gateway can create a side effect", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall(
            "lumen_create_image",
            { prompt: "too many", count: 2 },
            { id: "image-limit-call" },
          ),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("The configured image limit prevented submission."),
      ],
      async () => {
        gatewayCalls += 1;
        throw new Error("must not reach Gateway");
      },
    );
    const request = runtimeRequest({
      limits: { ...runtimeRequest().limits, max_images_per_run: 1 },
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(0);
    expect(writer.events).toContainEqual(
      expect.objectContaining({ type: "limit.reached", reason: "images" }),
    );
  });

  it("enforces the tool-call ceiling before a second Gateway submission", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "first" }, { id: "tool-1" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "second" }, { id: "tool-2" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("Only the first request was submitted."),
      ],
      async (_id, _ordinal, arguments_) => {
        gatewayCalls += 1;
        return {
          generation_ids: [`generation-${String(gatewayCalls)}`],
          mode: "text_to_image",
          replayed: false,
          accepted: {
            prompt: arguments_.prompt,
            reference_labels: [],
            count: 1,
            aspect_ratio: "1:1",
            quality: "2k",
            render_quality: "high",
            background: "auto",
            output_format: "webp",
          },
        };
      },
    );
    const request = runtimeRequest({
      limits: { ...runtimeRequest().limits, max_tool_calls: 1 },
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(1);
    expect(writer.events).toContainEqual(
      expect.objectContaining({ type: "limit.reached", reason: "tool_calls" }),
    );
  });

  it("stops at max turns and switches the final turn to no-tools", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "first" }, { id: "turn-tool-1" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "blocked" }, { id: "turn-tool-2" }),
          { stopReason: "toolUse" },
        ),
      ],
      async (_id, _ordinal, arguments_) => {
        gatewayCalls += 1;
        return {
          generation_ids: ["generation-turn-limit"],
          mode: "text_to_image",
          replayed: false,
          accepted: {
            prompt: arguments_.prompt,
            reference_labels: [],
            count: 1,
            aspect_ratio: "1:1",
            quality: "2k",
            render_quality: "high",
            background: "auto",
            output_format: "webp",
          },
        };
      },
    );
    const request = runtimeRequest({
      limits: { ...runtimeRequest().limits, max_turns: 2 },
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result.turnCount).toBeLessThanOrEqual(2);
    expect(gatewayCalls).toBe(1);
    expect(writer.events).toContainEqual(
      expect.objectContaining({ type: "limit.reached", reason: "turns" }),
    );
  });

  it("honors a host timeout signal while a Provider turn is in flight", async () => {
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("too slow")],
      async () => {
        throw new Error("tool must remain disabled");
      },
      1,
    );
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });

    const result = await executeAgentRun(
      request,
      new CollectingEventWriter(request.run_id, request.execution_epoch),
      AbortSignal.timeout(5),
      undefined,
      dependencies,
    );
    expect(result.outcome).toBe("cancelled");
    expect(result.errorCode).toBe("agent_cancelled");
  });
});
