"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  usePatchProviderEnabledMutation,
  useProbeProvidersMutation,
  useProviderStatsQuery,
  useProvidersQuery,
  useUpdateProvidersMutation,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import type {
  ProviderItemIn,
  ProviderItemOut,
  ProviderProbeResult,
  ProviderProxyOut,
  ProviderPurpose,
  ProviderStatsItem,
} from "@/lib/types";
import {
  emptyDraft,
  groupByPriority,
  normalizePurposes,
  providerHasStoredKey,
  providerOutToIn,
  proxyOutToIn,
  toDraft,
  type Draft,
  type FieldErrors,
} from "./model";

type ValidationFailure = {
  message: string;
  index?: number;
};

function mutationErrorMessage(
  error: Error,
  fallback: string,
): string {
  if (error instanceof ApiError) {
    return error.message || `${fallback} (HTTP ${error.status})`;
  }
  return error.message || fallback;
}

function probeResultsMap(
  items: ProviderProbeResult[] | undefined,
): Map<string, ProviderProbeResult> {
  return new Map((items ?? []).map((item) => [item.name, item]));
}

function providerStatsMap(
  items: ProviderStatsItem[] | undefined,
): Map<string, ProviderStatsItem> {
  return new Map((items ?? []).map((item) => [item.name, item]));
}

function validateProviderUrl(draft: Draft): string | null {
  try {
    const url = new URL(draft.base_url.trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return `「${draft.name}」基础地址必须使用 HTTP 或 HTTPS`;
    }
    return null;
  } catch {
    return `「${draft.name}」基础地址格式不合法`;
  }
}

function validateProviderDraft(
  draft: Draft,
  index: number,
  serverItems: ProviderItemOut[],
  proxyNames: Set<string>,
): ValidationFailure | null {
  if (!draft.name.trim()) {
    return { message: `供应商 #${index + 1} 缺少名称`, index };
  }
  if (!draft.base_url.trim()) {
    return { message: `「${draft.name}」缺少基础地址`, index };
  }
  const urlError = validateProviderUrl(draft);
  if (urlError) return { message: urlError, index };
  const existingProvider = serverItems.find(
    (item) => item.name.trim() === draft.name.trim(),
  );
  if (
    !draft.api_key.trim() &&
    !providerHasStoredKey(existingProvider) &&
    draft.enabled
  ) {
    return { message: `「${draft.name}」缺少 API 密钥`, index };
  }
  if (draft.proxy && !proxyNames.has(draft.proxy)) {
    return {
      message: `「${draft.name}」引用了不存在的代理：${draft.proxy}`,
      index,
    };
  }
  if (normalizePurposes(draft.purposes).length === 0) {
    return { message: `「${draft.name}」至少需要一个用途`, index };
  }
  return null;
}

function duplicateProviderNames(drafts: Draft[]): string[] {
  const names = drafts.map((draft) => draft.name.trim());
  return [...new Set(names.filter((name, index) => names.indexOf(name) !== index))];
}

function validateProviderDrafts(
  drafts: Draft[],
  serverItems: ProviderItemOut[],
  serverProxies: ProviderProxyOut[],
): ValidationFailure | null {
  const proxyNames = new Set(
    serverProxies.map((proxy) => proxy.name.trim()).filter(Boolean),
  );
  for (let index = 0; index < drafts.length; index += 1) {
    const failure = validateProviderDraft(
      drafts[index],
      index,
      serverItems,
      proxyNames,
    );
    if (failure) return failure;
  }
  const duplicates = duplicateProviderNames(drafts);
  return duplicates.length > 0
    ? { message: `名称重复：${duplicates.join(", ")}` }
    : null;
}

function providerPayloadItem(
  draft: Draft,
  serverItems: ProviderItemOut[],
): ProviderItemIn {
  const name = draft.name.trim();
  const apiKey = draft.api_key.trim();
  const existingProvider = serverItems.find(
    (item) => item.name.trim() === name,
  );
  const hasStoredKey = providerHasStoredKey(existingProvider);
  const endpoint = draft.image_jobs_endpoint ?? "auto";
  return {
    name,
    base_url: draft.base_url.trim(),
    ...(apiKey || !hasStoredKey ? { api_key: apiKey } : {}),
    priority: draft.priority,
    weight: Math.max(1, draft.weight),
    enabled: draft.enabled,
    purposes: normalizePurposes(draft.purposes),
    image_jobs_enabled: draft.image_jobs_enabled,
    image_jobs_endpoint: endpoint,
    image_jobs_endpoint_lock:
      endpoint === "auto" ? false : Boolean(draft.image_jobs_endpoint_lock),
    image_jobs_base_url: (draft.image_jobs_base_url ?? "").trim(),
    image_edit_input_transport: draft.image_edit_input_transport ?? "url",
    image_concurrency: Math.max(
      1,
      Math.min(32, Number(draft.image_concurrency ?? 1) || 1),
    ),
    proxy: draft.proxy || null,
  };
}

function providerPayload(
  drafts: Draft[],
  serverItems: ProviderItemOut[],
): ProviderItemIn[] {
  return drafts.map((draft) => providerPayloadItem(draft, serverItems));
}

function liveDraftErrors(drafts: Draft[] | null) {
  if (!drafts) return {};
  const result: Record<number, FieldErrors> = {};
  const names = drafts.map((draft) => draft.name.trim());
  drafts.forEach((draft, index) => {
    const errors: FieldErrors = {};
    const name = draft.name.trim();
    if (name && names.indexOf(name) !== index) {
      errors.name = "名称重复";
    }
    const baseUrl = draft.base_url.trim();
    if (baseUrl) {
      const error = validateProviderUrl(draft);
      if (error) {
        errors.base_url = error.includes("HTTP")
          ? "必须使用 HTTP 或 HTTPS"
          : "URL 格式不合法";
      }
    }
    if (Object.keys(errors).length > 0) result[index] = errors;
  });
  return result;
}

export function useProviderPanelState() {
  const providersQuery = useProvidersQuery();
  const updateMutation = useUpdateProvidersMutation();
  const enabledMutation = usePatchProviderEnabledMutation();
  const probeMutation = useProbeProvidersMutation();
  const statsQuery = useProviderStatsQuery({
    enabled: (providersQuery.data?.items.length ?? 0) > 0,
  });
  const settingsMutation = useUpdateSystemSettingsMutation();

  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [deleteConfirmIdx, setDeleteConfirmIdx] = useState<number | null>(null);
  const newCardRef = useRef<HTMLDivElement>(null);

  const serverItems = useMemo(
    () => providersQuery.data?.items ?? [],
    [providersQuery.data],
  );
  const serverProxies = useMemo(
    () => providersQuery.data?.proxies ?? [],
    [providersQuery.data],
  );
  const groups = useMemo(() => groupByPriority(serverItems), [serverItems]);
  const probeMap = useMemo(
    () => probeResultsMap(probeMutation.data?.items),
    [probeMutation.data],
  );
  const statsMap = useMemo(
    () => providerStatsMap(statsQuery.data?.items),
    [statsQuery.data],
  );
  const draftErrors = useMemo(() => liveDraftErrors(drafts), [drafts]);
  const serverKeyHints = useMemo(
    () =>
      new Map(
        serverItems.map((item) => [item.name.trim(), item.api_key_hint]),
      ),
    [serverItems],
  );

  useEffect(() => {
    if (savedAt == null) return;
    const timer = setTimeout(() => setSavedAt(null), 4000);
    return () => clearTimeout(timer);
  }, [savedAt]);

  const cancelEdit = useCallback(() => {
    setDrafts(null);
    setEditingIdx(null);
    setGlobalError(null);
    setDeleteConfirmIdx(null);
  }, []);

  useEffect(() => {
    if (!drafts) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (editingIdx !== null) {
        setEditingIdx(null);
        return;
      }
      cancelEdit();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [cancelEdit, drafts, editingIdx]);

  const quickSavePurposes = useCallback(
    (providerName: string, purposes: ProviderPurpose[]) => {
      if (purposes.length === 0) {
        setGlobalError("每个供应商至少需要一个用途");
        return;
      }
      const items = serverItems.map((provider) =>
        providerOutToIn(
          provider,
          provider.name === providerName ? { purposes } : {},
        ),
      );
      updateMutation.mutate(
        { items, proxies: serverProxies.map(proxyOutToIn) },
        {
          onSuccess: () => setSavedAt(Date.now()),
          onError: (error) =>
            setGlobalError(mutationErrorMessage(error, "保存失败")),
        },
      );
    },
    [serverItems, serverProxies, updateMutation],
  );

  const toggleProviderEnabled = useCallback(
    (providerName: string, enabled: boolean) => {
      enabledMutation.mutate(
        { name: providerName, enabled },
        {
          onSuccess: () => setSavedAt(Date.now()),
          onError: (error) =>
            setGlobalError(mutationErrorMessage(error, "切换失败")),
        },
      );
    },
    [enabledMutation],
  );

  const startEdit = useCallback(() => {
    setDrafts(serverItems.map(toDraft));
    setEditingIdx(null);
    setGlobalError(null);
    setDeleteConfirmIdx(null);
  }, [serverItems]);

  const addProvider = useCallback(() => {
    const draft = emptyDraft();
    setDrafts((current) => {
      const next = [...(current ?? []), draft];
      setTimeout(() => setEditingIdx(next.length - 1), 0);
      return next;
    });
    setDeleteConfirmIdx(null);
    setTimeout(() => {
      newCardRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    }, 100);
  }, []);

  const removeProvider = useCallback((index: number) => {
    setDrafts((current) =>
      current ? current.filter((_, itemIndex) => itemIndex !== index) : current,
    );
    setEditingIdx(null);
    setDeleteConfirmIdx(null);
  }, []);

  const updateDraft = useCallback(
    (index: number, patch: Partial<Draft>) => {
      setDrafts((current) => {
        if (!current) return current;
        const next = [...current];
        next[index] = { ...next[index], ...patch };
        return next;
      });
    },
    [],
  );

  const moveProvider = useCallback((index: number, direction: -1 | 1) => {
    setDrafts((current) => {
      if (!current) return current;
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setEditingIdx((current) => {
      if (current === index) return index + direction;
      if (current === index + direction) return index;
      return current;
    });
  }, []);

  const validateAndSave = useCallback(() => {
    if (!drafts) return;
    setGlobalError(null);
    const failure = validateProviderDrafts(
      drafts,
      serverItems,
      serverProxies,
    );
    if (failure) {
      setGlobalError(failure.message);
      if (failure.index != null) setEditingIdx(failure.index);
      return;
    }
    updateMutation.mutate(
      {
        items: providerPayload(drafts, serverItems),
        proxies: serverProxies.map(proxyOutToIn),
      },
      {
        onSuccess: () => {
          setSavedAt(Date.now());
          cancelEdit();
        },
        onError: (error) =>
          setGlobalError(mutationErrorMessage(error, "保存失败")),
      },
    );
  }, [
    cancelEdit,
    drafts,
    serverItems,
    serverProxies,
    updateMutation,
  ]);

  const onProbeAll = useCallback(
    () => probeMutation.mutate(undefined),
    [probeMutation],
  );
  const onProbeSingle = useCallback(
    (name: string) => probeMutation.mutate([name]),
    [probeMutation],
  );
  const onToggleAutoProbe = useCallback(
    (interval: number) => {
      settingsMutation.mutate(
        [{ key: "providers.auto_probe_interval", value: String(interval) }],
        { onSuccess: () => void statsQuery.refetch() },
      );
    },
    [settingsMutation, statsQuery],
  );

  return {
    providersQuery,
    statsItems: statsQuery.data?.items,
    serverItems,
    serverProxies,
    groups,
    source: providersQuery.data?.source ?? "none",
    drafts,
    editingIdx,
    deleteConfirmIdx,
    globalError,
    savedAt,
    draftErrors,
    serverKeyHints,
    newCardRef,
    probeMap,
    statsMap,
    probeTimestamp: probeMutation.data?.probed_at ?? null,
    autoProbeInterval: statsQuery.data?.auto_probe_interval ?? 120,
    enabledCount: serverItems.filter((provider) => provider.enabled).length,
    healthyCount: probeMutation.data
      ? probeMutation.data.items.filter((result) => result.ok).length
      : null,
    isEditing: drafts !== null,
    probing: probeMutation.isPending,
    settingsSaving: settingsMutation.isPending,
    updateSaving: updateMutation.isPending,
    quickSaving: enabledMutation.isPending || updateMutation.isPending,
    setEditingIdx,
    setDeleteConfirmIdx,
    clearGlobalError: () => setGlobalError(null),
    cancelEdit,
    startEdit,
    addProvider,
    removeProvider,
    updateDraft,
    moveProvider,
    validateAndSave,
    onProbeAll,
    onProbeSingle,
    onToggleAutoProbe,
    quickSavePurposes,
    toggleProviderEnabled,
  };
}

export type ProviderPanelState = ReturnType<typeof useProviderPanelState>;
