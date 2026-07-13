export interface AutosaveBatch<T> {
  count: number;
  payload: T;
}

export const CANVAS_AUTOSAVE_OPERATION_LIMIT = 500;

export function takeAutosaveOperations<T>(
  operations: readonly T[],
  limit = CANVAS_AUTOSAVE_OPERATION_LIMIT,
): T[] {
  return operations.slice(0, Math.max(0, limit));
}

export interface SerialAutosaveOptions<T> {
  delayMs?: number;
  readBatch: () => AutosaveBatch<T> | null;
  sendBatch: (batch: AutosaveBatch<T>) => Promise<void>;
  onError?: (error: unknown) => void;
}

export class RetryableAutosaveBatchReader<T> {
  private current: AutosaveBatch<T> | null = null;
  private readonly readFreshBatch: () => AutosaveBatch<T> | null;

  constructor(readFreshBatch: () => AutosaveBatch<T> | null) {
    this.readFreshBatch = readFreshBatch;
  }

  read(): AutosaveBatch<T> | null {
    if (!this.current) this.current = this.readFreshBatch();
    return this.current;
  }

  acknowledge(batch: AutosaveBatch<T>): void {
    if (this.current === batch) this.current = null;
  }

  discard(): void {
    this.current = null;
  }
}

export class SerialAutosave<T> {
  private readonly delayMs: number;
  private readonly readBatch: () => AutosaveBatch<T> | null;
  private readonly sendBatch: (batch: AutosaveBatch<T>) => Promise<void>;
  private readonly onError?: (error: unknown) => void;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private activeFlush: Promise<void> | null = null;
  private rerunRequested = false;
  private stopped = false;

  constructor(options: SerialAutosaveOptions<T>) {
    this.delayMs = options.delayMs ?? 750;
    this.readBatch = options.readBatch;
    this.sendBatch = options.sendBatch;
    this.onError = options.onError;
  }

  schedule(): void {
    if (this.stopped) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, this.delayMs);
  }

  flush(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.activeFlush) {
      this.rerunRequested = true;
      return this.activeFlush;
    }
    this.rerunRequested = false;
    const activeFlush = Promise.resolve()
      .then(() => this.runFlush())
      .finally(() => {
        if (this.activeFlush === activeFlush) {
          this.activeFlush = null;
          this.rerunRequested = false;
        }
      });
    this.activeFlush = activeFlush;
    return activeFlush;
  }

  stop(): void {
    this.stopped = true;
    this.rerunRequested = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private async runFlush(): Promise<void> {
    while (!this.stopped) {
      const batch = this.readBatch();
      if (!batch || batch.count <= 0) {
        this.rerunRequested = false;
        return;
      }
      try {
        await this.sendBatch(batch);
      } catch (error) {
        this.rerunRequested = false;
        this.onError?.(error);
        return;
      }
      if (!this.rerunRequested) return;
      this.rerunRequested = false;
    }
  }
}
