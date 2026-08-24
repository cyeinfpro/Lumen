import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

import { RuntimeAuthError, RuntimeAuthenticator } from "./auth.js";
import {
  loadConfig,
  type RuntimeConfig,
  validateRuntimeConfig,
} from "./config.js";
import {
  parseRuntimeRequest,
  RUNTIME_HEARTBEAT_EVENT,
} from "./contracts.js";
import { healthPayload, readinessPayload, RuntimeReadiness } from "./health.js";
import { RuntimeMetrics } from "./metrics.js";
import { NdjsonEventWriter, type EventWriter } from "./ndjson.js";
import { logRuntime, safeErrorCode } from "./redaction.js";
import {
  executeAgentRun,
  RuntimeExecutionError,
  type RuntimeDependencies,
} from "./runtime.js";

const RUN_PATH = "/v1/runs";

class RequestError extends Error {
  constructor(
    readonly statusCode: number,
    readonly code: string,
  ) {
    super("Agent Runtime request rejected");
    this.name = "RequestError";
  }
}

async function readBody(
  request: IncomingMessage,
  maximum: number,
  timeoutMs: number,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let total = 0;
    let settled = false;
    const cleanup = (): void => {
      clearTimeout(timer);
      request.off("data", onData);
      request.off("end", onEnd);
      request.off("error", onError);
      request.off("aborted", onAborted);
    };
    const finish = (error?: Error): void => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(Buffer.concat(chunks, total));
    };
    const onData = (raw: Buffer | Uint8Array): void => {
      const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
      total += chunk.length;
      if (total > maximum) {
        finish(new RequestError(413, "agent_runtime_request_too_large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    };
    const onEnd = (): void => finish();
    const onError = (): void => finish(new RequestError(400, "agent_runtime_body_error"));
    const onAborted = (): void => finish(new RequestError(400, "agent_runtime_body_aborted"));
    const timer = setTimeout(() => {
      finish(new RequestError(408, "agent_runtime_body_timeout"));
      request.destroy();
    }, timeoutMs);
    timer.unref();
    request.on("data", onData);
    request.once("end", onEnd);
    request.once("error", onError);
    request.once("aborted", onAborted);
  });
}

function parseJsonBody(body: Buffer): unknown {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    throw new RequestError(400, "agent_runtime_invalid_json");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new RequestError(400, "agent_runtime_invalid_json");
  }
}

function writeJson(
  response: ServerResponse,
  statusCode: number,
  payload: Record<string, unknown>,
): void {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body, "utf8"),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function writeError(response: ServerResponse, statusCode: number, code: string): void {
  writeJson(response, statusCode, {
    error: { code: safeErrorCode(code), message: "Agent Runtime request failed" },
  });
}

async function waitForStop(stop: AbortSignal, timeoutMs: number): Promise<boolean> {
  if (stop.aborted) return false;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (elapsed: boolean): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stop.removeEventListener("abort", onAbort);
      resolve(elapsed);
    };
    const onAbort = (): void => finish(false);
    const timer = setTimeout(() => finish(true), timeoutMs);
    timer.unref();
    stop.addEventListener("abort", onAbort, { once: true });
  });
}

interface HeartbeatHandle {
  stop(): Promise<void>;
}

const NOOP_HEARTBEAT: HeartbeatHandle = {
  async stop(): Promise<void> {},
};

function startHeartbeat(
  writer: EventWriter,
  intervalMs: number,
  onFailure: () => void,
): HeartbeatHandle {
  const controller = new AbortController();
  const task = (async (): Promise<void> => {
    try {
      while (await waitForStop(controller.signal, intervalMs)) {
        if (await writer.emit(RUNTIME_HEARTBEAT_EVENT)) continue;
        onFailure();
        return;
      }
    } catch {
      onFailure();
    }
  })();
  return {
    async stop(): Promise<void> {
      controller.abort();
      await task;
    },
  };
}

async function emitTerminal(
  writer: EventWriter,
  response: ServerResponse,
  type: string,
  payload: Record<string, unknown>,
): Promise<boolean> {
  try {
    if (await writer.emit(type, payload, true)) return true;
  } catch {
    // The writer latches and destroys the response after transport failure.
  }
  if (!response.destroyed) response.destroy();
  return false;
}

function authStatus(error: RuntimeAuthError): number {
  return error.code === "agent_runtime_auth_unconfigured" ||
    error.code === "agent_runtime_auth_capacity_exhausted"
    ? 503
    : 401;
}

interface ServerOptions {
  readonly config?: RuntimeConfig;
  readonly metrics?: RuntimeMetrics;
  readonly dependencies?: RuntimeDependencies;
}

export function createRuntimeServer(options: ServerOptions = {}): {
  server: Server;
  readiness: RuntimeReadiness;
  metrics: RuntimeMetrics;
} {
  const config = validateRuntimeConfig(options.config ?? loadConfig());
  const metrics = options.metrics ?? new RuntimeMetrics();
  const readiness = new RuntimeReadiness(config);
  const authenticator = new RuntimeAuthenticator(
    config.sharedSecret,
    config.nonceTtlSeconds,
    config.nonceCacheSize,
    config.authClockSkewSeconds,
  );
  let admittedRequests = 0;

  const server = createServer(async (request, response) => {
    const path = request.url ?? "/";
    if (request.method === "GET" && path === "/healthz") {
      writeJson(response, 200, healthPayload());
      return;
    }
    if (request.method === "GET" && path === "/readyz") {
      if (readiness.state.checkedAt === null) await readiness.check();
      writeJson(
        response,
        readiness.state.ready ? 200 : 503,
        readinessPayload(readiness.state, config.sharedSecret),
      );
      return;
    }
    if (request.method === "GET" && path === "/metrics") {
      response.writeHead(200, {
        "content-type": metrics.registry.contentType,
        "cache-control": "no-store",
      });
      response.end(await metrics.registry.metrics());
      return;
    }
    if (request.method !== "POST" || path !== RUN_PATH) {
      writeError(response, 404, "agent_runtime_not_found");
      return;
    }
    if (!readiness.state.ready) {
      writeError(response, 503, "agent_runtime_not_ready");
      return;
    }
    if (admittedRequests >= config.maxConcurrentRuns) {
      metrics.requests.labels("capacity_rejected").inc();
      writeError(response, 503, "agent_runtime_capacity_exhausted");
      return;
    }
    const contentType = request.headers["content-type"]?.split(";", 1)[0]?.trim().toLowerCase();
    if (contentType !== "application/json") {
      writeError(response, 415, "agent_runtime_content_type_required");
      return;
    }
    admittedRequests += 1;

    let rawBody: Buffer;
    try {
      rawBody = await readBody(
        request,
        config.maxRequestBytes,
        config.requestBodyTimeoutSeconds * 1000,
      );
      authenticator.verify("POST", RUN_PATH, request.headers, rawBody);
    } catch (error) {
      if (error instanceof RuntimeAuthError) {
        writeError(response, authStatus(error), error.code);
      } else if (error instanceof RequestError) {
        writeError(response, error.statusCode, error.code);
      } else {
        writeError(response, 400, "agent_runtime_invalid_request");
      }
      admittedRequests -= 1;
      return;
    }

    let runRequest;
    try {
      runRequest = parseRuntimeRequest(parseJsonBody(rawBody));
    } catch {
      writeError(response, 422, "agent_runtime_invalid_request");
      admittedRequests -= 1;
      return;
    }

    metrics.activeRuns.inc();
    const startedAt = process.hrtime.bigint();
    const abortController = new AbortController();
    let completed = false;
    const abortOnDisconnect = (): void => {
      if (!completed) abortController.abort();
    };
    request.once("aborted", abortOnDisconnect);
    response.once("close", abortOnDisconnect);
    response.writeHead(200, {
      "content-type": "application/x-ndjson; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-accel-buffering": "no",
      connection: "keep-alive",
    });
    response.flushHeaders();
    const writer = new NdjsonEventWriter(
      response,
      runRequest.run_id,
      runRequest.execution_epoch,
      config.maxLineBytes,
    );
    let outcome = "failed";
    const runtimeState: { streamFailed: boolean } = { streamFailed: false };
    const heartbeat = runRequest.event_features?.includes("heartbeat-v1")
      ? startHeartbeat(
          writer,
          config.heartbeatIntervalSeconds * 1000,
          () => {
            runtimeState.streamFailed = true;
            abortController.abort();
          },
        )
      : NOOP_HEARTBEAT;
    try {
      const result = await executeAgentRun(
        runRequest,
        writer,
        abortController.signal,
        metrics,
        options.dependencies,
      );
      await heartbeat.stop();
      const effectiveOutcome = runtimeState.streamFailed
        ? "failed"
        : result.outcome;
      const effectiveErrorCode = runtimeState.streamFailed
        ? "agent_runtime_error"
        : result.errorCode;
      outcome = effectiveOutcome;
      const eventType =
        effectiveOutcome === "cancelled"
          ? "run.cancelled"
          : effectiveOutcome === "failed"
            ? "run.failed"
            : "run.completed";
      const terminalWritten = await emitTerminal(writer, response, eventType, {
        status: effectiveOutcome,
        error_code: effectiveErrorCode,
        usage: result.usage,
        turn_count: result.turnCount,
        tool_call_count: result.toolCallCount,
        provider_dispatch_count: result.providerDispatchCount,
        provider_completed_count: result.providerCompletedCount,
      });
      if (!terminalWritten) outcome = "failed";
    } catch (error) {
      await heartbeat.stop();
      const result = error instanceof RuntimeExecutionError ? error.result : null;
      outcome = runtimeState.streamFailed
        ? "failed"
        : result?.outcome ?? (abortController.signal.aborted ? "cancelled" : "failed");
      const type = outcome === "cancelled"
        ? "run.cancelled"
        : outcome === "partial"
          ? "run.completed"
          : "run.failed";
      const terminalWritten = await emitTerminal(writer, response, type, {
        status: outcome,
        error_code: runtimeState.streamFailed
          ? "agent_runtime_error"
          : result?.errorCode ?? (
            outcome === "cancelled" ? "agent_cancelled" : "agent_runtime_error"
          ),
        usage: result?.usage ?? {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_write_1h_tokens: 0,
          reasoning_tokens: 0,
          total_tokens: 0,
        },
        turn_count: result?.turnCount ?? 0,
        tool_call_count: result?.toolCallCount ?? 0,
        provider_dispatch_count: result?.providerDispatchCount ?? 0,
        provider_completed_count: result?.providerCompletedCount ?? 0,
      });
      if (!terminalWritten) outcome = "failed";
      logRuntime("warn", "agent_runtime.run_failed", {
        run_id: runRequest.run_id,
        execution_epoch: runRequest.execution_epoch,
        trace_id: runRequest.trace_id,
        error_type: error instanceof Error ? error.name : "Error",
      });
    } finally {
      await heartbeat.stop();
      completed = true;
      request.off("aborted", abortOnDisconnect);
      response.off("close", abortOnDisconnect);
      if (!response.destroyed) response.end();
      admittedRequests -= 1;
      metrics.activeRuns.dec();
      metrics.requests.labels(outcome).inc();
      const duration = Number(process.hrtime.bigint() - startedAt) / 1_000_000_000;
      metrics.duration.labels(outcome).observe(duration);
    }
  });

  server.requestTimeout = config.requestBodyTimeoutSeconds * 1000;
  server.headersTimeout = 15_000;
  server.keepAliveTimeout = 5_000;
  server.maxRequestsPerSocket = 100;
  return { server, readiness, metrics };
}

async function main(): Promise<void> {
  const config = loadConfig();
  const { server, readiness } = createRuntimeServer({ config });
  await readiness.check();
  server.listen(config.port, config.host, () => {
    logRuntime("info", "agent_runtime.started", {
      host: config.host,
      port: config.port,
      ready: readiness.state.ready,
    });
  });
  const shutdown = (): void => {
    server.close((error) => {
      if (error) {
        logRuntime("error", "agent_runtime.shutdown_failed", {
          error_type: error.name,
        });
        process.exitCode = 1;
      }
    });
  };
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
}

if (import.meta.url === new URL(process.argv[1] ?? "", "file:").href) {
  void main().catch((error: unknown) => {
    logRuntime("error", "agent_runtime.startup_failed", {
      error_type: error instanceof Error ? error.name : "Error",
    });
    process.exitCode = 1;
  });
}
