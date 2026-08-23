import type { AgentEvent, AgentMessage } from "@earendil-works/pi-agent-core";
import {
  InMemoryCredentialStore,
  InMemoryModelsStore,
  fauxProvider,
  type AssistantMessage,
  type ImageContent,
  type Message,
  type Usage,
} from "@earendil-works/pi-ai";
import {
  createAgentSession,
  type CompactionResult,
  estimateTokens,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  shouldCompact,
} from "@earendil-works/pi-coding-agent";

import {
  AGENT_TOOL_CREATE_IMAGE,
  type RuntimeHistoryMessage,
  type RuntimeRequest,
  type RuntimeUsage,
} from "./contracts.js";
import { RUNTIME_VERSION } from "./config.js";
import type { RuntimeMetrics } from "./metrics.js";
import type { EventWriter } from "./ndjson.js";
import {
  prepareProviderRuntime,
  type PreparedProviderRuntime,
} from "./providers/runtime-provider.js";
import { emptyResourceLoader } from "./resource-loader.js";
import { createImageTool, ordinalFor, type ToolRuntimeState } from "./tools/create-image.js";
import {
  createImageGateway,
  type CreateImageGateway,
} from "./tools/gateway.js";

const EMPTY_USAGE: Usage = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

export interface RuntimeDependencies {
  prepareProvider(
    request: RuntimeRequest,
    onDispatch: () => Promise<void>,
  ): Promise<PreparedProviderRuntime>;
  createGateway(request: RuntimeRequest): CreateImageGateway;
}

export interface ExecuteResult {
  readonly outcome: "succeeded" | "partial" | "failed" | "cancelled";
  readonly errorCode: string | null;
  readonly usage: RuntimeUsage;
  readonly turnCount: number;
  readonly toolCallCount: number;
  readonly providerDispatchCount: number;
  readonly providerCompletedCount: number;
}

export class RuntimeExecutionError extends Error {
  constructor(
    readonly result: ExecuteResult,
    cause: unknown,
  ) {
    super("Agent Runtime execution failed", { cause });
    this.name = "RuntimeExecutionError";
  }
}

const DEFAULT_DEPENDENCIES: RuntimeDependencies = {
  prepareProvider: prepareProviderRuntime,
  createGateway: createImageGateway,
};

function zeroUsage(): RuntimeUsage {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cache_write_1h_tokens: 0,
    reasoning_tokens: 0,
    total_tokens: 0,
  };
}

function tokenValue(value: number | undefined, name: string, maximum: number): number {
  if (value === undefined) return 0;
  if (!Number.isSafeInteger(value) || value < 0 || value > maximum) {
    throw new Error(`provider reported invalid ${name}`);
  }
  return value;
}

function boundedProviderUsage(
  usage: Usage,
  request: RuntimeRequest,
  providerCallCount: number,
): RuntimeUsage {
  if (!Number.isSafeInteger(providerCallCount) || providerCallCount < 1) {
    throw new Error("invalid provider call count");
  }
  const input = tokenValue(
    usage.input,
    "input usage",
    request.provider.context_window * providerCallCount,
  );
  const output = tokenValue(
    usage.output,
    "output usage",
    request.limits.max_output_tokens * providerCallCount,
  );
  const cacheRead = tokenValue(
    usage.cacheRead,
    "cache-read usage",
    request.provider.context_window * providerCallCount,
  );
  const cacheWrite = tokenValue(
    usage.cacheWrite,
    "cache-write usage",
    request.provider.context_window * providerCallCount,
  );
  const cacheWrite1h = tokenValue(
    usage.cacheWrite1h,
    "one-hour cache-write usage",
    cacheWrite,
  );
  const reasoning = tokenValue(usage.reasoning, "reasoning usage", output);
  if (
    input + cacheRead + cacheWrite >
    request.provider.context_window * providerCallCount
  ) {
    throw new Error("provider input usage exceeds the model context window");
  }
  return {
    input_tokens: input,
    output_tokens: output,
    cache_read_tokens: cacheRead,
    cache_write_tokens: cacheWrite,
    cache_write_1h_tokens: cacheWrite1h,
    reasoning_tokens: reasoning,
    total_tokens: input + output + cacheRead + cacheWrite,
  };
}

export function boundedTurnUsage(usage: Usage, request: RuntimeRequest): RuntimeUsage {
  return boundedProviderUsage(usage, request, 1);
}

function addUsage(target: RuntimeUsage, usage: RuntimeUsage): void {
  for (const key of [
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "reasoning_tokens",
  ] as const) {
    target[key] += usage[key];
  }
  target.total_tokens =
    target.input_tokens + target.output_tokens +
    target.cache_read_tokens + target.cache_write_tokens;
}

function historyMessage(
  item: RuntimeHistoryMessage,
  request: RuntimeRequest,
  timestamp: number,
): Message {
  if (item.role === "user") {
    return { role: "user", content: item.text, timestamp };
  }
  return {
    role: "assistant",
    content: [{ type: "text", text: item.text }],
    api: request.provider.api,
    provider: request.provider.provider_id,
    model: request.provider.model,
    usage: EMPTY_USAGE,
    stopReason: "stop",
    timestamp,
  };
}

interface SeededPiSession {
  readonly manager: SessionManager;
  readonly entryMessageIds: Map<string, string>;
}

function seedPiSession(request: RuntimeRequest): SeededPiSession {
  const manager = SessionManager.inMemory("/tmp/lumen-agent-runtime", {
    id: request.agent_session_id,
  });
  const entryMessageIds = new Map<string, string>();
  const messageEntryIds = new Map<string, string>();
  const start = Date.now() - request.history.length * 2;
  const compaction = request.compaction ?? null;
  let compactionAppended = false;
  const appendCompaction = (): boolean => {
    if (compaction === null) return false;
    const firstKeptEntryId = messageEntryIds.get(
      compaction.first_kept_message_id,
    );
    if (firstKeptEntryId === undefined) {
      throw new Error("Pi compaction boundary is unavailable");
    }
    manager.appendCompaction(
      compaction.summary,
      firstKeptEntryId,
      compaction.tokens_before,
    );
    return true;
  };
  for (const [index, item] of request.history.entries()) {
    const messageId = item.message_id ?? `legacy-history-${String(index + 1)}`;
    if (compaction?.next_message_id === messageId) {
      compactionAppended = appendCompaction();
    }
    const entryId = manager.appendMessage(
      historyMessage(item, request, start + index),
    );
    entryMessageIds.set(entryId, messageId);
    messageEntryIds.set(messageId, entryId);
  }
  if (
    compaction !== null &&
    !compactionAppended &&
    compaction.next_message_id === request.user_message_id
  ) {
    compactionAppended = appendCompaction();
  }
  if (compaction !== null && !compactionAppended) {
    throw new Error("Pi compaction continuation is unavailable");
  }
  return { manager, entryMessageIds };
}

function currentImages(request: RuntimeRequest): ImageContent[] {
  return request.references.map((reference) => ({
    type: "image",
    mimeType: reference.mime_type,
    data: reference.data_base64,
  }));
}

function assistantMessage(message: AgentMessage): AssistantMessage | null {
  return message.role === "assistant" ? message : null;
}

function terminalErrorCode(message: AssistantMessage | null): string | null {
  if (message === null || message.stopReason === "stop" || message.stopReason === "toolUse") {
    return null;
  }
  if (message.stopReason === "aborted") return "agent_cancelled";
  if (message.stopReason === "length") return "agent_output_limit_reached";
  return "agent_provider_error";
}

function toolDetails(result: unknown): Record<string, unknown> {
  if (result === null || typeof result !== "object") return {};
  const details = (result as { details?: unknown }).details;
  return details !== null && typeof details === "object"
    ? (details as Record<string, unknown>)
    : {};
}

function finalOutcome(
  errorCode: string | null,
  state: ToolRuntimeState,
  signal: AbortSignal,
): ExecuteResult["outcome"] {
  if (signal.aborted) return "cancelled";
  if (errorCode === null && state.unknownResults === 0) return "succeeded";
  if (state.successfulCalls > 0 || state.unknownResults > 0) return "partial";
  return "failed";
}

async function emitOrThrow(
  writer: EventWriter,
  type: string,
  payload: Record<string, unknown> = {},
): Promise<void> {
  if (!(await writer.emit(type, payload))) {
    throw new Error("Agent Runtime output limit reached");
  }
}

function buildToolState(): ToolRuntimeState {
  return {
    ordinals: new Map(),
    errors: new Map(),
    modes: new Map(),
    nextOrdinal: 0,
    calls: 0,
    imageCalls: 0,
    acceptedImages: 0,
    successfulCalls: 0,
    failedCalls: 0,
    unknownResults: 0,
    lastErrorCode: null,
    limitReason: null,
  };
}

export async function executeAgentRun(
  request: RuntimeRequest,
  writer: EventWriter,
  signal: AbortSignal,
  metrics?: RuntimeMetrics,
  dependencies: RuntimeDependencies = DEFAULT_DEPENDENCIES,
): Promise<ExecuteResult> {
  const usage = zeroUsage();
  const tools = buildToolState();
  let turnCount = 0;
  let outputChars = 0;
  let lastAssistant: AssistantMessage | null = null;
  let providerDispatches = 0;
  let providerResponses = 0;
  let providerCompletions = 0;
  let closingTurn = false;
  const toolStartedAt = new Map<string, bigint>();

  const onDispatch = async (): Promise<void> => {
    providerDispatches += 1;
    metrics?.providerRequests.labels("dispatch", "sent").inc();
    await emitOrThrow(writer, "provider.dispatched", {
      turn: providerDispatches,
    });
  };
  const prepared: PreparedProviderRuntime = await dependencies.prepareProvider(
    request,
    onDispatch,
  );
  const gateway = dependencies.createGateway(request);
  const nativeCompactionEnabled =
    request.compaction !== undefined &&
    request.history.every((message) => message.message_id !== undefined);
  const compactionReserveTokens = Math.min(
    16_384,
    Math.max(1_024, Math.floor(prepared.model.contextWindow / 4)),
  );
  const compactionKeepRecentTokens = Math.min(
    20_000,
    Math.max(
      1_024,
      Math.floor(
        (prepared.model.contextWindow - compactionReserveTokens) / 2,
      ),
    ),
  );
  const customTools =
    request.allowed_tools.length === 1
      ? [createImageTool(request, gateway, tools)]
      : [];
  const settings = SettingsManager.inMemory({
    compaction: {
      enabled: nativeCompactionEnabled,
      reserveTokens: compactionReserveTokens,
      keepRecentTokens: compactionKeepRecentTokens,
    },
    retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
    transport: "sse",
    defaultTools: [],
    images: { autoResize: false, blockImages: false },
    defaultProjectTrust: "never",
  });
  const seededSession = seedPiSession(request);
  const sessionManager = seededSession.manager;
  const { session } = await createAgentSession({
    cwd: "/tmp/lumen-agent-runtime",
    agentDir: "/tmp/lumen-agent-runtime-empty",
    model: prepared.model,
    thinkingLevel:
      request.provider.reasoning_supported && request.reasoning_effort !== null
        ? request.reasoning_effort
        : "off",
    modelRuntime: prepared.modelRuntime,
    resourceLoader: emptyResourceLoader(request.system_prompt),
    noTools: "builtin",
    tools: request.allowed_tools,
    customTools,
    sessionManager,
    settingsManager: settings,
  });

  const expectedTools = [...request.allowed_tools];
  const activeTools = session.getActiveToolNames();
  const allTools = session.getAllTools().map((tool) => tool.name);
  if (
    session.sessionFile !== undefined ||
    JSON.stringify(activeTools) !== JSON.stringify(expectedTools) ||
    JSON.stringify(allTools) !== JSON.stringify(expectedTools)
  ) {
    session.dispose();
    await prepared.close();
    throw new Error("Pi tool or session isolation check failed");
  }

  session.agent.toolExecution = "sequential";
  session.agent.transport = "sse";
  session.agent.streamFunction = (model, context, options) =>
    prepared.modelRuntime.streamSimple(model, context, {
      ...options,
      apiKey: request.provider.api_key,
      headers: request.provider.headers,
      fetch: prepared.transport.fetch,
      transport: "sse",
      maxRetries: 0,
      maxRetryDelayMs: 0,
      timeoutMs: request.limits.run_timeout_seconds * 1000,
      maxTokens: Math.min(
        options?.maxTokens ?? request.limits.max_output_tokens,
        request.provider.max_output_tokens,
        request.limits.max_output_tokens,
      ),
      onResponse: async (response) => {
        providerResponses += 1;
        metrics?.providerRequests.labels("response", String(response.status)).inc();
        await emitOrThrow(writer, "provider.response", {
          turn: providerResponses,
          status: response.status,
        });
      },
    });

  const emitCompactionCheckpoint = async (
    result: CompactionResult,
    dispatchCountBefore: number,
    responseCountBefore: number,
  ): Promise<void> => {
    const providerCallCount = providerDispatches - dispatchCountBefore;
    const responseCount = providerResponses - responseCountBefore;
    const firstKeptMessageId = seededSession.entryMessageIds.get(
      result.firstKeptEntryId,
    );
    if (
      firstKeptMessageId === undefined ||
      result.usage === undefined ||
      providerCallCount < 1 ||
      providerCallCount > 2 ||
      responseCount !== providerCallCount ||
      Buffer.byteLength(result.summary, "utf8") > 48_000
    ) {
      throw new Error("Pi compaction checkpoint is incomplete");
    }
    const compactionUsage = boundedProviderUsage(
      result.usage,
      request,
      providerCallCount,
    );
    addUsage(usage, compactionUsage);
    providerCompletions += providerCallCount;
    await emitOrThrow(writer, "compaction.completed", {
      checkpoint_version: 1,
      pi_runtime_version: RUNTIME_VERSION,
      summary: result.summary,
      first_kept_message_id: firstKeptMessageId,
      tokens_before: result.tokensBefore,
      provider_call_count: providerCallCount,
      usage: compactionUsage,
    });
  };

  let runStarted = false;
  const emitRunStarted = async (): Promise<void> => {
    if (runStarted) return;
    runStarted = true;
    await emitOrThrow(writer, "run.started", {
      tools: expectedTools,
      runtime_version: RUNTIME_VERSION,
      reasoning_effort: session.thinkingLevel,
    });
  };

  const previousPrepare = session.agent.prepareNextTurnWithContext;
  session.agent.prepareNextTurnWithContext = async (context, turnSignal) => {
    const previous = await previousPrepare?.(context, turnSignal);
    const baseContext = previous?.context ?? context.context;
    const needsClosingTurn =
      context.toolResults.length > 0 &&
      (turnCount >= request.limits.max_turns - 1 || tools.limitReason !== null);
    if (!needsClosingTurn) return previous;
    closingTurn = true;
    return {
      ...previous,
      context: {
        ...baseContext,
        tools: [],
        systemPrompt: `${baseContext.systemPrompt}\n\nConclude now without calling any tool. Briefly report accepted asynchronous image jobs and any limitation.`,
      },
    };
  };
  session.agent.shouldStopAfterTurn = (context) => {
    if (turnCount >= request.limits.max_turns) return true;
    return closingTurn && context.toolResults.length === 0;
  };

  const unsubscribe = session.agent.subscribe(async (event: AgentEvent) => {
    if (signal.aborted) session.agent.abort();
    if (event.type === "agent_start") {
      await emitRunStarted();
      return;
    }
    if (
      event.type === "message_update" &&
      event.assistantMessageEvent.type === "text_delta"
    ) {
      const delta = event.assistantMessageEvent.delta;
      outputChars += delta.length;
      if (outputChars > request.limits.max_output_chars) {
        session.agent.abort();
        throw new Error("Agent Runtime text output limit reached");
      }
      for (let offset = 0; offset < delta.length; offset += 8192) {
        await emitOrThrow(writer, "text.delta", {
          delta: delta.slice(offset, offset + 8192),
        });
      }
      return;
    }
    if (event.type === "tool_execution_start") {
      toolStartedAt.set(event.toolCallId, process.hrtime.bigint());
      const ordinal = ordinalFor(tools, event.toolCallId);
      await emitOrThrow(writer, "tool.started", {
        tool_call_id: event.toolCallId,
        ordinal,
        name: event.toolName,
      });
      return;
    }
    if (event.type === "tool_execution_end") {
      const ordinal = ordinalFor(tools, event.toolCallId);
      const mode = tools.modes.get(event.toolCallId) ?? "unknown";
      const started = toolStartedAt.get(event.toolCallId);
      toolStartedAt.delete(event.toolCallId);
      const toolDuration =
        started === undefined
          ? null
          : Number(process.hrtime.bigint() - started) / 1_000_000_000;
      if (event.isError) {
        const recorded = tools.errors.get(event.toolCallId);
        const code =
          event.toolName === AGENT_TOOL_CREATE_IMAGE
            ? recorded?.code ?? "agent_tool_failed"
            : "agent_tool_not_allowed";
        metrics?.toolCalls.labels(event.toolName, mode, "failed").inc();
        if (toolDuration !== null) {
          metrics?.toolDuration.labels(event.toolName, mode, "failed").observe(toolDuration);
        }
        await emitOrThrow(writer, "tool.failed", {
          tool_call_id: event.toolCallId,
          ordinal,
          name: event.toolName,
          mode,
          error_code: code,
          result_unknown: recorded?.resultUnknown === true,
        });
      } else {
        const details = toolDetails(event.result);
        const resultMode = typeof details.mode === "string" ? details.mode : mode;
        metrics?.toolCalls.labels(event.toolName, resultMode, "succeeded").inc();
        if (toolDuration !== null) {
          metrics?.toolDuration.labels(event.toolName, resultMode, "succeeded").observe(toolDuration);
        }
        await emitOrThrow(writer, "tool.succeeded", {
          tool_call_id: event.toolCallId,
          ordinal,
          name: event.toolName,
          mode: details.mode,
          generation_ids: details.generation_ids,
          replayed: details.replayed,
        });
      }
      return;
    }
    if (event.type === "turn_end") {
      turnCount += 1;
      const message = assistantMessage(event.message);
      if (message !== null) {
        lastAssistant = message;
      }
      const turnUsage = message === null
        ? zeroUsage()
        : boundedTurnUsage(message.usage, request);
      addUsage(usage, turnUsage);
      if (
        message !== null &&
        message.stopReason !== "error" &&
        message.stopReason !== "aborted" &&
        turnUsage.total_tokens > 0
      ) {
        providerCompletions += 1;
      }
      await emitOrThrow(writer, "turn.completed", {
        turn: turnCount,
        usage: turnUsage,
        stop_reason: message?.stopReason ?? "error",
      });
    }
  });

  const abortListener = (): void => {
    session.abortCompaction();
    session.agent.abort();
  };
  signal.addEventListener("abort", abortListener, { once: true });
  try {
    await emitRunStarted();
    const images = currentImages(request);
    const estimateCompleteContext = (): number => [
      ...session.messages,
      {
        role: "user" as const,
        content: [
          { type: "text" as const, text: request.system_prompt },
          { type: "text" as const, text: request.current_prompt },
          ...images,
        ],
        timestamp: Date.now(),
      },
    ].reduce((total, message) => total + estimateTokens(message), 0);
    const compactionSettings = settings.getCompactionSettings();
    if (
      nativeCompactionEnabled &&
      shouldCompact(
        estimateCompleteContext(),
        prepared.model.contextWindow,
        compactionSettings,
      )
    ) {
      const dispatchCountBefore = providerDispatches;
      const responseCountBefore = providerResponses;
      let compacted = false;
      try {
        const result = await session.compact();
        await emitCompactionCheckpoint(
          result,
          dispatchCountBefore,
          responseCountBefore,
        );
        compacted = true;
      } catch (error) {
        if (
          !(error instanceof Error) ||
          !/Nothing to compact|Already compacted/u.test(error.message)
        ) {
          throw error;
        }
      }
      if (
        !compacted ||
        shouldCompact(
          estimateCompleteContext(),
          prepared.model.contextWindow,
          compactionSettings,
        )
      ) {
        throw new Error("Pi native compaction could not fit the Agent context");
      }
    }
    session.setAutoCompactionEnabled(false);
    await session.prompt(request.current_prompt, {
      images,
      expandPromptTemplates: false,
    });
    if (tools.limitReason !== null) {
      metrics?.limits.labels(tools.limitReason).inc();
      await emitOrThrow(writer, "limit.reached", { reason: tools.limitReason });
    } else if (turnCount >= request.limits.max_turns) {
      metrics?.limits.labels("turns").inc();
      await emitOrThrow(writer, "limit.reached", { reason: "turns" });
    }
    const errorCode =
      tools.unknownResults > 0
        ? "agent_tool_result_unknown"
        : tools.failedCalls > 0
          ? tools.lastErrorCode ?? "agent_tool_failed"
          : terminalErrorCode(lastAssistant);
    return {
      outcome: finalOutcome(errorCode, tools, signal),
      errorCode,
      usage,
      turnCount,
      toolCallCount: tools.calls,
      providerDispatchCount: providerDispatches,
      providerCompletedCount: providerCompletions,
    };
  } catch (error) {
    const errorCode = signal.aborted
      ? "agent_cancelled"
      : tools.unknownResults > 0
        ? "agent_tool_result_unknown"
        : tools.failedCalls > 0
          ? tools.lastErrorCode ?? "agent_tool_failed"
          : error instanceof Error && /output limit/iu.test(error.message)
          ? "agent_output_limit_reached"
          : terminalErrorCode(lastAssistant) ?? "agent_runtime_error";
    throw new RuntimeExecutionError(
      {
        outcome: finalOutcome(errorCode, tools, signal),
        errorCode,
        usage,
        turnCount,
        toolCallCount: tools.calls,
        providerDispatchCount: providerDispatches,
        providerCompletedCount: providerCompletions,
      },
      error,
    );
  } finally {
    signal.removeEventListener("abort", abortListener);
    unsubscribe();
    session.dispose();
    await prepared.close();
  }
}

export async function verifyPiIsolation(): Promise<void> {
  const faux = fauxProvider({ provider: "lumen-readiness" });
  const modelRuntime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsPath: null,
    modelsStore: new InMemoryModelsStore(),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
  modelRuntime.registerNativeProvider(faux.provider);
  const model = faux.getModel();
  const settings = SettingsManager.inMemory({
    compaction: { enabled: false },
    retry: { enabled: false, provider: { maxRetries: 0 } },
    defaultTools: [],
    defaultProjectTrust: "never",
  });
  const { session } = await createAgentSession({
    model,
    modelRuntime,
    noTools: "builtin",
    tools: [],
    customTools: [],
    resourceLoader: emptyResourceLoader("Lumen readiness check"),
    sessionManager: SessionManager.inMemory("/tmp/lumen-agent-runtime-ready"),
    settingsManager: settings,
  });
  try {
    if (
      session.sessionFile !== undefined ||
      session.getActiveToolNames().length !== 0 ||
      session.getAllTools().length !== 0
    ) {
      throw new Error("Pi isolation readiness check failed");
    }
  } finally {
    session.dispose();
  }
}
