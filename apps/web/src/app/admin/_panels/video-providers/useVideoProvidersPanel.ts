"use client";

import { useMemo, useReducer } from "react";

import {
  useUpdateVideoProvidersMutation,
  useVideoProvidersQuery,
} from "@/lib/queries";

import {
  analyzeDrafts,
  analyzeProvider,
  draftSaveError,
  draftToInput,
  saveError,
  toDraft,
  type Draft,
  type ModelDraft,
} from "./domain";
import { summarizeDrafts, summarizeProviders } from "./metrics";
import {
  initialVideoProvidersPanelState,
  videoProvidersPanelReducer,
} from "./panelState";

export function useVideoProvidersPanel() {
  const query = useVideoProvidersQuery();
  const updateMutation = useUpdateVideoProvidersMutation();
  const [state, dispatch] = useReducer(
    videoProvidersPanelReducer,
    initialVideoProvidersPanelState,
  );

  const serverItems = useMemo(
    () => query.data?.items ?? [],
    [query.data?.items],
  );
  const proxyNames = useMemo(
    () => (query.data?.proxies ?? []).map((item) => item.name),
    [query.data?.proxies],
  );
  const providerSummaries = useMemo(
    () => serverItems.map(analyzeProvider),
    [serverItems],
  );
  const providerMetrics = useMemo(
    () => summarizeProviders(providerSummaries),
    [providerSummaries],
  );
  const draftSummaries = useMemo(
    () =>
      state.drafts
        ? analyzeDrafts(state.drafts, state.enabledDraft, serverItems)
        : [],
    [state.drafts, state.enabledDraft, serverItems],
  );
  const draftMetrics = useMemo(
    () => summarizeDrafts(draftSummaries, state.enabledDraft),
    [draftSummaries, state.enabledDraft],
  );

  const beginEdit = () => {
    dispatch({
      type: "beginEdit",
      drafts: serverItems.map(toDraft),
      enabled: Boolean(query.data?.enabled),
    });
  };

  const addDraft = (draft: Draft) => {
    dispatch({ type: "addDraft", draft });
  };

  const updateDraft = (index: number, patch: Partial<Draft>) => {
    dispatch({ type: "patchDraft", index, patch });
  };

  const updateModel = (
    providerIndex: number,
    modelIndex: number,
    patch: Partial<ModelDraft>,
  ) => {
    dispatch({
      type: "patchModel",
      providerIndex,
      modelIndex,
      patch,
    });
  };

  const deleteDraft = (index: number) => {
    dispatch({ type: "deleteDraft", index });
  };

  const save = () => {
    if (!state.drafts || updateMutation.isPending) return;

    dispatch({ type: "saveStart" });
    const validationError = draftSaveError(
      state.drafts,
      state.enabledDraft,
      serverItems,
    );
    if (validationError) {
      dispatch({ type: "saveFailure", error: validationError });
      return;
    }

    updateMutation.mutate(
      {
        enabled: state.enabledDraft,
        items: state.drafts.map(draftToInput),
      },
      {
        onSuccess: () => dispatch({ type: "saveSuccess" }),
        onError: (error) =>
          dispatch({ type: "saveFailure", error: saveError(error) }),
      },
    );
  };

  const editor =
    state.drafts === null
      ? null
      : {
          drafts: state.drafts,
          summaries: draftSummaries,
          metrics: draftMetrics,
          enabled: state.enabledDraft,
          source: query.data?.source,
          serverItems,
          proxyNames,
          saving: updateMutation.isPending,
        };

  return {
    query,
    overview: {
      enabled: Boolean(query.data?.enabled),
      source: query.data?.source,
      serverItems,
      summaries: providerSummaries,
      metrics: providerMetrics,
    },
    editor,
    feedback: {
      error: state.error,
      saved: state.saved,
    },
    actions: {
      beginEdit,
      setEnabled: (enabled: boolean) =>
        dispatch({ type: "setEnabled", enabled }),
      addDraft,
      updateDraft,
      updateModel,
      deleteDraft,
      save,
      discard: () => dispatch({ type: "discard" }),
      dismissError: () => dispatch({ type: "dismissError" }),
    },
  };
}
