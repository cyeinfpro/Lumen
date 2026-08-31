import { isIP } from "node:net";

const MIN_SHARED_SECRET_BYTES = 32;

const INTEGER_BOUNDS = {
  port: [1, 65_535],
  authClockSkewSeconds: [1, 300],
  nonceTtlSeconds: [30, 600],
  nonceCacheSize: [100, 100_000],
  maxRequestBytes: [64 * 1024, 64 * 1024 * 1024],
  maxLineBytes: [1024, 1024 * 1024],
  maxConcurrentRuns: [1, 128],
  maxConcurrentBodyReads: [1, 256],
  maxInflightRequestBytes: [64 * 1024, 512 * 1024 * 1024],
  requestBodyTimeoutSeconds: [1, 60],
  heartbeatIntervalSeconds: [1, 60],
  toolGatewayTimeoutSeconds: [1, 300],
  toolGatewayMaxResponseBytes: [1024, 1024 * 1024],
  maxRunSeconds: [60, 23 * 60 * 60],
  maxProviderDispatches: [1, 1024],
  maxTurns: [1, 1024],
  maxTotalTokens: [4096, 100_000_000],
  maxEventBytes: [64 * 1024, 256 * 1024 * 1024],
  maxRepeatedToolCalls: [2, 128],
  shutdownGraceSeconds: [1, 20],
} as const;

function integerEnv(
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(
      `${name} must be an integer between ${String(minimum)} and ${String(maximum)}`,
    );
  }
  return parsed;
}

function sharedSecret(): string {
  const value = process.env.AGENT_RUNTIME_SHARED_SECRET?.trim() ?? "";
  if (Buffer.byteLength(value, "utf8") < MIN_SHARED_SECRET_BYTES) {
    return "";
  }
  return value;
}

export interface RuntimeConfig {
  readonly host: string;
  readonly port: number;
  readonly sharedSecret: string;
  readonly authClockSkewSeconds: number;
  readonly nonceTtlSeconds: number;
  readonly nonceCacheSize: number;
  readonly maxRequestBytes: number;
  readonly maxLineBytes: number;
  readonly maxConcurrentRuns: number;
  readonly maxConcurrentBodyReads: number;
  readonly maxInflightRequestBytes: number;
  readonly requestBodyTimeoutSeconds: number;
  readonly heartbeatIntervalSeconds: number;
  readonly toolGatewayTimeoutSeconds: number;
  readonly toolGatewayMaxResponseBytes: number;
  readonly maxRunSeconds: number;
  readonly maxProviderDispatches: number;
  readonly maxTurns: number;
  readonly maxTotalTokens: number;
  readonly maxEventBytes: number;
  readonly maxRepeatedToolCalls: number;
  readonly shutdownGraceSeconds: number;
}

function validatedInteger(
  value: unknown,
  name: keyof typeof INTEGER_BOUNDS,
): number {
  const [minimum, maximum] = INTEGER_BOUNDS[name];
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  ) {
    throw new Error(
      `${name} must be an integer between ${String(minimum)} and ${String(maximum)}`,
    );
  }
  return value as number;
}

export function validateRuntimeConfig(
  input: unknown,
  options: { readonly allowDisabledSecret?: boolean } = {},
): RuntimeConfig {
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Runtime config must be an object");
  }
  const config = input as Record<string, unknown>;
  const expectedKeys = new Set([
    "host",
    "sharedSecret",
    ...Object.keys(INTEGER_BOUNDS),
  ]);
  const keys = Object.keys(config);
  if (keys.some((key) => !expectedKeys.has(key)) || keys.length !== expectedKeys.size) {
    throw new Error("Runtime config has missing or unsupported fields");
  }
  if (typeof config.host !== "string" || config.host.trim() === "") {
    throw new Error("host must be a non-blank string");
  }
  const host = config.host.trim();
  if (host !== "localhost" && isIP(host) === 0) {
    throw new Error("host must be localhost or an IP address");
  }
  if (typeof config.sharedSecret !== "string") {
    throw new Error("sharedSecret must be a string");
  }
  const suppliedSecret = config.sharedSecret.trim();
  const secretBytes = Buffer.byteLength(suppliedSecret, "utf8");
  if (
    secretBytes < MIN_SHARED_SECRET_BYTES &&
    !(options.allowDisabledSecret === true && suppliedSecret === "")
  ) {
    throw new Error("sharedSecret must contain at least 32 UTF-8 bytes");
  }
  const normalizedSecret = secretBytes >= MIN_SHARED_SECRET_BYTES ? suppliedSecret : "";
  const normalized = {
    host,
    sharedSecret: normalizedSecret,
    ...Object.fromEntries(
      Object.keys(INTEGER_BOUNDS).map((name) => [
        name,
        validatedInteger(config[name], name as keyof typeof INTEGER_BOUNDS),
      ]),
    ),
  } as unknown as RuntimeConfig;
  if (normalized.maxInflightRequestBytes < normalized.maxRequestBytes) {
    throw new Error("maxInflightRequestBytes must cover one maximum request");
  }
  if (
    normalized.maxConcurrentBodyReads * normalized.maxRequestBytes >
    256 * 1024 * 1024
  ) {
    throw new Error("pre-auth body-read memory budget exceeds 256 MiB");
  }
  if (normalized.maxEventBytes < normalized.maxLineBytes * 2) {
    throw new Error("maxEventBytes must reserve at least two maximum event lines");
  }
  // Compaction checkpoints are validated against the actual serialized line at
  // emission time. Keep the minimum configurable line size useful for normal
  // events; an oversized checkpoint then fails as a typed framing error.
  return normalized;
}

export function loadConfig(): RuntimeConfig {
  return validateRuntimeConfig({
    host: process.env.AGENT_RUNTIME_HOST?.trim() || "0.0.0.0",
    port: integerEnv("AGENT_RUNTIME_PORT", 8090, 1, 65535),
    sharedSecret: sharedSecret(),
    authClockSkewSeconds: integerEnv(
      "AGENT_RUNTIME_AUTH_CLOCK_SKEW_SECONDS",
      30,
      1,
      300,
    ),
    nonceTtlSeconds: integerEnv("AGENT_RUNTIME_NONCE_TTL_SECONDS", 120, 30, 600),
    nonceCacheSize: integerEnv("AGENT_RUNTIME_NONCE_CACHE_SIZE", 10_000, 100, 100_000),
    maxRequestBytes: integerEnv(
      "AGENT_RUNTIME_MAX_REQUEST_BYTES",
      16 * 1024 * 1024,
      64 * 1024,
      64 * 1024 * 1024,
    ),
    maxLineBytes: integerEnv("AGENT_RUNTIME_MAX_LINE_BYTES", 64 * 1024, 1024, 1024 * 1024),
    maxConcurrentRuns: integerEnv("AGENT_RUNTIME_MAX_CONCURRENT_RUNS", 8, 1, 128),
    maxConcurrentBodyReads: integerEnv(
      "AGENT_RUNTIME_MAX_CONCURRENT_BODY_READS",
      16,
      1,
      256,
    ),
    maxInflightRequestBytes: integerEnv(
      "AGENT_RUNTIME_MAX_INFLIGHT_REQUEST_BYTES",
      64 * 1024 * 1024,
      64 * 1024,
      512 * 1024 * 1024,
    ),
    requestBodyTimeoutSeconds: integerEnv(
      "AGENT_RUNTIME_REQUEST_BODY_TIMEOUT_SECONDS",
      10,
      1,
      60,
    ),
    heartbeatIntervalSeconds: integerEnv(
      "AGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS",
      15,
      1,
      60,
    ),
    toolGatewayTimeoutSeconds: integerEnv(
      "AGENT_RUNTIME_TOOL_GATEWAY_TIMEOUT_SECONDS",
      30,
      1,
      300,
    ),
    toolGatewayMaxResponseBytes: integerEnv(
      "AGENT_RUNTIME_TOOL_GATEWAY_MAX_RESPONSE_BYTES",
      64 * 1024,
      1024,
      1024 * 1024,
    ),
    maxRunSeconds: integerEnv(
      "AGENT_RUNTIME_MAX_RUN_SECONDS",
      6 * 60 * 60,
      60,
      23 * 60 * 60,
    ),
    maxProviderDispatches: integerEnv(
      "AGENT_RUNTIME_MAX_PROVIDER_DISPATCHES",
      128,
      1,
      1024,
    ),
    maxTurns: integerEnv("AGENT_RUNTIME_MAX_TURNS", 128, 1, 1024),
    maxTotalTokens: integerEnv(
      "AGENT_RUNTIME_MAX_TOTAL_TOKENS",
      4_000_000,
      4096,
      100_000_000,
    ),
    maxEventBytes: integerEnv(
      "AGENT_RUNTIME_MAX_EVENT_BYTES",
      16 * 1024 * 1024,
      64 * 1024,
      256 * 1024 * 1024,
    ),
    maxRepeatedToolCalls: integerEnv(
      "AGENT_RUNTIME_MAX_REPEATED_TOOL_CALLS",
      8,
      2,
      128,
    ),
    shutdownGraceSeconds: integerEnv(
      "AGENT_RUNTIME_SHUTDOWN_GRACE_SECONDS",
      20,
      1,
      25,
    ),
  }, { allowDisabledSecret: true });
}

export const RUNTIME_VERSION = "pi-0.84.4";
export const MIN_RUNTIME_SHARED_SECRET_BYTES = MIN_SHARED_SECRET_BYTES;
