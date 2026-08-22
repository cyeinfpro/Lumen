const MIN_SHARED_SECRET_BYTES = 32;

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
  readonly maxStreamBytes: number;
  readonly maxEvents: number;
  readonly maxConcurrentRuns: number;
  readonly requestBodyTimeoutSeconds: number;
}

export function loadConfig(): RuntimeConfig {
  return {
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
      8 * 1024 * 1024,
      64 * 1024,
      32 * 1024 * 1024,
    ),
    maxLineBytes: integerEnv("AGENT_RUNTIME_MAX_LINE_BYTES", 64 * 1024, 1024, 1024 * 1024),
    maxStreamBytes: integerEnv(
      "AGENT_RUNTIME_MAX_STREAM_BYTES",
      8 * 1024 * 1024,
      64 * 1024,
      64 * 1024 * 1024,
    ),
    maxEvents: integerEnv("AGENT_RUNTIME_MAX_EVENTS", 4096, 16, 20_000),
    maxConcurrentRuns: integerEnv("AGENT_RUNTIME_MAX_CONCURRENT_RUNS", 8, 1, 128),
    requestBodyTimeoutSeconds: integerEnv(
      "AGENT_RUNTIME_REQUEST_BODY_TIMEOUT_SECONDS",
      10,
      1,
      60,
    ),
  };
}

export const RUNTIME_VERSION = "pi-0.84.2";
export const MIN_RUNTIME_SHARED_SECRET_BYTES = MIN_SHARED_SECRET_BYTES;
