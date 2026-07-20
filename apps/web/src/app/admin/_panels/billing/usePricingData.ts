"use client";

import {
  useQuery,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import { getAdminPricing, getSystemSettings } from "@/lib/apiClient";

const PRICING_QUERY_KEY = ["admin", "pricing"] as const;
const SETTINGS_QUERY_KEY = ["admin", "settings"] as const;
const BILLING_OVERVIEW_QUERY_KEY = [
  "admin",
  "billing",
  "overview",
] as const;

export function usePricingData(userBillingRootQueryKey: QueryKey) {
  const queryClient = useQueryClient();
  const pricingQuery = useQuery({
    queryKey: PRICING_QUERY_KEY,
    queryFn: getAdminPricing,
    retry: false,
  });
  const settingsQuery = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: getSystemSettings,
    retry: false,
  });
  const invalidateBilling = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: PRICING_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: BILLING_OVERVIEW_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: userBillingRootQueryKey }),
    ]);
  };

  return {
    pricingQuery,
    settingsQuery,
    invalidateBilling,
  };
}

export type InvalidateBilling = () => Promise<void>;
