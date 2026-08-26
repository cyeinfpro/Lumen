import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import type { IncomingHttpHeaders } from "node:http";

export const AUTH_VERSION = "v1";
export const AUTH_TIMESTAMP_HEADER = "x-lumen-agent-timestamp";
export const AUTH_NONCE_HEADER = "x-lumen-agent-nonce";
export const AUTH_SIGNATURE_HEADER = "x-lumen-agent-signature";

export class RuntimeAuthError extends Error {
  constructor(readonly code: string) {
    super("Agent Runtime authentication failed");
    this.name = "RuntimeAuthError";
  }
}

export function sha256Hex(body: Uint8Array): string {
  return createHash("sha256").update(body).digest("hex");
}

export function canonicalRuntimeRequest(
  method: string,
  path: string,
  timestamp: string,
  nonce: string,
  body: Uint8Array,
): string {
  return [AUTH_VERSION, method.toUpperCase(), path, timestamp, nonce, sha256Hex(body)].join("\n");
}

export function signRuntimeRequest(
  secret: string,
  method: string,
  path: string,
  timestamp: string,
  nonce: string,
  body: Uint8Array,
): string {
  return createHmac("sha256", secret)
    .update(canonicalRuntimeRequest(method, path, timestamp, nonce, body))
    .digest("hex");
}

class NonceCache {
  private readonly entries = new Map<string, number>();

  constructor(
    private readonly ttlMs: number,
    private readonly maximum: number,
  ) {}

  claim(nonce: string, nowMs: number): "claimed" | "replayed" | "full" {
    for (const [key, expiresAt] of this.entries) {
      if (expiresAt <= nowMs) this.entries.delete(key);
    }
    if (this.entries.has(nonce)) return "replayed";
    if (this.entries.size >= this.maximum) return "full";
    this.entries.set(nonce, nowMs + this.ttlMs);
    return "claimed";
  }
}

function oneHeader(headers: IncomingHttpHeaders, name: string): string {
  const value = headers[name];
  if (typeof value !== "string" || value.trim() === "") {
    throw new RuntimeAuthError("agent_runtime_auth_required");
  }
  return value.trim();
}

export class RuntimeAuthenticator {
  private readonly nonces: NonceCache;

  constructor(
    private readonly secret: string,
    nonceTtlSeconds: number,
    nonceCacheSize: number,
    private readonly clockSkewSeconds: number,
  ) {
    const replayWindowSeconds = clockSkewSeconds * 2 + 1;
    this.nonces = new NonceCache(
      Math.max(nonceTtlSeconds, replayWindowSeconds) * 1000,
      nonceCacheSize,
    );
  }

  verify(
    method: string,
    path: string,
    headers: IncomingHttpHeaders,
    body: Uint8Array,
    nowMs = Date.now(),
  ): void {
    if (Buffer.byteLength(this.secret, "utf8") < 32) {
      throw new RuntimeAuthError("agent_runtime_auth_unconfigured");
    }
    const timestamp = oneHeader(headers, AUTH_TIMESTAMP_HEADER);
    const nonce = oneHeader(headers, AUTH_NONCE_HEADER);
    const signature = oneHeader(headers, AUTH_SIGNATURE_HEADER).toLowerCase();
    if (!/^\d{10}$/u.test(timestamp) || !/^[A-Za-z0-9_-]{16,96}$/u.test(nonce)) {
      throw new RuntimeAuthError("agent_runtime_auth_invalid");
    }
    const timestampMs = Number(timestamp) * 1000;
    if (Math.abs(nowMs - timestampMs) > this.clockSkewSeconds * 1000) {
      throw new RuntimeAuthError("agent_runtime_auth_expired");
    }
    if (!/^[a-f0-9]{64}$/u.test(signature)) {
      throw new RuntimeAuthError("agent_runtime_auth_invalid");
    }
    const expected = signRuntimeRequest(this.secret, method, path, timestamp, nonce, body);
    const suppliedBytes = Buffer.from(signature, "hex");
    const expectedBytes = Buffer.from(expected, "hex");
    if (
      suppliedBytes.length !== expectedBytes.length ||
      !timingSafeEqual(suppliedBytes, expectedBytes)
    ) {
      throw new RuntimeAuthError("agent_runtime_auth_invalid");
    }
    const nonceResult = this.nonces.claim(nonce, nowMs);
    if (nonceResult === "replayed") {
      throw new RuntimeAuthError("agent_runtime_auth_replayed");
    }
    if (nonceResult === "full") {
      throw new RuntimeAuthError("agent_runtime_auth_capacity_exhausted");
    }
  }
}
