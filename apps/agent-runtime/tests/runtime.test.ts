import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
  lazyStream,
} from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { describe, expect, it } from "vitest";

import { CollectingEventWriter } from "../src/ndjson.js";
import {
  boundedTurnUsage,
  DEFAULT_RUNTIME_SAFETY_POLICY,
  executeAgentRun,
  RuntimeExecutionError,
  type RuntimeDependencies,
} from "../src/runtime.js";
import {
  ToolGatewayError,
  type CreateImageGateway,
} from "../src/tools/gateway.js";
import { runtimeModel } from "../src/providers/runtime-provider.js";
import { runtimeRequest, runtimeRequestV3 } from "./fixtures.js";

async function fakeDependencies(
  responses: Parameters<ReturnType<typeof fauxProvider>["setResponses"]>[0],
  gateway: CreateImageGateway,
  tokensPerSecond = 100_000,
  observeOptions?: (options: { readonly timeoutMs?: number } | undefined) => void,
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
    async prepareProvider(_request, onDispatch) {
      const streamSimple = modelRuntime.streamSimple.bind(modelRuntime);
      modelRuntime.streamSimple = (model, context, options) =>
        lazyStream(model, async () => {
          observeOptions?.(options);
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
    createGateway: () => gateway,
  };
}

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolveValue: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    resolveValue = resolve;
  });
  if (!resolveValue) throw new Error("deferred initialization failed");
  return { promise, resolve: resolveValue };
}

describe("Pi Runtime execution", () => {
  it("advertises GPT-5.6 max reasoning to Pi", () => {
    const request = runtimeRequestV3({
      provider: {
        ...runtimeRequest().provider,
        model: "openai/gpt-5.6-sol",
        context_window: 272_000,
        max_output_tokens: 16_384,
        thinking_level_map: { xhigh: "xhigh", max: "max" },
      },
      reasoning_effort: "max",
    });

    const model = runtimeModel(request);

    expect(model.contextWindow).toBe(272_000);
    expect(model.maxTokens).toBe(16_384);
    expect(model.thinkingLevelMap).toMatchObject({
      xhigh: "xhigh",
      max: "max",
    });
  });

  it("preserves Pi's provider-native length stop as a partial result", async () => {
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("Provider-native terminal text.", { stopReason: "length" })],
      async () => {
        throw new Error("image gateway must not run");
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

    expect(result.outcome).toBe("partial");
    expect(result.errorCode).toBe("agent_output_truncated");
    expect(writer.events).toContainEqual(
      expect.objectContaining({ type: "turn.completed", stop_reason: "length" }),
    );
  });

  it("uses Pi's native disabled-timeout setting for Provider requests", async () => {
    let timeoutMs: number | undefined;
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("No host deadline.")],
      async () => {
        throw new Error("image gateway must not run");
      },
      100_000,
      (options) => {
        timeoutMs = options?.timeoutMs;
      },
    );
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });

    await executeAgentRun(
      request,
      new CollectingEventWriter(request.run_id, request.execution_epoch),
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(timeoutMs).toBe(2_147_483_647);
  });

  it("continues server-side from a transcript ending in the source user turn", async () => {
    const dependencies = await fakeDependencies(
      [fauxAssistantMessage("Continuation from the same Pi transcript.")],
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const request = runtimeRequestV3({
      history: [
        {
          message_id: "source-user-message",
          role: "user",
          text: "Write a long response",
        },
      ],
      operation: "continue",
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      references: [],
    });

    await expect(
      executeAgentRun(
        request,
        new CollectingEventWriter(request.run_id, request.execution_epoch),
        new AbortController().signal,
        undefined,
        dependencies,
      ),
    ).resolves.toMatchObject({ outcome: "succeeded", providerDispatchCount: 1 });
  });

  it("uses Pi native compaction and emits a durable checkpoint", async () => {
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          "## Goal\nPreserve the full creative session.\n\n## Next Steps\n1. Continue.",
        ),
        fauxAssistantMessage("Continued after native Pi compaction."),
      ],
      async () => {
        throw new Error("image gateway must not run");
      },
    );
    const history = Array.from({ length: 48 }, (_, index) => ({
      message_id: `history-${String(index)}`,
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      text: `history ${String(index)} ${"context ".repeat(1_250)}`,
    }));
    const request = runtimeRequest({
      history,
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      current_prompt: "Continue the task.",
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
    expect(writer.events[0]?.type).toBe("run.started");
    const checkpoint = writer.events.find(
      (event) => event.type === "compaction.completed",
    );
    expect(checkpoint?.checkpoint_version).toBe(1);
    expect(checkpoint?.pi_runtime_version).toBe("pi-0.84.2");
    expect(String(checkpoint?.summary)).toContain(
      "Preserve the full creative session",
    );
    expect(typeof checkpoint?.tokens_before).toBe("number");
    expect(checkpoint?.provider_call_count).toBe(1);
    expect(typeof checkpoint?.usage).toBe("object");
    expect(String(checkpoint?.first_kept_message_id)).toMatch(/^history-/u);
    expect(checkpoint).not.toHaveProperty("next_message_id");
    expect(checkpoint).not.toHaveProperty("phase");
    expect(result.usage.total_tokens).toBeGreaterThan(0);
  });

  it("places a restored checkpoint before newer messages for repeat compaction", async () => {
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage("## Summary\nSecond native summary."),
        fauxAssistantMessage("## Continuation\nPreserve the latest retained turn."),
        fauxAssistantMessage("Answer after second compaction."),
      ],
      async () => {
        throw new Error("image gateway must not run");
      },
    );
    const repeated = "context ".repeat(15_000);
    const request = runtimeRequest({
      history: [
        { message_id: "retained-user", role: "user", text: "retained detail" },
        { message_id: "source-user", role: "user", text: repeated },
        { message_id: "source-assistant", role: "assistant", text: repeated },
        { message_id: "later-user", role: "user", text: repeated },
        { message_id: "later-assistant", role: "assistant", text: repeated },
      ],
      compaction: {
        summary: "## Summary\nPrevious native summary.",
        first_kept_message_id: "retained-user",
        next_message_id: "source-user",
        tokens_before: 120_000,
      },
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      current_prompt: "Continue after the second checkpoint.",
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
    expect(
      writer.events.filter((event) => event.type === "compaction.completed"),
    ).toHaveLength(1);
    expect(
      writer.events.find((event) => event.type === "compaction.completed"),
    ).toMatchObject({ provider_call_count: 2 });
  });

  it("does not persist unsafe current-turn overflow compaction", async () => {
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage("Discard this truncated draft.", { stopReason: "length" }),
        fauxAssistantMessage("## Goal\nPreserve context before retry."),
        fauxAssistantMessage("Complete regenerated answer."),
      ],
      async () => {
        throw new Error("image gateway must not run");
      },
    );
    const history = Array.from({ length: 12 }, (_, index) => ({
      message_id: `retry-history-${String(index)}`,
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      text: `history ${String(index)} ${"context ".repeat(1_250)}`,
    }));
    const request = runtimeRequest({
      history,
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      current_prompt: "Return the complete answer.",
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result.outcome).toBe("partial");
    expect(result.errorCode).toBe("agent_output_truncated");
    const resetIndex = writer.events.findIndex((event) => event.type === "text.reset");
    expect(resetIndex).toBe(-1);
    expect(
      writer.events.some((event) => event.type === "compaction.completed"),
    ).toBe(false);
  });

  it("keeps Pi transient-error retry semantics and resets the failed draft", async () => {
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage("Discard this transient draft.", {
          stopReason: "error",
          errorMessage: "rate limit exceeded",
        }),
        fauxAssistantMessage("Answer after Pi retry."),
      ],
      async () => {
        throw new Error("image gateway must not run");
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
    expect(result.errorCode).toBeNull();
    expect(writer.events.some((event) => event.type === "text.reset")).toBe(true);
    expect(result.providerDispatchCount).toBe(2);
  }, 10_000);

  it("restores a persisted Pi checkpoint with its retained tail", async () => {
    const dependencies = await fakeDependencies(
      [
        (context) => {
          expect(JSON.stringify(context.messages)).toContain(
            "Persisted Pi summary",
          );
          expect(JSON.stringify(context.messages)).toContain("retained detail");
          return fauxAssistantMessage("Checkpoint restored.");
        },
      ],
      async () => {
        throw new Error("image gateway must not run");
      },
    );
    const request = runtimeRequest({
      history: [
        {
          message_id: "retained-user",
          role: "user",
          text: "retained detail",
        },
        {
          message_id: "retained-assistant",
          role: "assistant",
          text: "retained answer",
        },
      ],
      compaction: {
        summary: "## Goal\nPersisted Pi summary",
        first_kept_message_id: "retained-user",
        next_message_id: "retained-assistant",
        tokens_before: 260_000,
      },
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      current_prompt: "Continue.",
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
    expect(
      writer.events.some((event) => event.type === "compaction.completed"),
    ).toBe(false);
  });

  it("does not preserve discarded text progress after reset and safety trip", async () => {
    const base = await fakeDependencies(
      [
        fauxAssistantMessage("Discard this transient draft.", {
          stopReason: "error",
          errorMessage: "rate limit exceeded",
        }),
        fauxAssistantMessage("must not complete"),
      ],
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const dependencies: RuntimeDependencies = {
      ...base,
      safetyPolicy: {
        ...DEFAULT_RUNTIME_SAFETY_POLICY,
        maxProviderDispatches: 1,
      },
    };
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

    expect(writer.events.some((event) => event.type === "text.reset")).toBe(true);
    expect(result).toMatchObject({
      outcome: "failed",
      errorCode: "agent_safety_budget_reached",
    });
  });

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

  it("fails the run when a requested session reference is unavailable", async () => {
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall(
            "lumen_create_image",
            { prompt: "Revise prior image", reference_labels: ["ref_1"] },
            { id: "missing-reference" },
          ),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("The reference is unavailable."),
      ],
      async () => {
        throw new Error("gateway must not be called");
      },
    );
    const request = runtimeRequest({ references: [] });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result.outcome).toBe("partial");
    expect(result.errorCode).toBe("agent_reference_not_found");
    expect(result.toolCallCount).toBe(1);
    expect(writer.events.find((event) => event.type === "tool.failed")).toMatchObject({
      error_code: "agent_reference_not_found",
      result_unknown: false,
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
      tool_policy: { ...runtimeRequest().tool_policy, max_images_per_run: 1 },
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(0);
    expect(result).toMatchObject({
      outcome: "partial",
      errorCode: "agent_image_limit_reached",
      toolCallCount: 1,
    });
    expect(writer.events).toContainEqual(
      expect.objectContaining({
        type: "tool.failed",
        error_code: "agent_image_limit_reached",
        result_unknown: false,
      }),
    );
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
      tool_policy: { ...runtimeRequest().tool_policy, max_image_tool_calls: 1 },
    });
    const writer = new CollectingEventWriter(request.run_id, request.execution_epoch);

    const result = await executeAgentRun(
      request,
      writer,
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(gatewayCalls).toBe(1);
    expect(result).toMatchObject({
      outcome: "partial",
      errorCode: "agent_tool_limit_reached",
      toolCallCount: 2,
    });
    expect(writer.events).toContainEqual(
      expect.objectContaining({ type: "limit.reached", reason: "tool_calls" }),
    );
  });

  it("lets Pi finish naturally across every business-authorized tool turn", async () => {
    let gatewayCalls = 0;
    const dependencies = await fakeDependencies(
      [
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "first" }, { id: "turn-tool-1" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage(
          fauxToolCall("lumen_create_image", { prompt: "second" }, { id: "turn-tool-2" }),
          { stopReason: "toolUse" },
        ),
        fauxAssistantMessage("Both image jobs were accepted."),
      ],
      async (_id, _ordinal, arguments_) => {
        gatewayCalls += 1;
        return {
          generation_ids: [`generation-turn-${String(gatewayCalls)}`],
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
    expect(result.turnCount).toBe(3);
    expect(gatewayCalls).toBe(2);
    expect(writer.events).not.toContainEqual(
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

  it("stops an unbounded unknown-tool loop before the next Provider dispatch", async () => {
    const base = await fakeDependencies(
      Array.from({ length: 20 }, (_, index) =>
        fauxAssistantMessage(
          fauxToolCall("bash", { command: "id" }, { id: `unknown-${String(index)}` }),
          { stopReason: "toolUse" },
        )),
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const dependencies: RuntimeDependencies = {
      ...base,
      safetyPolicy: {
        maxWallClockMs: 10_000,
        maxProviderDispatches: 20,
        maxTurns: 20,
        maxTotalTokens: 1_000_000,
        maxEventBytes: 1024 * 1024,
        maxRepeatedToolCalls: 4,
      },
    };
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });

    await expect(
      executeAgentRun(
        request,
        new CollectingEventWriter(request.run_id, request.execution_epoch),
        new AbortController().signal,
        undefined,
        dependencies,
      ),
    ).resolves.toMatchObject({
      outcome: "failed",
      errorCode: "agent_safety_budget_reached",
      providerDispatchCount: 5,
    });
  });

  it("enforces the signed run dispatch budget below the server ceiling", async () => {
    const permits: number[] = [];
    const base = await fakeDependencies(
      Array.from({ length: 8 }, (_, index) =>
        fauxAssistantMessage(
          fauxToolCall("bash", { command: "id" }, { id: `budget-${String(index)}` }),
          { stopReason: "toolUse" },
        )),
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const dependencies: RuntimeDependencies = {
      ...base,
      async authorizeProviderDispatch(_request, ordinal) {
        permits.push(ordinal);
      },
    };
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      provider_dispatch_url: "http://api:8000/internal/agent/runs/run-1/provider-dispatch",
      provider_dispatch_capability: "dispatch-capability-token-more-than-32-characters",
      safety_budget: { max_provider_dispatches: 2 },
    });

    const result = await executeAgentRun(
      request,
      new CollectingEventWriter(request.run_id, request.execution_epoch),
      new AbortController().signal,
      undefined,
      dependencies,
    );

    expect(result).toMatchObject({
      outcome: "failed",
      errorCode: "agent_safety_budget_reached",
      providerDispatchCount: 2,
    });
    expect(permits).toEqual([1, 2]);
  });

  it("does not dispatch when cancellation wins while the permit is pending", async () => {
    const permitStarted = deferred();
    const permitRelease = deferred();
    const base = await fakeDependencies(
      [fauxAssistantMessage("must not start")],
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const dependencies: RuntimeDependencies = {
      ...base,
      async authorizeProviderDispatch() {
        permitStarted.resolve();
        await permitRelease.promise;
      },
    };
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
      provider_dispatch_url: "http://api:8000/internal/agent/runs/run-1/provider-dispatch",
      provider_dispatch_capability: "dispatch-capability-token-more-than-32-characters",
      safety_budget: { max_provider_dispatches: 2 },
    });
    const controller = new AbortController();
    const execution = executeAgentRun(
      request,
      new CollectingEventWriter(request.run_id, request.execution_epoch),
      controller.signal,
      undefined,
      dependencies,
    );
    await permitStarted.promise;
    controller.abort("agent_cancelled");
    permitRelease.resolve();

    await expect(execution).resolves.toMatchObject({
      outcome: "cancelled",
      errorCode: "agent_cancelled",
      providerDispatchCount: 0,
    });
  });

  it("does not let provider cleanup failure replace a known successful result", async () => {
    const base = await fakeDependencies(
      [fauxAssistantMessage("Known result")],
      async () => {
        throw new Error("tool must remain disabled");
      },
    );
    const dependencies: RuntimeDependencies = {
      ...base,
      async prepareProvider(request, onDispatch) {
        const prepared = await base.prepareProvider(request, onDispatch);
        return {
          ...prepared,
          async close() {
            throw new Error("injected cleanup failure");
          },
        };
      },
    };
    const request = runtimeRequest({
      allowed_tools: [],
      tool_gateway_url: null,
      tool_capability: null,
    });

    await expect(
      executeAgentRun(
        request,
        new CollectingEventWriter(request.run_id, request.execution_epoch),
        new AbortController().signal,
        undefined,
        dependencies,
      ),
    ).resolves.toMatchObject({ outcome: "succeeded", errorCode: null });
  });
});
