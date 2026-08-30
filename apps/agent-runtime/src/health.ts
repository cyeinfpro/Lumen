import { createHash } from "node:crypto";

import { MIN_RUNTIME_SHARED_SECRET_BYTES, RUNTIME_VERSION, type RuntimeConfig } from "./config.js";
import { verifyPiIsolation } from "./runtime.js";

export interface ReadinessState {
  ready: boolean;
  checkedAt: string | null;
  errorCode: string | null;
}

export class RuntimeReadiness {
  readonly state: ReadinessState = {
    ready: false,
    checkedAt: null,
    errorCode: null,
  };

  constructor(private readonly config: RuntimeConfig) {}

  markDraining(): void {
    this.state.ready = false;
    this.state.checkedAt = new Date().toISOString();
    this.state.errorCode = "agent_runtime_draining";
  }

  async check(): Promise<ReadinessState> {
    try {
      if (
        Buffer.byteLength(this.config.sharedSecret, "utf8") <
        MIN_RUNTIME_SHARED_SECRET_BYTES
      ) {
        throw new Error("runtime shared secret is not configured");
      }
      await verifyPiIsolation();
      this.state.ready = true;
      this.state.errorCode = null;
    } catch {
      this.state.ready = false;
      this.state.errorCode = "agent_runtime_not_ready";
    }
    this.state.checkedAt = new Date().toISOString();
    return { ...this.state };
  }
}

export function healthPayload(config: RuntimeConfig): Record<string, unknown> {
  return {
    ok: true,
    service: "lumen-agent-runtime",
    runtime_version: RUNTIME_VERSION,
    max_request_bytes: config.maxRequestBytes,
    max_line_bytes: config.maxLineBytes,
  };
}

export function runtimeAuthKeyId(sharedSecret: string): string | null {
  if (Buffer.byteLength(sharedSecret, "utf8") < MIN_RUNTIME_SHARED_SECRET_BYTES) {
    return null;
  }
  return createHash("sha256").update(sharedSecret, "utf8").digest("hex").slice(0, 16);
}

export function readinessPayload(
  state: ReadinessState,
  config: RuntimeConfig,
): Record<string, unknown> {
  return {
    ok: state.ready,
    service: "lumen-agent-runtime",
    runtime_version: RUNTIME_VERSION,
    checked_at: state.checkedAt,
    error_code: state.errorCode,
    auth_key_id: runtimeAuthKeyId(config.sharedSecret),
    max_request_bytes: config.maxRequestBytes,
    max_line_bytes: config.maxLineBytes,
  };
}
