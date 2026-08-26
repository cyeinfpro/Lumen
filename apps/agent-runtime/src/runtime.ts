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
  type AgentSessionEvent,
  type CompactionResult,
  estimateTokens,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  shouldCompact,
} from "@earendil-works/pi-coding-agent";

import {
  AGENT_TOOL_CREATE_IMAGE,
  RUNTIME_TEXT_RESET_EVENT,
  runtimeToolPolicy,
  type RuntimeHistoryMessage,
  type RuntimeRequest,
  type RuntimeUsage,
} from "./contracts.js";
import { RUNTIME_VERSION } from "./config.js";
import type { RuntimeMetrics } from "./metrics.js";
import type { EventWriter } from "./ndjson.js";
import { logRuntime } from "./redaction.js";
import {
  authorizeProviderDispatch,
  ProviderDispatchPermitError,
} from "./providers/dispatch-permit.js";
import {
  prepareProviderRuntime,
  type PreparedProviderRuntime,
} from "./providers/runtime-provider.js";
import { emptyResourceLoader } from "./resource-loader.js";
import { createImageTool, ordinalFor, type ToolRuntimeState } from "./tools/create-image.js";
import {
  createImageGateway,
  type CreateImageGateway,
  type GatewayTransportPolicy,
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
    onDispatch: (signal?: AbortSignal) => Promise<void>,
  ): Promise<PreparedProviderRuntime>;
  createGateway(
    request: RuntimeRequest,
    policy?: GatewayTransportPolicy,
  ): CreateImageGateway;
  readonly gatewayPolicy?: GatewayTransportPolicy;
  readonly safetyPolicy?: RuntimeSafetyPolicy;
  authorizeProviderDispatch?(
    request: RuntimeRequest,
    ordinal: number,
    signal?: AbortSignal,
  ): Promise<void>;
}

export interface RuntimeSafetyPolicy {
  readonly maxWallClockMs: number;
  readonly maxProviderDispatches: number;
  readonly maxTurns: number;
  readonly maxTotalTokens: number;
  readonly maxEventBytes: number;
  readonly maxRepeatedToolCalls: number;
}

export const DEFAULT_RUNTIME_SAFETY_POLICY: RuntimeSafetyPolicy = {
  maxWallClockMs: 6 * 60 * 60 * 1000,
  maxProviderDispatches: 128,
  maxTurns: 128,
  maxTotalTokens: 4_000_000,
  maxEventBytes: 16 * 1024 * 1024,
  maxRepeatedToolCalls: 8,
};

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
  authorizeProviderDispatch,
};

class RuntimeSafetyError extends Error {
  constructor(readonly reason: string) {
    super("Agent Runtime safety budget reached");
    this.name = "RuntimeSafetyError";
  }
}

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
    request.provider.max_output_tokens * providerCallCount,
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

function historyMessages(
  item: RuntimeHistoryMessage,
  request: RuntimeRequest,
  timestamp: number,
): Message[] {
  if (item.role === "user") {
    return [{
      role: "user",
      content: [
        { type: "text", text: item.text },
        ...(item.images ?? []).map((image): ImageContent => ({
          type: "image",
          mimeType: image.mime_type,
          data: image.data_base64,
        })),
      ],
      timestamp,
    }];
  }
  const toolCalls = (item.tool_calls ?? []).map((toolCall) => ({
    type: "toolCall" as const,
    id: toolCall.id,
    name: toolCall.name,
    arguments: toolCall.arguments,
  }));
  const assistant: AssistantMessage = {
    role: "assistant",
    content: [{ type: "text", text: item.text }, ...toolCalls],
    api: item.api ?? request.provider.api,
    provider: item.provider_id ?? request.provider.provider_id,
    model: item.model ?? request.provider.model,
    usage: EMPTY_USAGE,
    stopReason: item.stop_reason ?? (toolCalls.length > 0 ? "toolUse" : "stop"),
    timestamp,
  };
  return [
    assistant,
    ...(item.tool_results ?? []).map((result, index): Message => ({
      role: "toolResult",
      toolCallId: result.tool_call_id,
      toolName: result.name,
      content: [{ type: "text", text: result.text }],
      isError: result.is_error,
      timestamp: timestamp + index + 1,
    })),
    ...(item.final_text
      ? [
          {
            role: "assistant" as const,
            content: [{ type: "text" as const, text: item.final_text }],
            api: item.api ?? request.provider.api,
            provider: item.provider_id ?? request.provider.provider_id,
            model: item.model ?? request.provider.model,
            usage: EMPTY_USAGE,
            stopReason: "stop" as const,
            timestamp: timestamp + (item.tool_results?.length ?? 0) + 1,
          },
        ]
      : []),
  ];
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
    for (const message of historyMessages(item, request, start + index * 10)) {
      const entryId = manager.appendMessage(message);
      entryMessageIds.set(entryId, messageId);
      if (!messageEntryIds.has(messageId)) {
        messageEntryIds.set(messageId, entryId);
      }
    }
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
  if (message.stopReason === "length") return "agent_output_truncated";
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
  hasProgress: boolean,
): ExecuteResult["outcome"] {
  if (signal.aborted && signal.reason !== "agent_runtime_shutdown") return "cancelled";
  if (errorCode === null && state.unknownResults === 0) return "succeeded";
  if (errorCode === "agent_runtime_shutdown") return "failed";
  if (hasProgress || state.successfulCalls > 0 || state.unknownResults > 0) return "partial";
  return "failed";
}

function stableToolSignature(name: string, value: unknown): string {
  const canonical = (item: unknown): string => {
    if (Array.isArray(item)) return `[${item.map(canonical).join(",")}]`;
    if (item !== null && typeof item === "object") {
      const record = item as Record<string, unknown>;
      return `{${Object.keys(record)
        .sort()
        .map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`)
        .join(",")}}`;
    }
    if (item === undefined) return "null";
    return JSON.stringify(item);
  };
  return `${name}:${canonical(value)}`;
}

function anySignalAborted(...signals: Array<AbortSignal | undefined>): boolean {
  return signals.some((candidate) => candidate?.aborted === true);
}

async function emitOrThrow(
  writer: EventWriter,
  type: string,
  payload: Record<string, unknown> = {},
): Promise<void> {
  if (!(await writer.emit(type, payload))) {
    throw new Error("Agent Runtime event could not be emitted");
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
  const toolPolicy = runtimeToolPolicy(request);
  const configuredSafety = dependencies.safetyPolicy ?? DEFAULT_RUNTIME_SAFETY_POLICY;
  const safetyPolicy: RuntimeSafetyPolicy = {
    ...configuredSafety,
    maxProviderDispatches: Math.min(
      configuredSafety.maxProviderDispatches,
      request.safety_budget?.max_provider_dispatches ??
        configuredSafety.maxProviderDispatches,
    ),
  };
  let turnCount = 0;
  let lastAssistant: AssistantMessage | null = null;
  let providerDispatches = 0;
  let providerResponses = 0;
  let providerCompletions = 0;
  const dispatchFailure: { code: string | null } = { code: null };
  let retainedTextChars = 0;
  const safetyState: { reason: string | null } = { reason: null };
  let abortForSafety: (() => void) | null = null;
  let repeatedToolSignature: string | null = null;
  let repeatedToolCalls = 0;
  const signatureCountedToolCalls = new Set<string>();
  const toolStartedAt = new Map<string, bigint>();
  let sessionEventTail: Promise<void> = Promise.resolve();
  let sessionEventFailure: unknown = null;

  const enqueueSessionEvent = (work: () => Promise<void>): void => {
    sessionEventTail = sessionEventTail
      .then(work)
      .catch((error: unknown) => {
        sessionEventFailure = error;
      });
  };
  const drainSessionEvents = async (): Promise<void> => {
    await sessionEventTail;
    if (sessionEventFailure instanceof Error) throw sessionEventFailure;
    if (sessionEventFailure !== null) {
      throw new Error("Agent Runtime session event failed");
    }
  };

  const tripSafety = (reason: string): void => {
    safetyState.reason ??= reason;
    abortForSafety?.();
  };
  const emitRuntimeEvent = async (
    type: string,
    payload: Record<string, unknown> = {},
  ): Promise<void> => {
    if (writer.bytesWritten >= safetyPolicy.maxEventBytes) {
      tripSafety("event_bytes");
      throw new RuntimeSafetyError("event_bytes");
    }
    await emitOrThrow(writer, type, payload);
  };
  const emitTextReset = async (): Promise<void> => {
    retainedTextChars = 0;
    await emitRuntimeEvent(RUNTIME_TEXT_RESET_EVENT);
  };
  const countToolSignature = (name: string, args: unknown): boolean => {
    const signature = stableToolSignature(name, args);
    if (signature === repeatedToolSignature) repeatedToolCalls += 1;
    else {
      repeatedToolSignature = signature;
      repeatedToolCalls = 1;
    }
    if (repeatedToolCalls <= safetyPolicy.maxRepeatedToolCalls) return false;
    tripSafety("repeated_tool_call");
    return true;
  };

  const onDispatch = async (providerSignal?: AbortSignal): Promise<void> => {
    await drainSessionEvents();
    if (anySignalAborted(signal, providerSignal)) {
      dispatchFailure.code = "agent_cancelled";
      throw new ProviderDispatchPermitError("agent_cancelled");
    }
    if (providerDispatches >= safetyPolicy.maxProviderDispatches) {
      tripSafety("provider_dispatches");
      throw new RuntimeSafetyError("provider_dispatches");
    }
    const dispatchSignal = providerSignal
      ? AbortSignal.any([signal, providerSignal])
      : signal;
    try {
      await (dependencies.authorizeProviderDispatch ?? authorizeProviderDispatch)(
        request,
        providerDispatches + 1,
        dispatchSignal,
      );
    } catch (error) {
      dispatchFailure.code =
        error instanceof ProviderDispatchPermitError
          ? error.code
          : "agent_provider_dispatch_denied";
      if (dispatchFailure.code === "agent_safety_budget_reached") {
        tripSafety("provider_dispatches");
      }
      throw error;
    }
    if (anySignalAborted(signal, providerSignal)) {
      dispatchFailure.code = "agent_cancelled";
      throw new ProviderDispatchPermitError("agent_cancelled");
    }
    providerDispatches += 1;
    metrics?.providerRequests.labels("dispatch", "sent").inc();
    await emitRuntimeEvent("provider.dispatched", {
      turn: providerDispatches,
    });
  };
  const prepared: PreparedProviderRuntime = await dependencies.prepareProvider(
    request,
    onDispatch,
  );
  let pendingSessionCleanup: (() => Promise<void>) | null = null;
  try {
  const gateway = dependencies.createGateway(request, dependencies.gatewayPolicy);
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
    retry: {
      enabled: true,
      maxRetries: 3,
      baseDelayMs: 2_000,
      provider: { maxRetries: 0, maxRetryDelayMs: 60_000 },
    },
    httpIdleTimeoutMs: 0,
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
    ...(request.provider.reasoning_supported && request.reasoning_effort !== null
      ? { thinkingLevel: request.reasoning_effort }
      : {}),
    modelRuntime: prepared.modelRuntime,
    resourceLoader: emptyResourceLoader(request.system_prompt),
    noTools: "builtin",
    tools: request.allowed_tools,
    customTools,
    sessionManager,
    settingsManager: settings,
  });
  pendingSessionCleanup = async () => {
    const [result] = await Promise.allSettled([
      Promise.resolve().then(() => session.dispose()),
    ]);
    if (result.status === "rejected") {
      metrics?.cleanupFailures.labels("session").inc();
      logRuntime("warn", "agent_runtime.cleanup_failed", {
        run_id: request.run_id,
        resource: "session",
        error_type: result.reason instanceof Error ? result.reason.name : "Error",
      });
    }
  };
  abortForSafety = () => {
    session.abortCompaction();
    session.agent.abort();
  };

  const expectedTools = [...request.allowed_tools];
  const activeTools = session.getActiveToolNames();
  const allTools = session.getAllTools().map((tool) => tool.name);
  if (
    session.sessionFile !== undefined ||
    JSON.stringify(activeTools) !== JSON.stringify(expectedTools) ||
    JSON.stringify(allTools) !== JSON.stringify(expectedTools)
  ) {
    throw new Error("Pi tool or session isolation check failed");
  }

  session.agent.toolExecution = "sequential";
  session.agent.transport = "sse";
  const httpIdleTimeoutMs = settings.getHttpIdleTimeoutMs();
  const providerTimeoutMs = httpIdleTimeoutMs === 0
    ? 2_147_483_647
    : httpIdleTimeoutMs;
  session.agent.streamFunction = (model, context, options) =>
    prepared.modelRuntime.streamSimple(model, context, {
      ...options,
      apiKey: request.provider.api_key,
      headers: request.provider.headers,
      fetch: prepared.transport.fetch,
      transport: "sse",
      timeoutMs: providerTimeoutMs,
      maxRetries: 0,
      onResponse: async (response) => {
        providerResponses += 1;
        metrics?.providerRequests.labels("response", String(response.status)).inc();
        await emitRuntimeEvent("provider.response", {
          turn: providerResponses,
          status: response.status,
        });
      },
    });

  const previousBeforeToolCall = session.agent.beforeToolCall;
  session.agent.beforeToolCall = async (context, toolSignal) => {
    const previous = await previousBeforeToolCall?.(context, toolSignal);
    if (previous?.block) return previous;
    signatureCountedToolCalls.add(context.toolCall.id);
    if (!countToolSignature(context.toolCall.name, context.args)) return previous;
    return {
      block: true,
      reason: "Agent safety budget reached",
      terminate: true,
    };
  };
  session.agent.shouldStopAfterTurn = () => {
    if (turnCount >= safetyPolicy.maxTurns) tripSafety("turns");
    if (usage.total_tokens >= safetyPolicy.maxTotalTokens) tripSafety("tokens");
    return safetyState.reason !== null;
  };
  const safetyTimer = setTimeout(() => {
    tripSafety("wall_clock");
  }, safetyPolicy.maxWallClockMs);
  safetyTimer.unref();

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
    await emitRuntimeEvent("compaction.completed", {
      checkpoint_version: request.version === 3 ? 2 : 1,
      pi_runtime_version: RUNTIME_VERSION,
      summary: result.summary,
      first_kept_message_id: firstKeptMessageId,
      ...(request.version === 3
        ? {
            next_message_id: request.user_message_id,
            phase: "pre_prompt",
          }
        : {}),
      tokens_before: result.tokensBefore,
      provider_call_count: providerCallCount,
      usage: compactionUsage,
    });
  };

  let runStarted = false;
  const emitRunStarted = async (): Promise<void> => {
    if (runStarted) return;
    runStarted = true;
    await emitRuntimeEvent("run.started", {
      tools: expectedTools,
      runtime_version: RUNTIME_VERSION,
      reasoning_effort: session.thinkingLevel,
    });
  };

  const previousPrepare = session.agent.prepareNextTurnWithContext;
  session.agent.prepareNextTurnWithContext = async (context, turnSignal) => {
    const previous = await previousPrepare?.(context, turnSignal);
    const baseContext = previous?.context ?? context.context;
    const toolCapacityExhausted =
      context.toolResults.length > 0 &&
      (tools.limitReason !== null ||
        tools.imageCalls >= toolPolicy.max_image_tool_calls ||
        tools.acceptedImages >= toolPolicy.max_images_per_run);
    if (!toolCapacityExhausted) return previous;
    tools.limitReason ??=
      tools.imageCalls >= toolPolicy.max_image_tool_calls
        ? "tool_calls"
        : "images";
    return {
      ...previous,
      context: {
        ...baseContext,
        tools: [],
        systemPrompt: `${baseContext.systemPrompt}\n\nNo image tool is available for the remainder of this prompt. Conclude naturally and briefly report any accepted asynchronous image jobs.`,
      },
    };
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
      for (let offset = 0; offset < delta.length; offset += 8192) {
        retainedTextChars += delta.slice(offset, offset + 8192).length;
        await emitRuntimeEvent("text.delta", {
          delta: delta.slice(offset, offset + 8192),
        });
      }
      return;
    }
    if (event.type === "tool_execution_start") {
      if (!signatureCountedToolCalls.delete(event.toolCallId)) {
        countToolSignature(event.toolName, event.args);
      }
      toolStartedAt.set(event.toolCallId, process.hrtime.bigint());
      const ordinal = ordinalFor(tools, event.toolCallId);
      await emitRuntimeEvent("tool.started", {
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
        if (
          event.toolName === AGENT_TOOL_CREATE_IMAGE &&
          !tools.errors.has(event.toolCallId)
        ) {
          const fallbackCode =
            tools.imageCalls >= toolPolicy.max_image_tool_calls
              ? "agent_tool_limit_reached"
              : tools.acceptedImages >= toolPolicy.max_images_per_run
                ? "agent_image_limit_reached"
                : "agent_tool_failed";
          if (fallbackCode === "agent_tool_limit_reached") {
            tools.limitReason = "tool_calls";
          } else if (fallbackCode === "agent_image_limit_reached") {
            tools.limitReason = "images";
          }
          tools.calls += 1;
          tools.failedCalls += 1;
          tools.lastErrorCode = fallbackCode;
          tools.errors.set(event.toolCallId, {
            code: fallbackCode,
            resultUnknown: false,
          });
        }
        const recorded = tools.errors.get(event.toolCallId);
        const code =
          event.toolName === AGENT_TOOL_CREATE_IMAGE
            ? recorded?.code ?? "agent_tool_failed"
            : "agent_tool_not_allowed";
        metrics?.toolCalls.labels(event.toolName, mode, "failed").inc();
        if (toolDuration !== null) {
          metrics?.toolDuration.labels(event.toolName, mode, "failed").observe(toolDuration);
        }
        await emitRuntimeEvent("tool.failed", {
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
        await emitRuntimeEvent("tool.succeeded", {
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
      await emitRuntimeEvent("turn.completed", {
        turn: turnCount,
        usage: turnUsage,
        stop_reason: message?.stopReason ?? "error",
      });
    }
  });

  let compactionStart:
    | { providerDispatches: number; providerResponses: number }
    | null = null;
  const unsubscribeSession = session.subscribe((event: AgentSessionEvent) => {
    if (
      event.type === "auto_retry_start" &&
      request.event_features?.includes("text-reset-v1")
    ) {
      enqueueSessionEvent(async () => {
        await emitTextReset();
      });
      return;
    }
    if (event.type === "compaction_start" && event.reason !== "manual") {
      compactionStart = { providerDispatches, providerResponses };
      return;
    }
    if (
      event.type !== "compaction_end" ||
      event.reason === "manual" ||
      event.result === undefined
    ) {
      return;
    }
    const start = compactionStart ?? { providerDispatches, providerResponses };
    compactionStart = null;
    enqueueSessionEvent(async () => {
      await emitCompactionCheckpoint(
        event.result as CompactionResult,
        start.providerDispatches,
        start.providerResponses,
      );
      if (
        event.willRetry &&
        request.event_features?.includes("text-reset-v1")
      ) {
        await emitTextReset();
      }
    });
  });

  const abortListener = (): void => {
    session.abortCompaction();
    session.agent.abort();
  };
  pendingSessionCleanup = async () => {
    clearTimeout(safetyTimer);
    signal.removeEventListener("abort", abortListener);
    const cleanup = await Promise.allSettled([
      Promise.resolve().then(() => unsubscribe()),
      Promise.resolve().then(() => unsubscribeSession()),
      Promise.resolve().then(() => session.dispose()),
    ]);
    cleanup.forEach((result, index) => {
      if (result.status !== "rejected") return;
      const resource = ["agent_listener", "session_listener", "session"][index];
      metrics?.cleanupFailures.labels(resource ?? "unknown").inc();
      logRuntime("warn", "agent_runtime.cleanup_failed", {
        run_id: request.run_id,
        resource,
        error_type:
          result.reason instanceof Error ? result.reason.name : "Error",
      });
    });
  };
  signal.addEventListener("abort", abortListener, { once: true });
  try {
    await emitRunStarted();
    const images = currentImages(request);
    const estimateCompleteContext = (): number => {
      const historyTokens = session.messages.reduce(
        (total, message) => total + estimateTokens(message),
        0,
      );
      const promptTokens = estimateTokens({
        role: "user" as const,
        content: [
          { type: "text" as const, text: request.system_prompt },
          { type: "text" as const, text: request.current_prompt },
        ],
        timestamp: Date.now(),
      });
      const imageTokens = request.references.reduce(
        (total, reference, index) =>
          total +
          (reference.estimated_input_tokens ??
            estimateTokens({
              role: "user",
              content: [images[index] as ImageContent],
              timestamp: Date.now(),
            })),
        0,
      );
      return historyTokens + promptTokens + imageTokens;
    };
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
    // Only explicit pre-prompt compaction is persisted. Automatic threshold and
    // overflow checkpoints can retain current-turn Pi entries that do not yet
    // have a durable Lumen message boundary.
    session.setAutoCompactionEnabled(false);
    if (request.version === 3 && request.operation === "continue") {
      await session.prompt(request.current_prompt, {
        images: [],
        expandPromptTemplates: false,
      });
    } else {
      await session.prompt(request.current_prompt, {
        images,
        expandPromptTemplates: false,
      });
    }
    await drainSessionEvents();
    if (tools.limitReason !== null) {
      metrics?.limits.labels(tools.limitReason).inc();
      await emitRuntimeEvent("limit.reached", { reason: tools.limitReason });
    }
    if (safetyState.reason !== null) {
      metrics?.limits.labels(safetyState.reason).inc();
      await emitRuntimeEvent("limit.reached", {
        reason: "agent_safety_budget_reached",
      });
    }
    const errorCode =
      signal.aborted
        ? signal.reason === "agent_runtime_shutdown"
          ? "agent_runtime_shutdown"
          : "agent_cancelled"
        : safetyState.reason !== null
          ? "agent_safety_budget_reached"
          : dispatchFailure.code !== null
            ? dispatchFailure.code
            : tools.unknownResults > 0
        ? "agent_tool_result_unknown"
        : tools.failedCalls > 0
          ? tools.lastErrorCode ?? "agent_tool_failed"
          : terminalErrorCode(lastAssistant);
    const hasProgress = retainedTextChars > 0 || tools.successfulCalls > 0;
    return {
      outcome: finalOutcome(errorCode, tools, signal, hasProgress),
      errorCode,
      usage,
      turnCount,
      toolCallCount: tools.calls,
      providerDispatchCount: providerDispatches,
      providerCompletedCount: providerCompletions,
    };
  } catch (error) {
    const errorCode = signal.aborted
      ? signal.reason === "agent_runtime_shutdown"
        ? "agent_runtime_shutdown"
        : "agent_cancelled"
      : safetyState.reason !== null || error instanceof RuntimeSafetyError
        ? "agent_safety_budget_reached"
        : dispatchFailure.code !== null
          ? dispatchFailure.code
          : tools.unknownResults > 0
          ? "agent_tool_result_unknown"
          : tools.failedCalls > 0
            ? tools.lastErrorCode ?? "agent_tool_failed"
            : terminalErrorCode(lastAssistant) ?? "agent_runtime_error";
    const hasProgress = retainedTextChars > 0 || tools.successfulCalls > 0;
    throw new RuntimeExecutionError(
      {
        outcome: finalOutcome(errorCode, tools, signal, hasProgress),
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
    await pendingSessionCleanup();
    pendingSessionCleanup = null;
  }
  } finally {
    if (pendingSessionCleanup !== null) {
      await pendingSessionCleanup();
    }
    const [providerCleanup] = await Promise.allSettled([
      Promise.resolve().then(() => prepared.close()),
    ]);
    if (providerCleanup.status === "rejected") {
      metrics?.cleanupFailures.labels("provider").inc();
      logRuntime("warn", "agent_runtime.cleanup_failed", {
        run_id: request.run_id,
        resource: "provider",
        error_type:
          providerCleanup.reason instanceof Error
            ? providerCleanup.reason.name
            : "Error",
      });
    }
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
