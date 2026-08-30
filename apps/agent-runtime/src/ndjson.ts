import type { ServerResponse } from "node:http";

import type { RuntimeEvent } from "./contracts.js";

export interface EventWriter {
  emit(type: string, payload?: Record<string, unknown>, force?: boolean): Promise<boolean>;
  readonly sequence: number;
  readonly bytesWritten: number;
  readonly maxLineBytes?: number;
}

export class NdjsonLineTooLargeError extends Error {
  constructor(
    readonly lineBytes: number,
    readonly maximumBytes: number,
  ) {
    super("NDJSON event exceeds the negotiated line-byte limit");
    this.name = "NdjsonLineTooLargeError";
  }
}

const DRAIN_TIMEOUT_MS = 30_000;

export function runtimeEventLineBytes(
  type: string,
  sequence: number,
  runId: string,
  executionEpoch: number,
  payload: Record<string, unknown>,
): number {
  return Buffer.byteLength(
    JSON.stringify({
      ...payload,
      version: 1,
      type,
      seq: sequence,
      run_id: runId,
      execution_epoch: executionEpoch,
    }),
    "utf8",
  ) + 1;
}

function runtimeEvent(
  type: string,
  sequence: number,
  runId: string,
  executionEpoch: number,
  payload: Record<string, unknown>,
): RuntimeEvent {
  return {
    ...payload,
    version: 1,
    type,
    seq: sequence,
    run_id: runId,
    execution_epoch: executionEpoch,
  };
}

function waitForDrain(
  response: ServerResponse,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = (): void => {
      clearTimeout(timer);
      response.off("drain", onDrain);
      response.off("close", onClose);
      response.off("error", onError);
    };
    const finish = (error?: Error): void => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const onDrain = (): void => finish();
    const onClose = (): void => {
      finish(new Error("NDJSON response closed during backpressure"));
    };
    const onError = (): void => {
      finish(new Error("NDJSON response failed during backpressure"));
    };
    const timer = setTimeout(() => {
      finish(new Error("NDJSON response backpressure timed out"));
    }, timeoutMs);
    timer.unref();
    response.once("drain", onDrain);
    response.once("close", onClose);
    response.once("error", onError);
  });
}

export class NdjsonEventWriter implements EventWriter {
  private nextSequence = 1;
  private totalBytes = 0;
  private writeTail: Promise<void> = Promise.resolve();
  private failure: Error | null = null;

  constructor(
    private readonly response: ServerResponse,
    private readonly runId: string,
    private readonly executionEpoch: number,
    readonly maxLineBytes: number,
    private readonly drainTimeoutMs: number = DRAIN_TIMEOUT_MS,
  ) {}

  get sequence(): number {
    return this.nextSequence - 1;
  }

  get bytesWritten(): number {
    return this.totalBytes;
  }

  private latchFailure(error: unknown): Error {
    if (this.failure === null) {
      this.failure = error instanceof Error
        ? error
        : new Error("NDJSON response write failed");
      this.response.destroy();
    }
    return this.failure;
  }

  async emit(
    type: string,
    payload: Record<string, unknown> = {},
    force = false,
  ): Promise<boolean> {
    void force;
    if (this.failure !== null) throw this.failure;
    let accepted = false;
    const write = this.writeTail.then(async () => {
      if (this.failure !== null) throw this.failure;
      const event = runtimeEvent(
        type,
        this.nextSequence,
        this.runId,
        this.executionEpoch,
        payload,
      );
      const line = `${JSON.stringify(event)}\n`;
      const lineBytes = runtimeEventLineBytes(
        type,
        this.nextSequence,
        this.runId,
        this.executionEpoch,
        payload,
      );
      if (lineBytes > this.maxLineBytes) {
        throw new NdjsonLineTooLargeError(lineBytes, this.maxLineBytes);
      }
      try {
        if (this.response.destroyed || this.response.writableEnded) {
          throw new Error("NDJSON response is not writable");
        }
        if (!this.response.write(line, "utf8")) {
          await waitForDrain(this.response, this.drainTimeoutMs);
        }
      } catch (error) {
        throw this.latchFailure(error);
      }
      this.nextSequence += 1;
      this.totalBytes += lineBytes;
      accepted = true;
    });
    this.writeTail = write.then(
      () => undefined,
      () => undefined,
    );
    await write;
    return accepted;
  }
}

export class CollectingEventWriter implements EventWriter {
  readonly events: RuntimeEvent[] = [];
  private totalBytes = 0;

  constructor(
    private readonly runId: string,
    private readonly executionEpoch: number,
    readonly maxLineBytes: number = Number.MAX_SAFE_INTEGER,
  ) {}

  get sequence(): number {
    return this.events.length;
  }

  get bytesWritten(): number {
    return this.totalBytes;
  }

  async emit(
    type: string,
    payload: Record<string, unknown> = {},
    force = false,
  ): Promise<boolean> {
    void force;
    const event = runtimeEvent(
      type,
      this.events.length + 1,
      this.runId,
      this.executionEpoch,
      payload,
    );
    const lineBytes = Buffer.byteLength(JSON.stringify(event), "utf8") + 1;
    if (lineBytes > this.maxLineBytes) {
      throw new NdjsonLineTooLargeError(lineBytes, this.maxLineBytes);
    }
    this.events.push(event);
    this.totalBytes += lineBytes;
    return true;
  }
}
