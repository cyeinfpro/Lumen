"use client";

import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { toast } from "@/components/ui/primitives";
import {
  bulkUpdateAdminPricing,
  importOpenAiPricing,
  rotateAdminRedemptionSecret,
  updateAdminPricing,
  updateSystemSettings,
} from "@/lib/apiClient";
import type { PricingRuleOut, SystemSettingItem } from "@/lib/types";
import {
  EMPTY_VIDEO_NEW_MODEL,
  billingSettingsPayload,
  buildBulkPricingInput,
  buildDisabledModelPricingItems,
  buildDisabledVideoPricingItems,
  buildImagePricingRequest,
  buildImagePricingRows,
  buildModelPricingItems,
  buildOfficialVideoDrafts,
  buildVideoPricingItems,
  groupModelRules,
  groupVideoRules,
  resolveBillingSettings,
  videoDraftKey,
  type BillingSettingsDraft,
  type ImagePricingRow,
  type ModelRuleRow,
  type VideoNewModelDraft,
  type VideoPricingVariant,
  type VideoResolution,
  type VideoRuleRow,
} from "./pricingModel";
import type { InvalidateBilling } from "./usePricingData";

function errorDescription(error: unknown): string | undefined {
  return error instanceof Error ? error.message : undefined;
}

export function useGlobalSettingsForm(
  items: SystemSettingItem[],
  invalidateBilling: InvalidateBilling,
) {
  const [draft, setDraft] = useState<BillingSettingsDraft>({});
  const [secretConfirmed, setSecretConfirmed] = useState(false);
  const values = useMemo(
    () => resolveBillingSettings(items, draft),
    [draft, items],
  );
  const saveMutation = useMutation({
    mutationFn: () => updateSystemSettings(billingSettingsPayload(values)),
    onSuccess: async () => {
      toast.success("全局设置已保存");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("保存失败", { description: errorDescription(error) }),
  });
  const rotateSecretMutation = useMutation({
    mutationFn: rotateAdminRedemptionSecret,
    onSuccess: async () => {
      setSecretConfirmed(false);
      toast.success(
        values.secretConfigured
          ? "兑换码 secret 已轮换"
          : "兑换码 secret 已生成",
      );
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("更新 secret 失败", {
        description: errorDescription(error),
      }),
  });
  const setValue = (field: keyof BillingSettingsDraft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  return {
    values,
    secretConfirmed,
    savePending: saveMutation.isPending,
    rotateSecretPending: rotateSecretMutation.isPending,
    setValue,
    setSecretConfirmed,
    save: () => saveMutation.mutate(),
    rotateSecret: () => rotateSecretMutation.mutate(),
  };
}

export type GlobalSettingsFormState = ReturnType<
  typeof useGlobalSettingsForm
>;

export function useBulkPricingForm(
  invalidateBilling: InvalidateBilling,
) {
  const [identity, setIdentity] = useState({
    model: "",
    channel: "",
    priority: "0",
  });
  const [rates, setRates] = useState<Record<string, string>>({});
  const saveMutation = useMutation({
    mutationFn: () =>
      bulkUpdateAdminPricing(
        buildBulkPricingInput({
          ...identity,
          rates,
        }),
      ),
    onSuccess: async () => {
      toast.success("批量模型定价已保存");
      setIdentity({ model: "", channel: "", priority: "0" });
      setRates({});
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("批量保存失败", {
        description: errorDescription(error),
      }),
  });
  const setIdentityField = (
    field: keyof typeof identity,
    value: string,
  ) => {
    setIdentity((current) => ({ ...current, [field]: value }));
  };
  const setRate = (key: string, value: string) => {
    setRates((current) => ({ ...current, [key]: value }));
  };

  return {
    ...identity,
    rates,
    savePending: saveMutation.isPending,
    setIdentityField,
    setRate,
    save: () => saveMutation.mutate(),
  };
}

export type BulkPricingFormState = ReturnType<typeof useBulkPricingForm>;

export function useImagePricingForm(
  items: PricingRuleOut[],
  savedThresholds: Record<string, number>,
  invalidateBilling: InvalidateBilling,
) {
  const [prices, setPrices] = useState<Record<string, string>>({});
  const [thresholds, setThresholds] = useState<Record<string, string>>({});
  const [customTiers, setCustomTiers] = useState<string[]>([]);
  const [newTier, setNewTier] = useState("");
  const [newTierThreshold, setNewTierThreshold] = useState("");
  const rows = useMemo(
    () => buildImagePricingRows(items, savedThresholds, customTiers),
    [customTiers, items, savedThresholds],
  );
  const saveMutation = useMutation({
    mutationFn: () => {
      const request = buildImagePricingRequest(rows, prices, thresholds);
      return updateAdminPricing(request.items, {
        image_size_thresholds: request.imageSizeThresholds,
      });
    },
    onSuccess: async () => {
      toast.success("尺寸定价已保存");
      setPrices({});
      setThresholds({});
      setCustomTiers([]);
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("保存失败", { description: errorDescription(error) }),
  });
  const addTier = () => {
    const tier = newTier.trim().toLowerCase();
    if (!tier) {
      toast.warning("请填写档位名称");
      return;
    }
    if (rows.some((row) => row.tier === tier)) {
      toast.warning("档位已存在");
      return;
    }
    const threshold = Number(newTierThreshold) || 0;
    setCustomTiers((current) =>
      current.includes(tier) ? current : [...current, tier],
    );
    setPrices((current) => ({ ...current, [tier]: "0" }));
    setThresholds((current) => ({
      ...current,
      [tier]: String(threshold),
    }));
    setNewTier("");
    setNewTierThreshold("");
  };

  return {
    rows,
    prices,
    thresholds,
    newTier,
    newTierThreshold,
    savePending: saveMutation.isPending,
    setNewTier,
    setNewTierThreshold,
    setPrice: (tier: string, value: string) =>
      setPrices((current) => ({ ...current, [tier]: value })),
    setThreshold: (tier: string, value: string) =>
      setThresholds((current) => ({ ...current, [tier]: value })),
    addTier,
    save: () => saveMutation.mutate(),
  };
}

export type ImagePricingFormState = ReturnType<typeof useImagePricingForm>;

export function useVideoPricingForm(
  items: PricingRuleOut[],
  invalidateBilling: InvalidateBilling,
) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [newModel, setNewModel] = useState<VideoNewModelDraft>({
    ...EMPTY_VIDEO_NEW_MODEL,
  });
  const [officialMultiplier, setOfficialMultiplier] = useState("1");
  const rows = useMemo(() => groupVideoRules(items), [items]);
  const saveMutation = useMutation({
    mutationFn: () =>
      updateAdminPricing(
        buildVideoPricingItems({
          rows,
          drafts,
          newModel,
        }),
      ),
    onSuccess: async () => {
      setDrafts({});
      setNewModel((current) => ({
        ...EMPTY_VIDEO_NEW_MODEL,
        note: current.note,
      }));
      toast.success("视频定价已保存");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("保存失败", { description: errorDescription(error) }),
  });
  const disableMutation = useMutation({
    mutationFn: (row: VideoRuleRow) =>
      updateAdminPricing(buildDisabledVideoPricingItems(row)),
    onSuccess: async () => {
      toast.success("视频模型已停用");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("停用失败", { description: errorDescription(error) }),
  });
  const applyOfficialPricing = () => {
    const multiplier = Number(officialMultiplier);
    if (!Number.isFinite(multiplier) || multiplier <= 0) {
      toast.warning("请填写大于 0 的官方价倍率");
      return;
    }
    const nextDrafts = buildOfficialVideoDrafts(multiplier);
    setDrafts((current) => ({ ...current, ...nextDrafts }));
    toast.success("已按官方价填充视频定价");
  };
  const setDraft = (
    model: string,
    variant: VideoPricingVariant,
    resolution: VideoResolution,
    value: string,
  ) => {
    const key = videoDraftKey(model, variant, resolution);
    setDrafts((current) => ({ ...current, [key]: value }));
  };
  const setNewModelField = (
    field: keyof VideoNewModelDraft,
    value: string,
  ) => {
    setNewModel((current) => ({ ...current, [field]: value }));
  };

  return {
    rows,
    drafts,
    newModel,
    officialMultiplier,
    savePending: saveMutation.isPending,
    disablePending: disableMutation.isPending,
    saveAvailable:
      rows.length > 0 ||
      newModel.model.trim().length > 0 ||
      Object.keys(drafts).length > 0,
    setOfficialMultiplier,
    setDraft,
    setNewModelField,
    applyOfficialPricing,
    save: () => saveMutation.mutate(),
    disable: (row: VideoRuleRow) => disableMutation.mutate(row),
  };
}

export type VideoPricingFormState = ReturnType<typeof useVideoPricingForm>;

export function useModelPricingForm(
  items: PricingRuleOut[],
  rate: string,
  invalidateBilling: InvalidateBilling,
) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [priceFile, setPriceFile] = useState("");
  const rows = useMemo(() => groupModelRules(items), [items]);
  const saveMutation = useMutation({
    mutationFn: () =>
      updateAdminPricing(buildModelPricingItems(rows, drafts)),
    onSuccess: async () => {
      setDrafts({});
      toast.success("模型价格已保存");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("保存失败", { description: errorDescription(error) }),
  });
  const disableMutation = useMutation({
    mutationFn: (row: ModelRuleRow) =>
      updateAdminPricing(buildDisabledModelPricingItems(row)),
    onSuccess: async () => {
      toast.success("模型已停用");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("停用失败", { description: errorDescription(error) }),
  });
  const importMutation = useMutation({
    mutationFn: () => importOpenAiPricing(priceFile, Number(rate) || 1),
    onSuccess: async () => {
      toast.success("对话模型价格已导入");
      setPriceFile("");
      await invalidateBilling();
    },
    onError: (error) =>
      toast.error("导入失败", { description: errorDescription(error) }),
  });

  return {
    rows,
    drafts,
    priceFile,
    savePending: saveMutation.isPending,
    importPending: importMutation.isPending,
    disablePending: disableMutation.isPending,
    setPriceFile,
    setDraft: (
      model: string,
      direction: "in" | "out",
      value: string,
    ) =>
      setDrafts((current) => ({
        ...current,
        [`${model}:${direction}`]: value,
      })),
    save: () => saveMutation.mutate(),
    importPricing: () => importMutation.mutate(),
    disable: (row: ModelRuleRow) => disableMutation.mutate(row),
  };
}

export type ModelPricingFormState = ReturnType<typeof useModelPricingForm>;

export type { ImagePricingRow };
