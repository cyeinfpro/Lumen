import type { ServerResponse } from "node:http";

import type { RuntimeEvent } from "./contracts.js";

export interface EventWriter {
  emit(type: string, payload?: Record<string, unknown>, force?: boolean): Promise<boolean>;
  readonly sequence: number;
  readonly bytesWritten: number;
}

function waitForDrain(response: ServerResponse): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = (): void => {
      response.off("drain", onDrain);
      response.off("close", onClose);
    };
    const onDrain = (): void => {
      cleanup();
      resolve();
    };
    const onClose = (): void => {
      cleanup();
      reject(new Error("NDJSON response closed during backpressure"));
    };
    response.once("drain", onDrain);
    response.once("close", onClose);
  });
}

export class NdjsonEventWriter implements EventWriter {
  private nextSequence = 1;
  private totalBytes = 0;
  private ordinaryEvents = 0;

  constructor(
    private readonly response: ServerResponse,
    private readonly runId: string,
    private readonly executionEpoch: number,
    private readonly maxLineBytes: number,
    private readonly maxStreamBytes: number,
    private readonly maxEvents: number,
  ) {}

  get sequence(): number {
    return this.nextSequence - 1;
  }

  get bytesWritten(): number {
    return this.totalBytes;
  }

  async emit(
    type: string,
    payload: Record<string, unknown> = {},
    force = false,
  ): Promise<boolean> {
    const event: RuntimeEvent = {
      version: 1,
      type,
      seq: this.nextSequence,
      run_id: this.runId,
      execution_epoch: this.executionEpoch,
      ...payload,
    };
    const line = `${JSON.stringify(event)}\n`;
    const lineBytes = Buffer.byteLength(line, "utf8");
    if (lineBytes > this.maxLineBytes) return false;
    const eventLimit = force ? this.maxEvents : Math.max(1, this.maxEvents - 1);
    const byteLimit = force
      ? this.maxStreamBytes
      : Math.max(this.maxLineBytes, this.maxStreamBytes - this.maxLineBytes);
    if (this.ordinaryEvents >= eventLimit || this.totalBytes + lineBytes > byteLimit) {
      return false;
    }
    this.nextSequence += 1;
    this.ordinaryEvents += 1;
    this.totalBytes += lineBytes;
    if (!this.response.write(line, "utf8")) await waitForDrain(this.response);
    return true;
  }
}

export class CollectingEventWriter implements EventWriter {
  readonly events: RuntimeEvent[] = [];
  private totalBytes = 0;

  constructor(
    private readonly runId: string,
    private readonly executionEpoch: number,
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
    const event: RuntimeEvent = {
      version: 1,
      type,
      seq: this.events.length + 1,
      run_id: this.runId,
      execution_epoch: this.executionEpoch,
      ...payload,
    };
    this.events.push(event);
    this.totalBytes += Buffer.byteLength(JSON.stringify(event), "utf8") + 1;
    return true;
  }
}
