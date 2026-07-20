import {
  normalizeVideoProviderEnabled,
  type Draft,
  type ModelDraft,
} from "./domain";

export type VideoProvidersPanelState = {
  drafts: Draft[] | null;
  enabledDraft: boolean;
  error: string | null;
  saved: boolean;
};

export type VideoProvidersPanelAction =
  | { type: "beginEdit"; drafts: Draft[]; enabled: boolean }
  | { type: "setEnabled"; enabled: boolean }
  | { type: "addDraft"; draft: Draft }
  | { type: "patchDraft"; index: number; patch: Partial<Draft> }
  | {
      type: "patchModel";
      providerIndex: number;
      modelIndex: number;
      patch: Partial<ModelDraft>;
    }
  | { type: "deleteDraft"; index: number }
  | { type: "saveStart" }
  | { type: "saveFailure"; error: string }
  | { type: "saveSuccess" }
  | { type: "discard" }
  | { type: "dismissError" };

export const initialVideoProvidersPanelState: VideoProvidersPanelState = {
  drafts: null,
  enabledDraft: false,
  error: null,
  saved: false,
};

export function patchDraftAt(
  drafts: Draft[] | null,
  index: number,
  patch: Partial<Draft>,
): Draft[] | null {
  if (!drafts || index < 0 || index >= drafts.length) return drafts;

  const next = [...drafts];
  const patched = { ...next[index], ...patch };
  next[index] = {
    ...patched,
    enabled: normalizeVideoProviderEnabled(patched.kind, patched.enabled),
  };
  return next;
}

export function patchModelAt(
  drafts: Draft[] | null,
  providerIndex: number,
  modelIndex: number,
  patch: Partial<ModelDraft>,
): Draft[] | null {
  const provider = drafts?.[providerIndex];
  if (
    !drafts ||
    !provider ||
    providerIndex < 0 ||
    modelIndex < 0 ||
    modelIndex >= provider.models.length
  ) {
    return drafts;
  }

  const next = [...drafts];
  const models = [...provider.models];
  models[modelIndex] = { ...models[modelIndex], ...patch };
  next[providerIndex] = { ...provider, models };
  return next;
}

export function deleteDraftAt(
  drafts: Draft[] | null,
  index: number,
): Draft[] | null {
  return (
    drafts?.filter((_draft, candidateIndex) => candidateIndex !== index) ?? null
  );
}

export function videoProvidersPanelReducer(
  state: VideoProvidersPanelState,
  action: VideoProvidersPanelAction,
): VideoProvidersPanelState {
  switch (action.type) {
    case "beginEdit":
      return {
        drafts: action.drafts,
        enabledDraft: action.enabled,
        error: null,
        saved: false,
      };
    case "setEnabled":
      return { ...state, enabledDraft: action.enabled };
    case "addDraft":
      return { ...state, drafts: [...(state.drafts ?? []), action.draft] };
    case "patchDraft":
      return {
        ...state,
        drafts: patchDraftAt(state.drafts, action.index, action.patch),
      };
    case "patchModel":
      return {
        ...state,
        drafts: patchModelAt(
          state.drafts,
          action.providerIndex,
          action.modelIndex,
          action.patch,
        ),
      };
    case "deleteDraft":
      return {
        ...state,
        drafts: deleteDraftAt(state.drafts, action.index),
      };
    case "saveStart":
      return { ...state, error: null };
    case "saveFailure":
      return { ...state, error: action.error };
    case "saveSuccess":
      return { ...state, drafts: null, error: null, saved: true };
    case "discard":
      return { ...state, drafts: null, error: null };
    case "dismissError":
      return { ...state, error: null };
  }
}
