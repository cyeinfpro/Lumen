"use client";

import type { QueryKey } from "@tanstack/react-query";

import type { PricingRuleOut, SystemSettingItem } from "@/lib/types";
import {
  BulkPricingSection,
  GlobalSettingsSection,
  ImagePricingSection,
  ModelPricingSection,
  VideoPricingSection,
} from "./PricingSections";
import { DEFAULT_IMAGE_THRESHOLDS } from "./pricingModel";
import { usePricingData } from "./usePricingData";
import {
  useBulkPricingForm,
  useGlobalSettingsForm,
  useImagePricingForm,
  useModelPricingForm,
  useVideoPricingForm,
} from "./usePricingForms";

const EMPTY_PRICING_RULES: PricingRuleOut[] = [];
const EMPTY_SETTINGS: SystemSettingItem[] = [];

export function PricingSubpanel({
  userBillingRootQueryKey,
}: {
  userBillingRootQueryKey: QueryKey;
}) {
  const data = usePricingData(userBillingRootQueryKey);
  const pricingItems = data.pricingQuery.data?.items ?? EMPTY_PRICING_RULES;
  const settingsItems = data.settingsQuery.data?.items ?? EMPTY_SETTINGS;
  const imageThresholds =
    data.pricingQuery.data?.image_size_thresholds ?? DEFAULT_IMAGE_THRESHOLDS;
  const globalSettingsForm = useGlobalSettingsForm(
    settingsItems,
    data.invalidateBilling,
  );
  const bulkPricingForm = useBulkPricingForm(data.invalidateBilling);
  const imagePricingForm = useImagePricingForm(
    pricingItems,
    imageThresholds,
    data.invalidateBilling,
  );
  const videoPricingForm = useVideoPricingForm(
    pricingItems,
    data.invalidateBilling,
  );
  const modelPricingForm = useModelPricingForm(
    pricingItems,
    globalSettingsForm.values.rate,
    data.invalidateBilling,
  );

  return (
    <div className="space-y-5">
      <GlobalSettingsSection form={globalSettingsForm} />
      <BulkPricingSection form={bulkPricingForm} />
      <ImagePricingSection form={imagePricingForm} />
      <VideoPricingSection
        form={videoPricingForm}
        loading={data.pricingQuery.isLoading}
      />
      <ModelPricingSection
        form={modelPricingForm}
        rate={globalSettingsForm.values.rate}
        loading={data.pricingQuery.isLoading}
        onRateChange={(value) => globalSettingsForm.setValue("rate", value)}
      />
    </div>
  );
}
