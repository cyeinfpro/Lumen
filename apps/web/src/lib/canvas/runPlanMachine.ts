import type { CanvasPendingPlanIntent, CanvasPlanInput, CanvasPlanPreview, CanvasPlanRunDetail } from "./runPlanTypes";

export interface CanvasPlanState {
  phase: "idle" | "previewing" | "ready" | "submitting" | "unknown" | "admitted" | "error";
  generation: number;
  preview: CanvasPlanPreview | null;
  input: CanvasPlanInput | null;
  previewContext: string | null;
  pending: CanvasPendingPlanIntent | null;
  run: CanvasPlanRunDetail | null;
  error: string | null;
}
export const initialCanvasPlanState: CanvasPlanState = {
  phase: "idle", generation: 0, preview: null, input: null, previewContext: null,
  pending: null, run: null, error: null,
};
export type CanvasPlanEvent =
  | { type: "invalidate" }
  | { type: "previewing"; generation: number }
  | { type: "preview"; generation: number; preview: CanvasPlanPreview; input: CanvasPlanInput; context: string }
  | { type: "submitting" }
  | { type: "unknown"; pending: CanvasPendingPlanIntent }
  | { type: "admitted"; run: CanvasPlanRunDetail; refresh?: boolean }
  | { type: "error"; error: string; generation?: number };
export function canvasPlanReducer(state: CanvasPlanState, event: CanvasPlanEvent): CanvasPlanState {
  if (event.type === "invalidate") return invalidateCanvasPlan(state);
  switch (event.type) {
    case "previewing":
      if (admissionUnresolved(state)) return state;
      return { ...state, phase: "previewing", generation: event.generation, preview: null, input: null, error: null };
    case "preview":
      if (state.phase !== "previewing" || event.generation !== state.generation) return state;
      return { ...state, phase: "ready", preview: event.preview, input: event.input, previewContext: event.context, error: null };
    case "submitting":
      return state.pending ? state : { ...state, phase: "submitting", error: null };
    case "unknown":
      return { ...state, phase: "unknown", pending: event.pending, preview: null, input: null,
        error: "提交状态待确认。查询不会重新生成；不要创建新的付费请求。" };
    case "admitted":
      return acceptCanvasPlanRun(state, event);
    case "error":
      if (event.generation !== undefined && event.generation !== state.generation) return state;
      return { ...state, phase: state.pending ? "unknown" : "error", preview: null, input: null, error: event.error };
  }
}
export function canvasPlanCanStart(state: CanvasPlanState, context: string): boolean {
  return state.phase === "ready" && !!state.preview && !!state.input &&
    state.previewContext === context && !state.pending;
}

function invalidateCanvasPlan(state: CanvasPlanState): CanvasPlanState {
  const phase = state.pending || state.phase === "submitting" ? state.phase : state.run ? "admitted" : "idle";
  return { ...state, preview: null, input: null, previewContext: null, phase };
}

function admissionUnresolved(state: CanvasPlanState) { return !!state.pending || state.phase === "submitting"; }

function acceptCanvasPlanRun(state: CanvasPlanState, event: Extract<CanvasPlanEvent, { type: "admitted" }>): CanvasPlanState {
  if (event.refresh && (state.pending || state.run?.id !== event.run.id)) return state;
  if (state.run?.id === event.run.id && (state.run.last_event_seq ?? 0) > (event.run.last_event_seq ?? 0)) return state;
  if (event.refresh) return { ...state, run: event.run };
  return { ...state, phase: "admitted", run: event.run, pending: null, preview: null, input: null, error: null };
}
