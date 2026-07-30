"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  createApiSupplier,
  getByokSettings,
  listApiSuppliers,
  patchApiSupplier,
  patchByokSettings,
  probeApiSupplier,
} from "@/lib/apiClient";
import type {
  ApiSupplierTemplateOut,
  ByokSettingsOut,
  ByokSettingsPatchIn,
} from "@/lib/types";

import {
  detectMode,
  EMPTY_SUPPLIER,
  MODE_DEFS,
  retentionStateFor,
  supplierDraftToCreateBody,
  supplierDraftToPatchBody,
  supplierToDraft,
  validateBaseUrl,
} from "./ByokPanel.model";
import type { ByokMode, SupplierDraft } from "./ByokPanel.model";
import { ByokNotices } from "./ByokPanel.shared";
import {
  ByokSystemSettingsSection,
  Overview,
} from "./ByokPanel.settings";
import {
  ByokSupplierList,
  NewSupplierSection,
} from "./ByokPanel.suppliers";

export function ByokPanel() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ["admin", "byok-settings"],
    queryFn: getByokSettings,
    retry: false,
  });
  const suppliersQuery = useQuery({
    queryKey: ["admin", "byok-suppliers"],
    queryFn: listApiSuppliers,
    retry: false,
  });

  const [settingsDraft, setSettingsDraft] = useState<ByokSettingsPatchIn>({});
  const [newSupplier, setNewSupplier] =
    useState<SupplierDraft>(EMPTY_SUPPLIER);
  const [newSupplierUrlError, setNewSupplierUrlError] = useState<string | null>(
    null,
  );
  const [newSupplierOpen, setNewSupplierOpen] = useState(false);
  const [supplierDrafts, setSupplierDrafts] = useState<
    Record<string, SupplierDraft>
  >({});
  const [supplierUrlErrors, setSupplierUrlErrors] = useState<
    Record<string, string | null>
  >({});
  const [openSupplierId, setOpenSupplierId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<Record<string, string>>({});

  const saveSettingsMutation = useMutation({
    mutationFn: () => patchByokSettings(settingsDraft),
    onSuccess: async () => {
      setSaved("系统设置已更新");
      setSettingsDraft({});
      await queryClient.invalidateQueries({
        queryKey: ["admin", "byok-settings"],
      });
    },
    onError: (mutationError) => setError(errorText(mutationError)),
  });

  const createSupplierMutation = useMutation({
    mutationFn: () =>
      createApiSupplier(supplierDraftToCreateBody(newSupplier)),
    onSuccess: async () => {
      setNewSupplier(EMPTY_SUPPLIER);
      setNewSupplierUrlError(null);
      setNewSupplierOpen(false);
      setSaved("供应商已创建");
      await queryClient.invalidateQueries({
        queryKey: ["admin", "byok-suppliers"],
      });
    },
    onError: (mutationError) => setError(errorText(mutationError)),
  });

  const patchSupplierMutation = useMutation({
    mutationFn: (payload: { id: string; body: SupplierDraft }) =>
      patchApiSupplier(
        payload.id,
        supplierDraftToPatchBody(payload.body),
      ),
    onSuccess: async () => {
      setSaved("供应商已更新");
      await queryClient.invalidateQueries({
        queryKey: ["admin", "byok-suppliers"],
      });
    },
    onError: (mutationError) => setError(errorText(mutationError)),
  });

  const probeSupplierMutation = useMutation({
    mutationFn: (payload: { id: string; api_key: string }) =>
      probeApiSupplier(payload.id, payload.api_key),
    onSuccess: (result, variables) => {
      setProbeResult((current) => ({
        ...current,
        [variables.id]: result.ok
          ? `通过 · ${result.latency_ms}ms`
          : `${result.error_code ?? "probe_failed"} · ${result.latency_ms}ms`,
      }));
    },
    onError: (mutationError, variables) => {
      setProbeResult((current) => ({
        ...current,
        [variables.id]: errorText(mutationError),
      }));
    },
  });

  const settings = settingsQuery.data;
  const suppliers = useMemo(
    () => suppliersQuery.data?.items ?? [],
    [suppliersQuery.data],
  );
  const effectiveSettings = useMemo<ByokSettingsOut | undefined>(() => {
    if (!settings) return undefined;
    return { ...settings, ...settingsDraft };
  }, [settings, settingsDraft]);
  const currentMode = detectMode(effectiveSettings);
  const totalActive = useMemo(
    () =>
      suppliers.reduce(
        (total, supplier) => total + supplier.active_credentials,
        0,
      ),
    [suppliers],
  );

  const setMode = (mode: ByokMode) => {
    const definition = MODE_DEFS.find((item) => item.value === mode);
    if (!definition) return;
    setSettingsDraft((current) => ({
      ...current,
      ...definition.toggles,
    }));
  };

  const {
    hideDays: retentionHideDays,
    deleteDays: retentionDeleteDays,
    invalid: retentionInvalid,
  } = retentionStateFor(settingsDraft, settings, effectiveSettings);

  const createSupplier = () => {
    const urlError = validateBaseUrl(newSupplier.base_url);
    if (urlError) {
      setNewSupplierUrlError(urlError);
      return;
    }
    if (!newSupplier.name.trim()) {
      setError("供应商名称不能为空");
      return;
    }
    createSupplierMutation.mutate();
  };

  const saveSupplier = (supplier: ApiSupplierTemplateOut) => {
    const body = supplierDrafts[supplier.id] ?? supplierToDraft(supplier);
    const urlError = validateBaseUrl(body.base_url);
    if (urlError) {
      setSupplierUrlErrors((current) => ({
        ...current,
        [supplier.id]: urlError,
      }));
      return;
    }
    patchSupplierMutation.mutate({ id: supplier.id, body });
  };

  const probeSupplier = (supplier: ApiSupplierTemplateOut) => {
    probeSupplierMutation.mutate({
      id: supplier.id,
      api_key: (supplierDrafts[supplier.id]?.probe_key ?? "").trim(),
    });
  };

  const loading = settingsQuery.isLoading || suppliersQuery.isLoading;
  const settingsBusy = saveSettingsMutation.isPending;
  const settingsDirty = Object.keys(settingsDraft).length > 0;

  return (
    <div className="space-y-6">
      <Overview
        mode={currentMode}
        supplierCount={suppliers.length}
        activeCredentials={totalActive}
        loading={loading}
      />

      <ByokSystemSettingsSection
        currentMode={currentMode}
        effectiveSettings={effectiveSettings}
        draft={settingsDraft}
        settings={settings}
        hideDays={retentionHideDays}
        deleteDays={retentionDeleteDays}
        retentionInvalid={retentionInvalid}
        busy={settingsBusy}
        dirty={settingsDirty}
        onSetMode={setMode}
        onPatch={(patch) =>
          setSettingsDraft((current) => ({ ...current, ...patch }))
        }
        onSave={() => saveSettingsMutation.mutate()}
        onDiscard={() => setSettingsDraft({})}
      />

      <NewSupplierSection
        draft={newSupplier}
        urlError={newSupplierUrlError}
        open={newSupplierOpen}
        busy={createSupplierMutation.isPending}
        onToggle={() => setNewSupplierOpen((current) => !current)}
        onChange={setNewSupplier}
        onUrlBlur={setNewSupplierUrlError}
        onCreate={createSupplier}
      />

      <ByokSupplierList
        suppliers={suppliers}
        openSupplierId={openSupplierId}
        supplierDrafts={supplierDrafts}
        supplierUrlErrors={supplierUrlErrors}
        probeResult={probeResult}
        busy={
          patchSupplierMutation.isPending ||
          probeSupplierMutation.isPending
        }
        onToggle={(id) =>
          setOpenSupplierId((current) => (current === id ? null : id))
        }
        onChange={(id, draft) =>
          setSupplierDrafts((current) => ({ ...current, [id]: draft }))
        }
        onUrlBlur={(id, urlError) =>
          setSupplierUrlErrors((current) => ({
            ...current,
            [id]: urlError,
          }))
        }
        onSave={saveSupplier}
        onProbe={probeSupplier}
      />

      <ByokNotices
        error={error}
        saved={saved}
        onClearError={() => setError(null)}
        onClearSaved={() => setSaved(null)}
      />
    </div>
  );
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message || `请求失败 (HTTP ${error.status})`;
  }
  return error instanceof Error ? error.message : "请求失败";
}
