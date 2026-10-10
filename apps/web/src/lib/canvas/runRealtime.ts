import type { CanvasDocument, CanvasNodeExecution, CanvasRun } from "./types";
import { mergeCanvasDocumentByRevision } from "./documentMerge.ts";

export type CanvasRunNotice = {
  canvas_id: string; run_id: string; seq: number;
  execution_id: string | null; event_type: string;
};
export type CanvasRunEvent = {
  run_id: string; seq: number; payload: Record<string, unknown>;
};
export type CanvasRunEventBatch = {
  items: CanvasRunEvent[]; after_seq: number; next_after_seq: number;
  last_event_seq: number; has_more: boolean; snapshot_required: boolean;
};
export type CanvasRunDetail = { run: CanvasRun; executions: CanvasNodeExecution[] };
const id = (value: unknown): value is string =>
  typeof value === "string" && value.trim().length > 0 && value.length <= 128;
const seq = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function parseCanvasRunNotice(value: unknown): CanvasRunNotice | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (raw.schema_version !== 1 || !id(raw.canvas_id) || !id(raw.run_id) ||
    !seq(raw.seq) || raw.seq === 0 ||
    (raw.execution_id != null && !id(raw.execution_id)) ||
    typeof raw.event_type !== "string" || !/^[a-z][a-z0-9_.:-]{0,79}$/.test(raw.event_type)) return null;
  return { canvas_id: raw.canvas_id, run_id: raw.run_id, seq: raw.seq,
    execution_id: raw.execution_id as string | null ?? null, event_type: raw.event_type };
}

function validBatchHeader(value: CanvasRunEventBatch, after: number): boolean {
  return Boolean(value && value.snapshot_required === false && value.after_seq === after &&
    seq(value.next_after_seq) && seq(value.last_event_seq) &&
    Array.isArray(value.items) && value.items.length <= 200 &&
    typeof value.has_more === "boolean");
}
function validBatchEvent(event: CanvasRunEvent, runId: string, expected: number): boolean {
  return Boolean(event && typeof event === "object" && event.run_id === runId &&
    event.seq === expected && event.payload && typeof event.payload === "object" &&
    !Array.isArray(event.payload));
}
export function continuousCanvasBatch(
  value: CanvasRunEventBatch, runId: string, after: number,
): boolean {
  if (!validBatchHeader(value, after)) return false;
  let expected = after + 1;
  for (const event of value.items) {
    if (!validBatchEvent(event, runId, expected)) return false;
    expected += 1;
  }
  const cursor = expected - 1;
  return value.next_after_seq === cursor && cursor <= value.last_event_seq &&
    value.has_more === (cursor < value.last_event_seq) &&
    (value.items.length > 0 || cursor === value.last_event_seq);
}

export function mergeCanvasRunDetail(
  current: CanvasDocument, detail: CanvasRunDetail,
): CanvasDocument {
  const incoming = new Map(detail.executions.map((execution) => [execution.id, execution]));
  const executions = current.recent_executions.map((execution) => {
    const replacement = incoming.get(execution.id);
    incoming.delete(execution.id);
    return replacement ?? execution;
  });
  const runs = current.active_runs.filter((run) => run.id !== detail.run.id);
  return mergeCanvasDocumentByRevision(current, { ...current,
    recent_executions: [...incoming.values(), ...executions],
    active_runs: [detail.run, ...runs] });
}

export type CanvasRunRealtimeDependencies = {
  isCurrent(): boolean;
  current(canvasId: string): CanvasDocument | undefined;
  interested(canvasId: string): boolean;
  batch(canvasId: string, runId: string, after: number, signal: AbortSignal): Promise<CanvasRunEventBatch>;
  detail(canvasId: string, runId: string, signal: AbortSignal): Promise<CanvasRunDetail>;
  apply(canvasId: string, detail: CanvasRunDetail): void;
  snapshot(canvasId: string, signal: AbortSignal): Promise<void>;
};
type RunState = { notice: CanvasRunNotice; acknowledged: number; controller: AbortController; job?: Promise<void> };

// One coordinator per authenticated identity. It uses the existing global SSE/
// cross-tab delivery; it never creates another EventSource or generation task.
export class CanvasRunRealtimeCoordinator {
  private readonly states = new Map<string, RunState>();
  private disposed = false;
  private readonly dependencies: CanvasRunRealtimeDependencies;
  constructor(dependencies: CanvasRunRealtimeDependencies) { this.dependencies = dependencies; }

  receive(value: unknown): Promise<void> {
    const notice = parseCanvasRunNotice(value);
    if (!notice || !this.current() || !this.dependencies.interested(notice.canvas_id)) return Promise.resolve();
    const key = JSON.stringify([notice.canvas_id, notice.run_id]);
    let state = this.states.get(key);
    if (!state) {
      if (this.states.size >= 128) {
        const oldest = this.states.keys().next().value!;
        this.states.get(oldest)?.controller.abort();
        this.states.delete(oldest);
      }
      const snapshot = this.dependencies.current(notice.canvas_id);
      const snapshotSeq = snapshot?.active_runs.find((run) => run.id === notice.run_id)?.last_event_seq;
      state = { notice, acknowledged: seq(snapshotSeq) ? snapshotSeq : 0,
        controller: new AbortController() };
      this.states.set(key, state);
    }
    if (notice.seq > state.notice.seq) state.notice = notice;
    if (state.job || state.notice.seq <= state.acknowledged) return state.job ?? Promise.resolve();
    const run = state;
    let completed = false;
    run.job = this.drain(run).then(() => { completed = true; }).catch(() => {
      // Do not advance after failed reads. Existing snapshot polling/replay can retry.
    }).finally(() => {
      run.job = undefined;
      // A notice can arrive in a microtask between the drain's final check and
      // promise cleanup. Restart only a successful drain, never spin on errors.
      if (completed && run.notice.seq > run.acknowledged) {
        void this.receive({ schema_version: 1, ...run.notice });
      }
    });
    return run.job;
  }

  dispose(): void {
    this.disposed = true;
    for (const state of this.states.values()) state.controller.abort();
    this.states.clear();
  }

  private current(): boolean { return !this.disposed && this.dependencies.isCurrent(); }
  private assertCurrent(state: RunState): void {
    state.controller.signal.throwIfAborted();
    if (!this.current()) throw new DOMException("Stale canvas realtime scope", "AbortError");
  }

  private async restore(state: RunState): Promise<void> {
    const target = state.notice.seq;
    await this.dependencies.snapshot(state.notice.canvas_id, state.controller.signal);
    this.assertCurrent(state);
    state.acknowledged = Math.max(state.acknowledged, target);
  }

  private async drain(state: RunState): Promise<void> {
    let pages = 0;
    while (state.acknowledged < state.notice.seq) {
      this.assertCurrent(state);
      if (!this.dependencies.interested(state.notice.canvas_id)) return;
      if (++pages > 3) { await this.restore(state); return; }
      const { canvas_id: canvasId, run_id: runId } = state.notice;
      const batch = await this.dependencies.batch(canvasId, runId, state.acknowledged, state.controller.signal);
      this.assertCurrent(state);
      if (!continuousCanvasBatch(batch, runId, state.acknowledged) ||
        batch.items.some((event) => event.payload.selection_updated === true ||
          (Array.isArray(event.payload.outputs) && event.payload.outputs.length > 0))) {
        await this.restore(state); continue;
      }
      const detail = await this.dependencies.detail(canvasId, runId, state.controller.signal);
      this.assertCurrent(state);
      if (detail.run.id !== runId || detail.run.last_event_seq !== batch.next_after_seq ||
        detail.executions.some((execution) => execution.run_id !== runId)) {
        await this.restore(state); continue;
      }
      this.dependencies.apply(canvasId, detail);
      state.acknowledged = batch.next_after_seq;
      if (!batch.has_more && state.acknowledged < state.notice.seq) {
        await this.restore(state);
      }
    }
  }
}
