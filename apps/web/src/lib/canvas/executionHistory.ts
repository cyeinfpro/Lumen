import type {
  CanvasDocument, CanvasExecutionFreshness, CanvasExecutionHistoryPage,
  CanvasGraph, CanvasHistoricalExecution,
} from "./types";

export const CANVAS_HISTORY_PAGE_SIZE = 30;
export const CANVAS_HISTORY_MAX_PAGES = 100;
export type HistoryScope = Readonly<{ userId: string; epoch: number; canvasId: string; nodeId: string }>;
export function canvasHistoryQueryKey(scope: HistoryScope, cursor: string | null) {
  return ["user", scope.userId, "canvas", "history", scope.epoch, scope.canvasId, scope.nodeId, cursor] as const;
}
export function historyRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
export function normalizeExecutionFreshness(value: unknown): Record<string, CanvasExecutionFreshness> {
  return Object.fromEntries(Object.entries(historyRecord(value) ?? {}).map(([id, entry]) => {
    const raw = historyRecord(entry);
    return [id, { state: raw?.state === "fresh" || raw?.state === "stale" ? raw.state : "unknown",
      reason: typeof raw?.reason === "string" ? raw.reason : null }];
  }));
}

// Projection is about the exact saved graph. Pending edits, older revisions,
// or a different local graph can never inherit a server "fresh" badge.
export function savedCanvasProjectionMatches(document: CanvasDocument, graph: CanvasGraph, revision: number, pending: number): boolean {
  return pending === 0 && revision === document.revision && JSON.stringify(graph) === JSON.stringify(document.graph);
}
export function visibleExecutionFreshness(
  document: CanvasDocument, graph: CanvasGraph, revision: number,
  pending: number, executionId: string, matches = savedCanvasProjectionMatches(document, graph, revision, pending),
): CanvasExecutionFreshness {
  if (!matches) {
    return { state: "unknown", reason: "draft_changed" };
  }
  return document.execution_freshness?.[executionId] ?? { state: "unknown", reason: "snapshot_unavailable" };
}

function freeze<T>(value: T): T {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
export function immutableHistorySelection(value: CanvasHistoricalExecution): CanvasHistoricalExecution {
  return freeze(structuredClone(value));
}
export function completeExecutionProvenance(value: CanvasHistoricalExecution): boolean {
  return !!value.config_snapshot && Object.keys(value.config_snapshot).length > 0 &&
    !!value.input_snapshot && Array.isArray(value.input_snapshot.bindings) &&
    typeof value.input_snapshot.prompt === "string" &&
    !!value.definition_hash && !!value.input_hash && !!value.processor_version;
}

export interface ExecutionComparison {
  rows: Array<{ path: string; a: string; b: string }>;
  incomplete: boolean;
  truncated: boolean;
}
// Bound both traversal and rendered strings. Equality is never claimed when a
// legacy snapshot is absent or a comparison was truncated.
type ComparisonContext = { result: ExecutionComparison; budget: number };
function printComparedValue(value: unknown, context: ComparisonContext): string {
  const text = value === undefined ? "缺失" : JSON.stringify(value) ?? "缺失";
  if (text.length > 1200) context.result.truncated = true;
  return text.length > 1200 ? text.slice(0, 1200) + "…" : text;
}
function compareChildren(left: Record<string, unknown>, right: Record<string, unknown>, path: string, depth: number, context: ComparisonContext) {
  for (const key of Array.from(new Set([...Object.keys(left), ...Object.keys(right)])).sort()) {
    if (context.budget <= 0) { context.result.truncated = true; break; }
    compareValue(left[key], right[key], path ? path + "." + key : key, depth + 1, context);
  }
}
function compareArrays(left: unknown[], right: unknown[], path: string, depth: number, context: ComparisonContext) {
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    if (context.budget <= 0) { context.result.truncated = true; break; }
    compareValue(left[i], right[i], path + "[" + i + "]", depth + 1, context);
  }
}
function compareLeaf(left: unknown, right: unknown, path: string, context: ComparisonContext) {
  const a = printComparedValue(left, context), b = printComparedValue(right, context);
  if (a !== b) context.result.rows.push({ path, a, b });
  else if (typeof left === "object" || typeof right === "object") context.result.truncated = true;
}
function compareValue(left: unknown, right: unknown, path: string, depth: number, context: ComparisonContext) {
  if (--context.budget < 0 || context.result.rows.length >= 80) { context.result.truncated = true; return; }
  if (Object.is(left, right)) return;
  if (depth >= 8) { compareLeaf(left, right, path, context); return; }
  const l = historyRecord(left), r = historyRecord(right);
  if (l && r) compareChildren(l, r, path, depth, context);
  else if (Array.isArray(left) && Array.isArray(right)) compareArrays(left, right, path, depth, context);
  else compareLeaf(left, right, path, context);
}
export function compareHistoricalExecutions(a: CanvasHistoricalExecution, b: CanvasHistoricalExecution): ExecutionComparison {
  const result: ExecutionComparison = { rows: [], incomplete: !completeExecutionProvenance(a) || !completeExecutionProvenance(b), truncated: false };
  const context = { result, budget: 300 };
  for (const key of ["config_snapshot", "input_snapshot", "outputs", "definition_hash", "input_hash", "processor_version"] as const) {
    compareValue(a[key], b[key], key, 0, context);
  }
  return result;
}

export interface HistoryState {
  items: CanvasHistoricalExecution[];
  cursor: string | null;
  nextCursor: string | null;
  previous: Array<string | null>;
  loading: boolean;
  error: string | null;
  a: CanvasHistoricalExecution | null;
  b: CanvasHistoricalExecution | null;
}
type PageDirection = "initial" | "next" | "previous" | "retry";
function pageDestination(state: HistoryState, direction: PageDirection): Pick<HistoryState, "cursor" | "previous"> | null {
  if (direction === "initial") return { cursor: null, previous: [] };
  if (direction === "next") {
    if (!state.nextCursor || state.previous.length >= CANVAS_HISTORY_MAX_PAGES - 1) return null;
    return { cursor: state.nextCursor, previous: [...state.previous, state.cursor] };
  }
  if (direction === "previous") {
    if (!state.previous.length) return null;
    return { cursor: state.previous.at(-1) ?? null, previous: state.previous.slice(0, -1) };
  }
  return { cursor: state.cursor, previous: state.previous };
}
export class CanvasHistoryPager {
  state: HistoryState = { items: [], cursor: null, nextCursor: null, previous: [], loading: false, error: null, a: null, b: null };
  private generation = 0;
  private controller: AbortController | null = null;
  private listeners = new Set<() => void>();
  readonly scope: HistoryScope;
  private readonly read: (cursor: string | null, signal: AbortSignal) => Promise<CanvasExecutionHistoryPage>;
  private readonly current: () => boolean;
  constructor(scope: HistoryScope, read: (cursor: string | null, signal: AbortSignal) => Promise<CanvasExecutionHistoryPage>, current: () => boolean) {
    this.scope = scope; this.read = read; this.current = current;
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  private update(next: Partial<HistoryState>) { this.state = { ...this.state, ...next }; this.listeners.forEach((fn) => fn()); }
  select(slot: "a" | "b", execution: CanvasHistoricalExecution) {
    if (this.current() && execution.node_id === this.scope.nodeId) this.update({ [slot]: immutableHistorySelection(execution) });
  }
  clear() { this.update({ a: null, b: null }); }
  cancel() { ++this.generation; this.controller?.abort(); this.update({ loading: false }); }
  dispose() { this.cancel(); this.listeners.clear(); }
  private live(controller: AbortController, generation: number) {
    return !controller.signal.aborted && generation === this.generation && this.current();
  }
  private accept(page: CanvasExecutionHistoryPage, destination: Pick<HistoryState, "cursor" | "previous">) {
    if (page.items.length > CANVAS_HISTORY_PAGE_SIZE || page.items.some((item) => item.node_id !== this.scope.nodeId)) throw new Error("历史响应与所选节点不一致");
    const seen = new Set<string>();
    const items = page.items.filter((item) => !seen.has(item.id) && !!seen.add(item.id));
    const nextCursor = page.next_cursor === destination.cursor || destination.previous.includes(page.next_cursor) ? null : page.next_cursor;
    this.update({ items, nextCursor, loading: false });
  }
  async load(direction: PageDirection = "initial") {
    if (!this.current() || (this.state.loading && direction !== "initial")) return;
    const destination = pageDestination(this.state, direction);
    if (!destination) return;
    this.controller?.abort();
    const controller = new AbortController(), generation = ++this.generation;
    this.controller = controller;
    this.update({ ...destination, loading: true, error: null, items: [], nextCursor: null });
    try {
      const page = await this.read(destination.cursor, controller.signal);
      if (this.live(controller, generation)) this.accept(page, destination);
    } catch (error) {
      if (this.live(controller, generation)) this.update({ loading: false, error: error instanceof Error ? error.message : "历史加载失败" });
    }
  }
}
