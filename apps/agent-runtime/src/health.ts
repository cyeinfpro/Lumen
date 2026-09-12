import { createHash } from "node:crypto";

import { MIN_RUNTIME_SHARED_SECRET_BYTES, RUNTIME_VERSION, type RuntimeConfig } from "./config.js";
import { verifyPiIsolation } from "./runtime.js";

export interface ReadinessState {
  ready: boolean;
  checkedAt: string | null;
  errorCode: string | null;
}

export class RuntimeReadiness {
  private draining = false;
  readonly state: ReadinessState = {
    ready: false,
    checkedAt: null,
    errorCode: null,
  };

  constructor(private readonly config: RuntimeConfig) {}

  markDraining(): void {
    this.draining = true;
    this.state.ready = false;
    this.state.checkedAt = new Date().toISOString();
    this.state.errorCode = "agent_runtime_draining";
  }

  private recordProbeResult(ready: boolean): void {
    // Probe completion and shutdown are separate lifecycle events. Always read
    // the current state here, rather than reusing a pre-await readiness state.
    if (this.draining) return;
    this.state.ready = ready;
    this.state.errorCode = ready ? null : "agent_runtime_not_ready";
    this.state.checkedAt = new Date().toISOString();
  }

  async check(): Promise<ReadinessState> {
    if (this.draining) return { ...this.state };
    let ready = false;
    try {
      if (
        Buffer.byteLength(this.config.sharedSecret, "utf8") <
        MIN_RUNTIME_SHARED_SECRET_BYTES
      ) {
        throw new Error("runtime shared secret is not configured");
      }
      await verifyPiIsolation();
      ready = true;
    } catch {
      // A failed isolation probe must remain unavailable.
    }
    this.recordProbeResult(ready);
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
