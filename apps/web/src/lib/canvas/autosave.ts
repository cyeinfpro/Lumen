export interface AutosaveBatch<T> {
  count: number;
  payload: T;
}

export interface SerialAutosaveOptions<T> {
  delayMs?: number;
  readBatch: () => AutosaveBatch<T> | null;
  sendBatch: (batch: AutosaveBatch<T>) => Promise<void>;
  onError?: (error: unknown) => void;
}

export class SerialAutosave<T> {
  private readonly delayMs: number;
  private readonly readBatch: () => AutosaveBatch<T> | null;
  private readonly sendBatch: (batch: AutosaveBatch<T>) => Promise<void>;
  private readonly onError?: (error: unknown) => void;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inFlight = false;
  private rerun = false;
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

  async flush(): Promise<void> {
    if (this.stopped) return;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.inFlight) {
      this.rerun = true;
      return;
    }
    const batch = this.readBatch();
    if (!batch || batch.count <= 0) return;
    this.inFlight = true;
    try {
      await this.sendBatch(batch);
    } catch (error) {
      this.rerun = false;
      this.onError?.(error);
      return;
    } finally {
      this.inFlight = false;
    }
    if (this.rerun) {
      this.rerun = false;
      await this.flush();
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
