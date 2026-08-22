"use client";

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ApiError } from "@/lib/apiClient";
import { discoverProviderModels } from "@/lib/api/system";
import { useSystemSettingsQuery } from "@/lib/queries";
import type { Draft } from "./model";
import {
  modelProfilePatch,
  preferredProviderModel,
  type ProviderModelDiscoveryState,
} from "./modelDiscovery";

function discoveryErrorMessage(error: Error): string {
  if (error instanceof ApiError) {
    return error.message || `模型读取失败 (HTTP ${error.status})`;
  }
  return error.message || "模型读取失败";
}

function modelDiscoveryDraft(
  drafts: Draft[] | null,
  index: number,
  serverKeyHints: Map<string, string>,
): Draft | null {
  const draft = drafts?.[index];
  if (!draft || !draft.base_url.trim()) return null;
  const hasStoredKey = Boolean(serverKeyHints.get(draft.name.trim())?.trim());
  return draft.api_key.trim() || hasStoredKey ? draft : null;
}

function modelDiscoveryError(
  result: Awaited<ReturnType<typeof discoverProviderModels>>,
): string | null {
  if (result.error) return result.error;
  return result.models.length === 0 ? "供应商未返回模型" : null;
}

export function useProviderModelDiscovery({
  drafts,
  setDrafts,
  serverKeyHints,
}: {
  drafts: Draft[] | null;
  setDrafts: Dispatch<SetStateAction<Draft[] | null>>;
  serverKeyHints: Map<string, string>;
}) {
  const settingsQuery = useSystemSettingsQuery({ retry: false });
  const [modelDiscoveries, setModelDiscoveries] = useState<
    Record<number, ProviderModelDiscoveryState>
  >({});
  const discoveryControllers = useRef(new Map<number, AbortController>());
  const currentDefaultModel = useMemo(
    () =>
      settingsQuery.data?.items
        .find((item) => item.key === "upstream.default_model")
        ?.value?.trim() || "gpt-5.6-sol",
    [settingsQuery.data?.items],
  );
  const defaultModelForSave = useMemo(
    () =>
      Object.values(modelDiscoveries).find(
        (item) => item.setAsDefault && item.selectedModelId,
      )?.selectedModelId ?? null,
    [modelDiscoveries],
  );

  const cancelAll = useCallback(() => {
    for (const controller of discoveryControllers.current.values()) {
      controller.abort();
    }
    discoveryControllers.current.clear();
    setModelDiscoveries({});
  }, []);

  useEffect(() => cancelAll, [cancelAll]);

  const remove = useCallback((draftKey: number | undefined) => {
    if (draftKey === undefined) return;
    discoveryControllers.current.get(draftKey)?.abort();
    discoveryControllers.current.delete(draftKey);
    setModelDiscoveries((current) => {
      const next = { ...current };
      delete next[draftKey];
      return next;
    });
  }, []);

  const updateDraft = useCallback(
    (index: number, patch: Partial<Draft>) => {
      const draftKey = drafts?.[index]?._key;
      const invalidatesDiscovery = [
        "base_url",
        "api_key",
        "proxy",
        "agent_api",
      ].some((key) => Object.prototype.hasOwnProperty.call(patch, key));
      const changesConnection = ["base_url", "api_key"].some((key) =>
        Object.prototype.hasOwnProperty.call(patch, key),
      );
      if (invalidatesDiscovery) remove(draftKey);
      setDrafts((current) => {
        if (!current) return current;
        const next = [...current];
        next[index] = {
          ...next[index],
          ...(changesConnection
            ? {
                agent_models: [],
                responses_supported: null,
                vision_supported: null,
                agent_context_window: 128_000,
                agent_max_output_tokens: 16_384,
                agent_reasoning_supported: false,
              }
            : {}),
          ...patch,
        };
        return next;
      });
    },
    [drafts, remove, setDrafts],
  );

  const discover = useCallback(
    async (index: number) => {
      const draft = modelDiscoveryDraft(drafts, index, serverKeyHints);
      if (!draft) return;
      const draftKey = draft._key;
      discoveryControllers.current.get(draftKey)?.abort();
      const controller = new AbortController();
      discoveryControllers.current.set(draftKey, controller);
      setModelDiscoveries((current) => ({
        ...current,
        [draftKey]: {
          status: "loading",
          models: current[draftKey]?.models ?? [],
          selectedModelId: current[draftKey]?.selectedModelId ?? null,
          setAsDefault: current[draftKey]?.setAsDefault ?? false,
          error: null,
        },
      }));
      try {
        const result = await discoverProviderModels(
          {
            provider_name: draft.name.trim() || null,
            base_url: draft.base_url.trim(),
            api_key: draft.api_key.trim(),
            proxy: draft.proxy || null,
            agent_api: draft.agent_api ?? "openai-responses",
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        const resultError = modelDiscoveryError(result);
        if (resultError) {
          setModelDiscoveries((current) => ({
            ...current,
            [draftKey]: {
              status: "error",
              models: [],
              selectedModelId: null,
              setAsDefault: false,
              error: resultError,
            },
          }));
          return;
        }
        const selected = preferredProviderModel(
          result.models,
          currentDefaultModel,
        );
        if (!selected) return;
        const setAsDefault = selected.id !== currentDefaultModel;
        setDrafts(
          (current) =>
            current?.map((item) =>
              item._key === draftKey
                ? { ...item, ...modelProfilePatch(selected, result.models) }
                : item,
            ) ?? current,
        );
        setModelDiscoveries((current) => {
          const next = clearDefaultCandidates(current);
          next[draftKey] = {
            status: "ready",
            models: result.models,
            selectedModelId: selected.id,
            setAsDefault,
            error: null,
          };
          return next;
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        setModelDiscoveries((current) => ({
          ...current,
          [draftKey]: {
            status: "error",
            models: [],
            selectedModelId: null,
            setAsDefault: false,
            error: discoveryErrorMessage(error as Error),
          },
        }));
      } finally {
        if (discoveryControllers.current.get(draftKey) === controller) {
          discoveryControllers.current.delete(draftKey);
        }
      }
    },
    [currentDefaultModel, drafts, serverKeyHints, setDrafts],
  );

  const select = useCallback(
    (index: number, modelId: string) => {
      const draft = drafts?.[index];
      if (!draft) return;
      const discovery = modelDiscoveries[draft._key];
      const selected = discovery?.models.find((model) => model.id === modelId);
      if (!discovery || !selected) return;
      setDrafts((current) => {
        if (!current) return current;
        const next = [...current];
        next[index] = {
          ...next[index],
          ...modelProfilePatch(selected, discovery.models),
        };
        return next;
      });
      setModelDiscoveries((current) => ({
        ...clearDefaultCandidates(current),
        [draft._key]: {
          ...discovery,
          selectedModelId: modelId,
          setAsDefault: modelId !== currentDefaultModel,
        },
      }));
    },
    [currentDefaultModel, drafts, modelDiscoveries, setDrafts],
  );

  const setDefault = useCallback(
    (index: number, enabled: boolean) => {
      const draftKey = drafts?.[index]?._key;
      if (draftKey === undefined) return;
      setModelDiscoveries((current) => {
        const next = enabled ? clearDefaultCandidates(current) : { ...current };
        if (next[draftKey]) {
          next[draftKey] = { ...next[draftKey], setAsDefault: enabled };
        }
        return next;
      });
    },
    [drafts],
  );

  return {
    currentDefaultModel,
    defaultModelForSave,
    modelDiscoveries,
    cancelAll,
    remove,
    updateDraft,
    discover,
    select,
    setDefault,
  };
}

function clearDefaultCandidates(
  current: Record<number, ProviderModelDiscoveryState>,
): Record<number, ProviderModelDiscoveryState> {
  return Object.fromEntries(
    Object.entries(current).map(([key, value]) => [
      key,
      { ...value, setAsDefault: false },
    ]),
  ) as Record<number, ProviderModelDiscoveryState>;
}
